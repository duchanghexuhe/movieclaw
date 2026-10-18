"""实时远程转码 Worker（docs/design/transcode-latency.md §5/§6 的 Windows 实现）。

与 Mac 客户端（macos/MovieClawTranscoder）说同一套 WS 协议
（protocol_version=1）：

- 连 ``/api/v1/transcode-worker/ws``（Bearer 令牌走 Authorization 头），
  hello 里申报 NVENC 能力；服务端按 ``_SUPPORTED_BACKENDS`` 放行 nvenc；
- 收 ``job.start``：起本机代理（上传代理 + 取源代理），把 ffmpeg 参数里的
  远程地址重写到本地回环，执行 ffmpeg；进度按 stderr 的 ``progress=`` 行
  解析后以 ``job.progress`` 上报；
- 收 ``job.stop``（seek 重启会带 force）杀进程；收 ``job.pause/resume``
  挂起/恢复；终态以 ``job.finished``/``job.failed`` 上报。

产物回传与 Mac 客户端同构（ArtifactUploadProxy + FragmentSegmenter）：
ffmpeg 用分片化 MP4（frag_keyframe+delay_moov+...）PUT 到本机回环代理，
代理按 MP4 盒子切段归到 HLS 分片栅格，逐块 PUT 给 NAS；一块传不动就整段
攒内存重传。取源代理则把 NAS 的取源请求按 8MiB 块缓存、顺序预取。

纯标准库实现（与 worker.py 同一约束）：WebSocket 握手/帧协议手写，
HTTP 代理用 http.server + urllib。
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import secrets
import socket
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

#: 与服务端 REMOTE_WORKER_PROTOCOL_VERSION 对齐（remote_worker.py）
#: v2：hello 报 roles+max_concurrency；job.start 带 class（realtime/preload）
PROTOCOL_VERSION = 2
#: 心跳间隔与服务端静默判离线窗口（对齐 Mac 客户端的 45 秒）
HEARTBEAT_SECONDS = 15.0
SERVER_SILENCE_SECONDS = 45.0
# ffmpeg 子进程不弹控制台窗：pythonw 启动时子进程无可继承的控制台，
# 不加这个每次起 ffmpeg 都会自建黑窗。常量仅 Windows 有，其它平台取 0
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
#: ffmpeg 输出分片化 MP4 的片段时长（与服务端 PROGRESSIVE_FRAGMENT_US 一致）
FRAGMENT_SECONDS = 0.5
#: 取源代理的块大小与预取参数（对齐 Mac 的 SourceReadProxy）
BLOCK_BYTES = 8 * 1024 * 1024
PREFETCH_AFTER_BLOCKS = 6

ARTIFACT_NAME_PATTERN_RULES = ("init.mp4", "stream.mp4", "live.m3u8", "index.m3u8", "seg")


def _allowed_artifact_name(name: str) -> bool:
    """与 NAS 端 ``_ARTIFACT_NAME`` 同一批名字（issue #444 教训：白名单必须
    两端一致，TS 分片才不会被本机拒收）。"""
    if name in ("init.mp4", "stream.mp4", "live.m3u8", "index.m3u8"):
        return True
    return name.startswith("seg") and name[8:] in (".m4s", ".ts") and name[3:8].isdigit()


# ---------------------------------------------------------------------------
# FragmentSegmenter：分片化 MP4 → init + 按 HLS 栅格归段的片段
# 与 ProgressiveSegments.swift 同一条切段规则（这是播放列表与分片对得上的前提）
# ---------------------------------------------------------------------------


def _read_u32(data: bytes, at: int) -> int:
    return int.from_bytes(data[at : at + 4], "big")


def _read_u64(data: bytes, at: int) -> int:
    return int.from_bytes(data[at : at + 8], "big")


@dataclass
class FragmentOutput:
    kind: str  # "init" | "fragment"
    segment: int | None
    data: bytes
    starts_segment: bool


class FragmentSegmenter:
    """把 ffmpeg 的分片化 MP4 流切成 init.mp4 与一个个片段。

    切段规则与 ffmpeg 的 HLS muxer 一模一样：第一段编号是 NAS 给的起转分片
    号；之后从这一轮第一帧起算，满一格后的第一个同步帧开新的一段。不能拿
    时间戳除以段长取整——``-force_key_frames`` 的 t 从这一轮第一帧起算。
    """

    MAX_BOX_BYTES = 256 * 1024 * 1024

    def __init__(self, segment_seconds: float, start_segment: int | None = None) -> None:
        self.segment_seconds = segment_seconds
        self.start_segment = start_segment
        self.buffer = b""
        self.ftyp: bytes | None = None
        self.video_track_id: int | None = None
        self.timescale = 90_000.0
        self.pending_moof: bytes | None = None
        self.leading_fragments = b""
        self.current_segment: int | None = None
        self.failure: str | None = None
        self.run_start: tuple[float, int] | None = None

    def append(self, data: bytes) -> list[FragmentOutput]:
        if self.failure is not None:
            return []
        self.buffer += data
        outputs: list[FragmentOutput] = []
        while True:
            box = self._next_box()
            if box is None:
                break
            kind, payload = box
            if kind == "ftyp":
                self.ftyp = payload
            elif kind == "moov":
                self._parse_moov(payload)
                outputs.append(
                    FragmentOutput("init", None, (self.ftyp or b"") + payload, True)
                )
            elif kind == "moof":
                self.pending_moof = payload
            elif kind == "mdat":
                if self.pending_moof is None:
                    continue
                moof = self.pending_moof
                self.pending_moof = None
                output = self._place(moof, moof + payload)
                if output is not None:
                    outputs.append(output)
            # styp/sidx/mfra/free：HLS 用不到
        return outputs

    def _next_box(self) -> tuple[str, bytes] | None:
        buf = self.buffer
        if len(buf) < 8:
            return None
        size = _read_u32(buf, 0)
        kind = buf[4:8].decode("latin-1")
        if size == 1:
            if len(buf) < 16:
                return None
            size = _read_u64(buf, 8)
        if size < 8 or size > self.MAX_BOX_BYTES:
            self.failure = f"分片化 MP4 里出现了长度不合理的盒子（{kind} {size} 字节）"
            self.buffer = b""
            return None
        if len(buf) < size:
            return None
        self.buffer = buf[size:]
        return kind, buf[:size]

    def _place(self, moof: bytes, fragment: bytes) -> FragmentOutput | None:
        timing = self._video_timing(moof)
        if self.run_start is None:
            if timing is None:
                self.leading_fragments += fragment
                return None
            index = self.start_segment if self.start_segment is not None else round(
                timing[0] / self.segment_seconds
            )
            self.run_start = (timing[0], index)
            self.current_segment = index
            data = self.leading_fragments + fragment
            self.leading_fragments = b""
            return FragmentOutput("fragment", index, data, True)
        current = self.current_segment
        if current is None:
            return None
        if timing is not None and timing[1]:
            start_pts, start_seg = self.run_start
            due = start_pts + (current - start_seg + 1) * self.segment_seconds - 0.001
            if timing[0] >= due:
                self.current_segment = current + 1
                return FragmentOutput("fragment", current + 1, fragment, True)
        return FragmentOutput("fragment", current, fragment, False)

    def _parse_moov(self, moov: bytes) -> None:
        for kind, trak in _children(moov):
            if kind != "trak":
                continue
            track_id: int | None = None
            handler: str | None = None
            scale: int | None = None
            for ckind, child in _children(trak):
                if ckind == "tkhd":
                    version = child[8]
                    offset = 12 + (16 if version == 1 else 8)
                    track_id = _read_u32(child, offset)
                elif ckind == "mdia":
                    for ikind, item in _children(child):
                        if ikind == "mdhd":
                            version = item[8]
                            offset = 12 + (16 if version == 1 else 8)
                            scale = _read_u32(item, offset)
                        elif ikind == "hdlr":
                            handler = item[16:20].decode("latin-1")
            if handler == "vide" and track_id is not None:
                self.video_track_id = track_id
                if scale and scale > 0:
                    self.timescale = float(scale)

    def _video_timing(self, moof: bytes) -> tuple[float, bool] | None:
        for kind, traf in _children(moof):
            if kind != "traf":
                continue
            track_id: int | None = None
            default_flags: int | None = None
            base = 0
            first_flags: int | None = None
            composition_offset = 0
            for bkind, data in _children(traf):
                if bkind == "tfhd":
                    flags = _read_u32(data, 8) & 0x00FF_FFFF
                    track_id = _read_u32(data, 12)
                    cursor = 16
                    if flags & 0x01:
                        cursor += 8
                    if flags & 0x02:
                        cursor += 4
                    if flags & 0x08:
                        cursor += 4
                    if flags & 0x10:
                        cursor += 4
                    if flags & 0x20:
                        default_flags = _read_u32(data, cursor)
                elif bkind == "tfdt":
                    base = (
                        _read_u64(data, 12)
                        if data[8] == 1
                        else _read_u32(data, 12)
                    )
                elif bkind == "trun":
                    word = _read_u32(data, 8)
                    version = word >> 24
                    flags = word & 0x00FF_FFFF
                    cursor = 16
                    if flags & 0x01:
                        cursor += 4
                    if flags & 0x04:
                        first_flags = _read_u32(data, cursor)
                        cursor += 4
                    if flags & 0x100:
                        cursor += 4
                    if flags & 0x200:
                        cursor += 4
                    if flags & 0x400:
                        if first_flags is None:
                            first_flags = _read_u32(data, cursor)
                        cursor += 4
                    if flags & 0x800:
                        raw = _read_u32(data, cursor)
                        composition_offset = (
                            raw if version == 0 else raw - (1 << 32) if raw >= (1 << 31) else raw
                        )
            if track_id != self.video_track_id:
                continue
            flags = first_flags if first_flags is not None else (default_flags or 0)
            # sample_is_non_sync_sample 是第 16 位
            is_sync = (flags & 0x0001_0000) == 0
            return ((base + composition_offset) / self.timescale, is_sync)
        return None


def _children(box: bytes) -> list[tuple[str, bytes]]:
    out: list[tuple[str, bytes]] = []
    cursor = 8
    while cursor + 8 <= len(box):
        size = int.from_bytes(box[cursor : cursor + 4], "big")
        if size < 8 or cursor + size > len(box):
            break
        kind = box[cursor + 4 : cursor + 8].decode("latin-1")
        out.append((kind, box[cursor : cursor + size]))
        cursor += size
    return out


# ---------------------------------------------------------------------------
# ArtifactUploadProxy：本机回环 HTTP 服务接 ffmpeg 的 PUT，切段逐块回传 NAS
# ---------------------------------------------------------------------------


#: 产物回传与取源都走内网 NAS 地址：显式绕过进程环境里的 http_proxy
#（Clash 常驻机器上 urllib 会把内网流量交给代理，吞吐掉到 1.7MB/s——
# 2026-10-15 真机：PUT 8MB 恒定 4.6s，是播放供片追不上转码的直接原因）。
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class ArtifactUploadProxy:
    """为一个实时任务提供仅监听回环地址的 HLS 产物代理。

    ffmpeg 的 HTTP HLS muxer 用 chunked body PUT 产物，连接断了没法重发已
    生成的分片。代理先在内存收齐单个产物，再用固定 Content-Length 传 NAS
    并做有限次重试。对 progressive 任务（stream.mp4），边收边切段
    （``FragmentSegmenter``）逐块回传，块失败退整段重传。
    """

    MAX_UPLOAD_ATTEMPTS = 5
    RETRYABLE_STATUS = {408, 425, 429, 499, 500, 502, 503, 504}
    RETRY_DELAY = 1.0

    def __init__(
        self,
        job_id: str,
        remote_base_url: str,
        query: str | None,
        segment_seconds: float = 4.0,
        start_segment: int | None = None,
        on_abandoned: Callable[[str, int, str], None] | None = None,
        on_rejected: Callable[[str], None] | None = None,
        on_first_segment: Callable[[], None] | None = None,
        debug: Callable[[str], None] | None = None,
    ) -> None:
        self.job_id = job_id
        self.remote_base_url = remote_base_url.rstrip("/")
        self.query = query
        self.segment_seconds = segment_seconds
        self.start_segment = start_segment
        self.on_abandoned = on_abandoned
        self.on_rejected = on_rejected
        self.on_first_segment = on_first_segment
        self._debug = debug or (lambda msg: None)
        self._lock = threading.Lock()
        self._stopped = False
        self._first_segment_reported = False
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, name=f"upload-{job_id[-6:]}", daemon=True
        )
        # 渐进式上传状态：一个分片的块按序串行传；传不动退整段
        self._segmenter: FragmentSegmenter | None = None
        self._segments: dict[int, dict] = {}
        self._upload_lock = threading.Lock()

    # -- 本机 HTTP 服务 -----------------------------------------------------

    def start(self) -> str:
        self._thread.start()
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def stop(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
        self._server.shutdown()
        self._server.server_close()

    @property
    def local_base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def rewrite(self, arguments: list[str]) -> list[str]:
        """把 ffmpeg 参数里的远程产物地址换成本机代理地址。"""
        remote_prefix = self.remote_base_url + "/"
        local_prefix = self.local_base_url + "/"
        out = []
        for arg in arguments:
            if arg.startswith(remote_prefix):
                out.append(local_prefix + arg[len(remote_prefix) :])
            elif arg == "init.mp4" or arg.startswith("init.mp4?"):
                out.append(local_prefix + arg)
            else:
                out.append(arg)
        return out

    @staticmethod
    def remote_artifact_base_url(arguments: list[str]) -> tuple[str, str] | None:
        """从 ffmpeg 参数里找产物回传基址与其 query（token）。"""
        for arg in arguments:
            if "/artifacts/" not in arg:
                continue
            parsed = urllib.parse.urlsplit(arg)
            if "/artifacts/" not in parsed.path:
                continue
            prefix = parsed.path[: parsed.path.index("/artifacts/")]
            base = urllib.parse.urlunsplit(
                (parsed.scheme, parsed.netloc, prefix + "/artifacts", "", "")
            )
            return base, parsed.query or ""
        return None

    def _make_handler(self):  # type: ignore[no-untyped-def]
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # 静默：ffmpeg 的 PUT 很吵
                pass

            def do_PUT(self) -> None:  # noqa: N802
                name = urllib.parse.unquote(
                    urllib.parse.urlsplit(self.path).path.lstrip("/")
                )
                if not _allowed_artifact_name(name):
                    # ffmpeg 不看响应码：白名单外必须让任务层知道（对齐 Mac 端 rejectArtifact）
                    if proxy.on_rejected is not None:
                        proxy.on_rejected(name)
                    self._reply(404)
                    self.close_connection = True
                    return
                body = self._read_body()
                if body is None:
                    self.close_connection = True
                    return
                self._reply(201)

                if name == "stream.mp4":
                    proxy._feed_progressive(body)
                    return
                # 非 progressive：整产物直接传（init/playlist/整段落盘分片）
                query = urllib.parse.urlsplit(self.path).query or proxy.query
                self._async_upload(name, body, query)

            def _read_body(self) -> bytes | None:
                """读 PUT 请求体：ffmpeg 的 HTTP muxer 用 chunked 编码（实测无
                Content-Length），按 CL 读会得 0 字节并掐断连接（-10053）。"""
                if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
                    chunks: list[bytes] = []
                    try:
                        while True:
                            size_line = self.rfile.readline(64).strip()
                            size = int(size_line.split(b";")[0], 16)
                            if size == 0:
                                # 尾随 header 直到空行
                                while True:
                                    trailer = self.rfile.readline(4096)
                                    if trailer in (b"\r\n", b"\n", b""):
                                        break
                                return b"".join(chunks)
                            chunk = self.rfile.read(size)
                            if len(chunk) < size:
                                return None
                            self.rfile.read(2)  # CRLF
                            chunks.append(chunk)
                    except (OSError, ValueError):
                        return None
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    return self.rfile.read(length) if length else b""
                except OSError:
                    return None

            def _reply(self, status: int) -> None:
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()

            def _async_upload(self, name: str, body: bytes, query: str | None) -> None:
                threading.Thread(
                    target=proxy._upload_whole,
                    args=(name, body, query),
                    name=f"up-{name[:12]}",
                    daemon=True,
                ).start()

        return Handler

    # -- 渐进式（stream.mp4）路径 -------------------------------------------

    def feed_bytes(self, data: bytes) -> None:
        """TailFeeder 的入口：中转文件的新增字节喂进分片器并上传。"""
        if self._stopped or not data:
            return
        segmenter = self._ensure_segmenter()
        outputs = segmenter.append(data)
        self._consume_outputs(outputs)

    def _ensure_segmenter(self) -> FragmentSegmenter:
        with self._lock:
            if self._segmenter is None:
                self._segmenter = FragmentSegmenter(self.segment_seconds, self.start_segment)
            return self._segmenter

    def _feed_progressive(self, body: bytes) -> None:
        if self._stopped or not body:
            return
        segmenter = self._ensure_segmenter()
        outputs = segmenter.append(body)
        threading.Thread(
            target=self._consume_outputs, args=(outputs,), name="seg-out", daemon=True
        ).start()

    def _consume_outputs(self, outputs: list[FragmentOutput]) -> None:
        for output in outputs:
            if self._stopped:
                return
            if output.kind == "init":
                self._debug(f"upload init.mp4 begin ({len(output.data)}B)")
                self._upload_whole("init.mp4", output.data, self.query)
                self._debug("upload init.mp4 done")
                continue
            name = f"seg{output.segment:05d}.m4s"
            with self._upload_lock:
                if self._stopped:
                    return
                seg = self._segments.setdefault(
                    output.segment,
                    {"name": name, "buffer": b"", "starts_sent": False},
                )
                seg["buffer"] += output.data
                if output.starts_segment:
                    if not self._first_segment_reported:
                        self._first_segment_reported = True
                        if self.on_first_segment is not None:
                            self.on_first_segment()
                    # 下一段开始 = 上一段已完整：立即整段上传（Windows 回环
                    # PUT 逐 fragment 一发要 800ms，跟不上 ffmpeg 0.5s/片的
                    # 产出速度，积压到会话回收——首日端到端实测。整段一 PUT
                    # 换 4 秒的起播延迟，换来与转码速度匹配的吞吐）
                    finished = [
                        s for k, s in self._segments.items()
                        if k < output.segment and not s.get("uploaded")
                    ]
                    for done in finished:
                        self._segments[output.segment]  # 保持引用结构稳定
                        done_key = done["name"]
                        data = done["buffer"]
                        done["uploaded"] = True
                        self._debug(f"upload {done_key} begin ({len(data)}B)")
                        self._upload_whole(done_key, data, self.query)
                        self._debug(f"upload {done_key} done")
                        # 从待收尾表移除（保留当前段）
                        for k in [k for k, s in self._segments.items()
                                  if s.get("uploaded") and k != output.segment]:
                            del self._segments[k]

    def finish_progressive(self) -> None:
        """ffmpeg 正常收尾：把仍在缓冲的最后一段整段上传。"""
        with self._upload_lock:
            for seg in sorted(self._segments.values(), key=lambda s: s["name"]):
                if self._stopped:
                    return
                if not seg.get("uploaded"):
                    self._upload_whole(seg["name"], seg["buffer"], self.query)
            self._segments.clear()

    # -- 上传执行 -----------------------------------------------------------

    def _remote_url(self, name: str, extra_query: str | None) -> str:
        url = f"{self.remote_base_url}/{urllib.parse.quote(name)}"
        queries = [q for q in (self.query, extra_query) if q]
        if queries:
            url += "?" + "&".join(queries)
        return url

    def _put(self, url: str, body: bytes) -> int:
        request = urllib.request.Request(
            url, data=body, method="PUT",
            headers={"Content-Type": "application/octet-stream", "Cache-Control": "no-store"},
        )
        # **显式直连**：urllib 默认会读进程环境里的 http_proxy——挂机的机器带着
        # Clash 等代理环境变量时，内网 PUT 被绕去代理（实测稳定 4.6s/8MB =
        # 1.7MB/s，供片永远追不上播放）。NAS 内网地址直连永远是对的。
        began = time.monotonic()
        try:
            with _NO_PROXY_OPENER.open(request, timeout=30) as resp:
                resp.read()
                status = resp.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        except OSError:
            return 502
        self._debug(
            f"put {url.rsplit('/', 1)[-1].split('?')[0]}: {len(body)}B status={status}"
            f" in {time.monotonic() - began:.2f}s"
        )
        return status

    def _upload_whole(self, name: str, body: bytes, query: str | None) -> bool:
        url = self._remote_url(name, query)
        for attempt in range(1, self.MAX_UPLOAD_ATTEMPTS + 1):
            if self._stopped:
                return False
            status = self._put(url, body)
            if 200 <= status < 300:
                return True
            if status not in self.RETRYABLE_STATUS or attempt == self.MAX_UPLOAD_ATTEMPTS:
                if self.on_abandoned is not None:
                    self.on_abandoned(name, status, f"NAS 返回 HTTP {status}")
                return False
            time.sleep(self.RETRY_DELAY * attempt)
        return False


# ---------------------------------------------------------------------------
# SourceReadProxy：按块缓存与预取的取源代理（ffmpeg 的 -i 指向它）
# ---------------------------------------------------------------------------


class SourceReadProxy:
    """把 NAS 取源请求代理到本机：连接按块（8MiB）缓存、顺序预取。

    ffmpeg 直连 NAS 也能干活，代理的价值在 seek 频繁时复用连接与块缓存。
    代理起不来不影响转码（ffmpeg 直连 NAS），与 Mac 端同一取舍。
    """

    def __init__(
        self,
        job_id: str,
        origin_url: str,
        debug=None,  # Callable[[str], None] | None：调试回调（MOVIECLAW_WORKER_DEBUG）
    ) -> None:
        self.job_id = job_id
        self._debug = debug
        parsed = urllib.parse.urlsplit(origin_url)
        self.origin = urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, "", "", "")
        )
        self._path = parsed.path
        self._query = parsed.query
        self._size: int | None = None
        self._cache: dict[int, bytes] = {}
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, name=f"src-{job_id[-6:]}", daemon=True
        )

    def start(self) -> str:
        self._thread.start()
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def rewrite(self, arguments: list[str], local_base_url: str) -> list[str]:
        out = []
        for arg in arguments:
            if arg.startswith(self.origin):
                parsed = urllib.parse.urlsplit(arg)
                out.append(
                    local_base_url + parsed.path + (("?" + parsed.query) if parsed.query else "")
                )
            else:
                out.append(arg)
        return out

    @staticmethod
    def remote_source_url(arguments: list[str]) -> str | None:
        """ffmpeg 参数里的 -i 若指向 NAS 的取源端点，返回它。"""
        for index, arg in enumerate(arguments):
            if arg != "-i" or index + 1 >= len(arguments):
                continue
            candidate = arguments[index + 1]
            parsed = urllib.parse.urlsplit(candidate)
            if parsed.scheme in ("http", "https") and "/transcode-worker/sessions/" in parsed.path:
                return candidate
        return None

    @staticmethod
    def strip_proxy_target(arguments: list[str]) -> list[str]:
        """代理关闭时原样返回参数：-i 本就指向 NAS 的 /source，ffmpeg 直连即可。

        留这个函数是为了让调用点表达「这里刻意不经过代理」而不只是"删掉那段
        代码"；将来若代理修好了，把这里换回 remote_source_url 分支即可。
        """
        return arguments

    def _make_handler(self):  # type: ignore[no-untyped-def]
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802
                # **按块服务 Range 请求**，绝不透传长流（流式透传在 ffmpeg 的
                # 短读模式下死锁：ffmpeg 读一小段就停，代理还卡在往上读 17GB，
                # ffmpeg 的下一个请求没人接——首日端到端实测）。块模式与 Mac
                # 端同构：每个请求独立响应，数据按 8MiB 块从上游取并缓存。
                split = urllib.parse.urlsplit(self.path)
                # 非取源路径（ffconcat 清单等）直接透传——它们是小文件，读完即走
                if "/source" not in split.path and not split.path.endswith(".ffconcat"):
                    self._passthrough()
                    return
                import re

                total = proxy._total_size()
                if total is None:
                    self.send_response(502)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                range_header = self.headers.get("Range", "")
                match = re.match(r"bytes=(\d+)-(\d*)", range_header)
                start = int(match.group(1)) if match else 0
                # 无 end：HTTP 允许只回一部分（Content-Range 标明完整区间即可），
                # 给一个块窗口让 ffmpeg 继续发请求，而不是赌它读完 17GB
                if match and match.group(2):
                    end = min(int(match.group(2)), total - 1)
                else:
                    end = min(start + BLOCK_BYTES - 1, total - 1)
                if start >= total:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{total}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                payload = bytearray()
                offset = start
                while offset <= end:
                    block_index, in_block = divmod(offset, BLOCK_BYTES)
                    block = proxy._block(block_index)
                    if block is None:
                        self.send_response(502)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    piece = block[in_block : in_block + (end - offset + 1)]
                    payload += piece
                    offset += len(piece)
                self.send_response(206)
                self.send_header("Content-Type", "video/x-matroska")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Content-Range", f"bytes {start}-{offset - 1}/{total}")
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                # ffmpeg 掐断（seek）：正常
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    self.wfile.write(payload)

            def _passthrough(self) -> None:
                """小文件直通（清单/探针）：读进内存再转发，量小无碍。"""
                split = urllib.parse.urlsplit(self.path)
                url = proxy.origin + split.path + (("?" + split.query) if split.query else "")
                try:
                    resp = _NO_PROXY_OPENER.open(url, timeout=30)
                except urllib.error.HTTPError as exc:
                    self.send_response(exc.code)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    exc.close()
                    return
                except OSError:
                    self.send_response(502)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                with resp:
                    body = resp.read()
                    self.send_response(resp.status)
                    for key in ("Content-Type",):
                        if key in resp.headers:
                            self.send_header(key, resp.headers[key])
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                        self.wfile.write(body)

            # HEAD 供 ffprobe 探时长（尽力而为：直连也是这个待遇）
            def do_HEAD(self) -> None:  # noqa: N802
                total = proxy._total_size()
                self.send_response(200)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(total) if total else "0")
                self.end_headers()

        return Handler

    def _total_size(self) -> int | None:
        """源文件总大小：向上游发一个 1 字节 Range，解析 Content-Range 总量。"""
        if self._size is not None:
            return self._size
        url = self.origin + self._path + "?" + self._query
        request = urllib.request.Request(url, headers={"Range": "bytes=0-0"})
        try:
            with _NO_PROXY_OPENER.open(request, timeout=30) as resp:
                content_range = resp.headers.get("Content-Range", "")
                resp.read()
        except (urllib.error.HTTPError, OSError):
            return None
        import re

        match = re.match(r"bytes \d+-\d+/(\d+)", content_range)
        if not match:
            return None
        self._size = int(match.group(1))
        return self._size

    def _block(self, index: int, *, allow_prefetch: bool = True) -> bytes | None:
        with self._lock:
            cached = self._cache.get(index)
        if cached is not None:
            return cached
        # 越过文件尾的块（预取越界）直接放弃：上游 416，白费一趟
        total = self._total_size()
        if total is not None and index * BLOCK_BYTES >= total:
            return None
        start = index * BLOCK_BYTES
        # 上游是标准 Range 端点：Range 头指明块区间（offset/bytes 参数是
        # 早期设计的残留，真实上游并不认识）
        url = self.origin + self._path + "?" + self._query
        request = urllib.request.Request(
            url, headers={"Range": f"bytes={start}-{start + BLOCK_BYTES - 1}"}
        )
        began = time.monotonic()
        try:
            with _NO_PROXY_OPENER.open(request, timeout=60) as resp:
                data = resp.read()
        except (urllib.error.HTTPError, OSError):
            if self._debug:
                self._debug(f"src block {index}: FETCH FAILED ({time.monotonic() - began:.2f}s)")
            return None
        if self._debug:
            self._debug(
                f"src block {index}: {len(data)}B in {time.monotonic() - began:.2f}s"
                f" (cache={len(self._cache)})"
            )
        with self._lock:
            self._cache[index] = data
            # 缓存上限 32 块（256MiB）：seek 走过的块不必永久留
            while len(self._cache) > 32:
                self._cache.pop(next(iter(self._cache)))
        # 顺序读预取后两块。**只有按需取（ffmpeg 真在等的那块）允许派生预取**，
        # 预取自身不级联——曾经预取成功也再派生两块，链式翻倍往前冲（ffmpeg
        # 还在读开头，预取已经冲到几十块之外），8MiB 块的 FIFO 缓存被预取洪水
        # 冲垮，工作集反复被挤掉重取，深 seek 首帧被拖到 40 秒开外，播放器
        # 等不到分片直接降档（2026-10-15 真机 debug 日志：block 6 被重取 6 次）。
        if allow_prefetch:
            for ahead in (index + 1, index + 2):
                threading.Thread(
                    target=self._prefetch, args=(ahead,), name="prefetch", daemon=True
                ).start()
        return data

    def _prefetch(self, index: int) -> None:
        with self._lock:
            if index in self._cache:
                return
        self._block(index, allow_prefetch=False)


# ---------------------------------------------------------------------------
# RealtimeClient：WS 控制连接 + 任务执行
# ---------------------------------------------------------------------------


class _SocketIdle(Exception):
    """recv 在帧边界处超时：连接没坏，只是暂时没消息。

    不能做成 OSError 子类——上层 run_forever 把 OSError 一律当断线处理；
    曾经帧边界超时与对端关闭都走 ``return None`` 同一条路，服务端安静
    5 秒（心跳间隔 15 秒，必然出现）Worker 就自断重连，实测每隔几秒
    「上线→离线」循环一整天。
    """


#: 帧读到一半的收尾宽限。recv_json 的空等超时压得很短（见主循环注释），
#: 大报文（job.start 带整条 ffmpeg 命令）会跨几个 TCP 段，后一段慢半拍
#: 不该被当成断线。
_FRAME_GRACE_S = 5.0


class ServerClosed(OSError):
    """服务端发来关闭帧（RFC 6455）。code/reason 是服务端写好的理由：1008 就是「不接受你」。"""

    def __init__(self, code: int, reason: str) -> None:
        self.code = code
        self.reason = reason
        text = f"服务端关闭了连接（{code}）"
        super().__init__(f"{text}：{reason}" if reason else text)


def rejection_kind(reason: str) -> str:
    """1008 关闭理由分三类：auth（凭证失效）/ remote_disabled（远程转码没开）/ other。

    文字与 macOS 转码器的 NASRejection 一致（服务端 transcode_worker.py 写死的几句，以及
    注销转码器时 login_devices 断开连接的那句）：改服务端文案时两边一起看。
    """
    if any(word in reason for word in ("凭证", "吊销", "注销")):
        return "auth"
    if "尚未启用远程转码" in reason:
        return "remote_disabled"
    return "other"


#: 1008 拒绝后的重试间隔（秒）：凭证失效慢慢试（NAS 从备份恢复后凭证可能重新有效），
#: 开关没开每分钟问一次，其他原因也每分钟一次——与 Mac 转码器一致，不走指数退避
_REJECT_RETRY_S = {"auth": 300.0, "remote_disabled": 60.0, "other": 60.0}


class _WebSocket:
    """RFC6455 客户端的最小实现（文本帧收发 + 心跳），够本协议使用。"""

    def __init__(self, host: str, port: int, path: str, token: str, timeout: float = 10.0) -> None:
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._sock.settimeout(timeout)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"Authorization: Bearer {token}\r\n\r\n"
        )
        self._sock.sendall(request.encode())
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise OSError("WebSocket 握手无响应")
            response += chunk
        head = response.split(b"\r\n\r\n", 1)[0].decode("latin-1")
        if " 101 " not in head.splitlines()[0]:
            raise OSError(f"WebSocket 握手被拒：{head.splitlines()[0] if head else 'empty'}")
        self._buffer = response.split(b"\r\n\r\n", 1)[1]
        self._closed = False
        self._lock = threading.Lock()

    def send_json(self, payload: dict) -> None:
        self.send_text(json.dumps(payload, ensure_ascii=False))

    def send_text(self, text: str) -> None:
        data = text.encode("utf-8")
        with self._lock:
            self._sock.sendall(self._frame(0x1, data))

    def _frame(self, opcode: int, data: bytes) -> bytes:
        mask_bit = 0x80
        header = bytes([0x80 | opcode])
        length = len(data)
        if length < 126:
            header += bytes([mask_bit | length])
        elif length < 65536:
            header += bytes([mask_bit | 126]) + struct.pack(">H", length)
        else:
            header += bytes([mask_bit | 127]) + struct.pack(">Q", length)
        mask = secrets.token_bytes(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        return header + mask + masked

    def recv_json(self, timeout: float) -> dict | None:
        """收一条文本消息；超时返回 None，连接断开抛 OSError。"""
        self._sock.settimeout(timeout)
        try:
            return self._read_message()
        except _SocketIdle:
            return None

    def _read_message(self) -> dict | None:
        while True:
            header = self._recv_exact(2)
            if header is None:
                raise OSError("连接已关闭")
            opcode = header[0] & 0x0F
            masked = header[1] & 0x80
            length = header[1] & 0x7F
            if length == 126:
                ext = self._recv_exact(2)
                if ext is None:
                    raise OSError("连接已关闭")
                length = struct.unpack(">H", ext)[0]
            elif length == 127:
                ext = self._recv_exact(8)
                if ext is None:
                    raise OSError("连接已关闭")
                length = struct.unpack(">Q", ext)[0]
            mask = self._recv_exact(4) if masked else None
            payload = self._recv_exact(length) if length else b""
            if payload is None:
                raise OSError("连接已关闭")
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:  # close：载荷 = 2 字节状态码 + UTF-8 理由（都可省略）
                code = struct.unpack(">H", payload[:2])[0] if len(payload) >= 2 else 1005
                reason = payload[2:].decode("utf-8", errors="replace")
                self.close()  # RFC 6455：收到关闭帧要回一个再断开，服务端才好收尾
                raise ServerClosed(code, reason)
            if opcode == 0x9:  # ping → pong
                with self._lock:
                    self._sock.sendall(self._frame(0xA, payload))
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode == 0x1:
                return json.loads(payload.decode("utf-8"))

    def _recv_exact(self, count: int) -> bytes | None:
        chunks = b""
        # 握手响应可能与第一条 WS 帧挤在同一个 TCP 段里：先吃残留再读
        # socket，否则这帧被当丢失、流解析从此错位
        if self._buffer:
            chunks, self._buffer = self._buffer[:count], self._buffer[count:]
        grace_until: float | None = None
        while len(chunks) < count:
            try:
                chunk = self._sock.recv(count - len(chunks))
            except TimeoutError as exc:
                if not chunks:
                    # 帧边界超时≠断线：让 recv_json 转成 None 继续等；
                    # 半截数据卡住才算连接有问题，维持抛超时由上层断线
                    raise _SocketIdle from exc
                # 帧开了头才超时：多半是跨段报文的后一段慢了半拍，不是断线。
                # 给一段宽限收完本帧，宽限烧完才按断线上抛——空等超时被压短
                # 之后，没有这道宽限慢帧会被误杀（fc03ad2d 同类教训）
                if grace_until is None:
                    grace_until = time.monotonic() + _FRAME_GRACE_S
                elif time.monotonic() > grace_until:
                    raise
                self._sock.settimeout(_FRAME_GRACE_S)
                continue
            if not chunk:
                return chunks if chunks else None
            chunks += chunk
        return chunks

    def close(self) -> None:
        # 对端先发来关闭帧时也要走到 socket.close()：只标记不关，fd 会挂到 GC 才放
        if not self._closed:
            self._closed = True
            with contextlib.suppress(OSError), self._lock:
                self._sock.sendall(self._frame(0x8, b""))
        with contextlib.suppress(OSError):
            self._sock.close()


# ---------------------------------------------------------------------------
# TailFeeder：跟读持续增长的本地文件（ffmpeg 的中转输出），逐块交给分片器
# ---------------------------------------------------------------------------


class TailFeeder:
    """跟读 ffmpeg 正在写的分片化 MP4 中转文件，把新增字节推给回调。

    与 Mac 端"ffmpeg PUT 到回环代理"等价，只是数据通道从 TCP 换成了本地
    文件——绕开 Windows 上 SMB 源 + 回环 PUT 的死锁（见 RealtimeJob._run_inner
    注释）。轮询 0.2 秒读新增字节；文件比上次读更短 = seek 换了新轮次，重置。
    """

    def __init__(self, path, on_bytes, on_error=None, debug=None) -> None:  # type: ignore[no-untyped-def]
        from pathlib import Path as _P

        self.path = _P(path)
        self.on_bytes = on_bytes
        self._debug = debug or (lambda msg: None)
        self.on_error = on_error
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._offset = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="tail", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)

    def drained(self) -> bool:
        """ffmpeg 已退出后：文件是否已读完（供收尾等待）。"""
        try:
            return self.path.stat().st_size <= self._offset
        except OSError:
            return True

    def _loop(self) -> None:
        self._debug("tail loop started")
        while not self._stop.is_set():
            try:
                size = self.path.stat().st_size
            except OSError:
                self._debug("tail stat failed")
                self._stop.wait(0.2)
                continue
            if size < self._offset:
                self._offset = 0  # 新一轮（seek 重启覆盖了文件）
            if size > self._offset:
                try:
                    with open(self.path, "rb") as f:
                        f.seek(self._offset)
                        data = f.read(size - self._offset)
                except OSError as exc:
                    if self.on_error is not None:
                        self.on_error(f"读中转文件失败：{exc}")
                    self._debug(f"tail read failed: {exc}")
                    self._stop.wait(0.5)
                    continue
                self._offset += len(data)
                if data:
                    if self._offset < 2 * 1024 * 1024 or self._offset % (8 * 1024 * 1024) < 65536:
                        self._debug(f"tail fed {len(data)}B (offset={self._offset})")
                    self.on_bytes(data)
            self._stop.wait(0.2)


# ---------------------------------------------------------------------------
# RealtimeJob：一个实时转码任务的执行状态机
# ---------------------------------------------------------------------------


class RealtimeJob:
    """一个实时转码任务的执行状态机。"""

    def __init__(
        self,
        job_id: str,
        message: dict,
        ffmpeg: str,
        client: RealtimeClient,
    ) -> None:
        self.job_id = job_id
        self.attempt_id = str(message.get("attempt_id") or job_id)
        self.display_name = str(message.get("display_name") or "")
        self.client = client
        self.ffmpeg = ffmpeg
        self.process: subprocess.Popen | None = None
        self.upload_proxy: ArtifactUploadProxy | None = None
        self.source_proxy: SourceReadProxy | None = None
        self.stop_requested = False
        self.paused = threading.Event()
        self.stderr_tail: list[str] = []
        self._stderr_lock = threading.Lock()
        self._last_progress_sent = 0.0
        self._temp_stream: Path | None = None
        self._debug_lock = threading.Lock()
        self._debug_t0 = time.monotonic()
        self._debug_file = (
            self._work_dir() / "ffmpeg-debug.log"
            if os.environ.get("MOVIECLAW_WORKER_DEBUG")
            else ""
        )

    def _work_dir(self) -> Path:
        directory = Path(os.environ.get("TEMP", ".")) / f"mc-realtime-{self.job_id[-8:]}"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    # -- 生命周期 -----------------------------------------------------------

    def stop(self, force: bool) -> None:
        self.stop_requested = True
        process = self.process
        if process is None or process.poll() is not None:
            return
        try:
            if force and os.name == "nt":
                process.kill()
            else:
                process.terminate()
        except OSError:
            pass

    def pause(self) -> None:
        self.paused.set()

    def resume(self) -> None:
        self.paused.clear()

    def _note_stderr(self, line: str) -> None:
        with self._stderr_lock:
            self.stderr_tail.append(line)
            if len(self.stderr_tail) > 40:
                self.stderr_tail.pop(0)
        self._debug_log(line)

    def _debug_log(self, message: str) -> None:
        """调试文件：MOVIECLAW_WORKER_DEBUG=1 时把 ffmpeg stderr 与代理耗时
        逐行带时间戳落盘（远程排障用——stderr_tail 只留 40 行且仅在失败时
        上报，播放器主动停掉的会话什么都查不到）。文件随任务临时目录清理。"""
        if not self._debug_file:
            return
        try:
            stamp = time.monotonic() - self._debug_t0
            with (
                self._debug_lock,
                open(self._debug_file, "a", encoding="utf-8", errors="replace") as fh,
            ):
                fh.write(f"[+{stamp:7.2f}s] {message}\n")
        except OSError:
            pass

    def _gate_on_pause(self) -> None:
        # 暂停用「停止喂数据」近似：ffmpeg 读源阻塞在取源代理上即等于暂停。
        while self.paused.is_set() and not self.stop_requested:
            time.sleep(0.2)

    def run(self, message: dict) -> None:
        """执行一轮 ffmpeg；结束时上报终态。由调用方在独立线程里调。"""
        try:
            self._run_inner(message)
        except Exception as exc:  # noqa: BLE001
            if not self.stop_requested:
                self.client._send_safe(
                    {
                        "type": "job.failed",
                        "job_id": self.job_id,
                        "attempt_id": self.attempt_id,
                        "error": str(exc)[:2000],
                    }
                )
        finally:
            # 任务线程退出即释放并发槽位：条目原本只在 NAS 显式 job.stop 或同
            # job 换轮次重派时移除，ffmpeg 自然结束（无论成败）条目留在表里，
            # 之后每个 job.start 都被"Worker 并发已满"挡住，直到 Worker 重连
            #（2026-10-15 真机：两个失败任务把并发占死约 5 分钟）。
            # **身份校验**：seek 换轮次时旧线程的收尾不能把新轮次的条目弹掉
            with self.client.jobs_lock:
                if self.client.jobs.get(self.job_id) is self:
                    self.client.jobs.pop(self.job_id, None)

    def _run_inner(self, message: dict) -> None:
        arguments = list(message.get("ffmpeg_args") or [])
        if not arguments:
            raise RuntimeError("任务缺少 ffmpeg_args")
        self._debug_log("argv: " + " ".join(arguments)[:1500])
        self._debug_log(
            f"start_segment={message.get('start_segment')} "
            f"segment_seconds={message.get('segment_seconds')}"
        )
        # 1) 上传代理：从参数里找产物基址。**ffmpeg 不直接 PUT 代理**——
        # Windows 上 SMB 源 + 回环 PUT 组合会死锁（ffmpeg 发完 header 即 RST，
        # 实测 lavfi/本地源都正常，与服务器行为无关，2026-10-14 二分钟：
        # -seekable/-flush_packets/响应时机全排除）。改为 ffmpeg 写**本地中转
        # 文件**，TailFeeder 跟读新增字节喂给分片器上传——本地顺序写几 GB/s，
        # 不构成瓶颈；seek 重启换新文件，任务结束清理。
        remote = ArtifactUploadProxy.remote_artifact_base_url(arguments)
        feeder: TailFeeder | None = None
        if remote is not None:
            base, query = remote
            segment_seconds = float(message.get("segment_seconds") or 4)
            start_segment = message.get("start_segment")
            self.upload_proxy = ArtifactUploadProxy(
                self.job_id,
                base,
                query,
                segment_seconds=segment_seconds,
                start_segment=int(start_segment) if start_segment is not None else None,
                on_abandoned=self._on_artifact_abandoned,
                on_rejected=self._on_artifact_rejected,
                debug=self._debug_log,
            )
            self.upload_proxy.start()
            stream_index = arguments.index("stream.mp4") if "stream.mp4" in arguments else -1
            if stream_index < 0:
                for i, a in enumerate(arguments):
                    if a.split("?")[0].endswith("/stream.mp4"):
                        stream_index = i
                        break
            if stream_index < 0:
                raise RuntimeError("找不到 stream.mp4 输出参数")
            self._temp_stream = self._work_dir() / "stream.frag.mp4"
            arguments[stream_index] = str(self._temp_stream)
            feeder = TailFeeder(
                self._temp_stream,
                on_bytes=self.upload_proxy.feed_bytes,
                on_error=lambda msg: self._on_artifact_abandoned("stream.mp4", 500, msg),
                debug=self._debug_log,
            )
        # 2) 取源代理：**默认关闭**，ffmpeg 直连 NAS 的 /source 端点。
        #
        # 曾经默认开启（"seek 频繁时复用连接与块缓存"），但真机实测它是毒药：
        # 代理经 NAS:8096 逐块拉 8MiB，实测读速只有 ~1.7MB/s（块耗时恒定
        # 4.6s，与服务端 6ms 的响应时间、磁盘 38MB/s、同链路 SMB 30MB/s 全对
        # 不上——瓶颈在 dockerd 用户态端口代理对单连接大响应的转发），ffmpeg
        # 转码速度被压到 0.9× 实时，追不上播放器，首帧 40 秒起、供片 0.35×，
        # 播放器等不到分片降档。禁用代理直连后同一会话 speed p50=10.3×。
        # MOVIECLAW_WORKER_SOURCE_PROXY=1 可显式打开（排查用）。
        if not os.environ.get("MOVIECLAW_WORKER_SOURCE_PROXY"):
            arguments = SourceReadProxy.strip_proxy_target(arguments)
        # 3) 起 ffmpeg：-progress pipe:1 由服务端拼好；stdout 解析进度
        with self._stderr_lock:
            self.stderr_tail.clear()
        self.process = subprocess.Popen(
            [self.ffmpeg, *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=_NO_WINDOW,
        )
        if feeder is not None:
            feeder.start()
        self.client._send_safe(
            {"type": "job.accepted", "job_id": self.job_id, "attempt_id": self.attempt_id}
        )
        stderr_thread = threading.Thread(
            target=self._pump_stderr, name=f"err-{self.job_id[-6:]}", daemon=True
        )
        stderr_thread.start()
        exit_code = self._pump_stdout_and_wait()
        stderr_thread.join(timeout=5)
        if feeder is not None:
            # ffmpeg 退出后文件里可能还有没读完的尾巴；给 feeder 一点时间追平
            deadline = time.time() + 10
            while time.time() < deadline and not feeder.drained():
                time.sleep(0.2)
            feeder.stop()
        if self.upload_proxy is not None:
            self.upload_proxy.finish_progressive()
        if self.source_proxy is not None:
            self.source_proxy.stop()
        try:
            if self._temp_stream is not None and self._temp_stream.exists():
                self._temp_stream.unlink()
        except OSError:
            pass
        if self.upload_proxy is not None:
            # 给在途上传一点收尾时间
            deadline = time.time() + 15
            while time.time() < deadline and not self.upload_proxy._stopped:
                with self.upload_proxy._upload_lock:
                    if not self.upload_proxy._segments:
                        break
                time.sleep(0.2)
            self.upload_proxy.stop()
        if self.stop_requested:
            return  # NAS 叫停（seek 旧轮次）：不报终态
        tail = " ".join(self._stderr_tail_copy())[-2000:]
        if exit_code == 0:
            self.client._send_safe(
                {
                    "type": "job.finished",
                    "job_id": self.job_id,
                    "attempt_id": self.attempt_id,
                    "exit_code": 0,
                }
            )
        else:
            self.client._send_safe(
                {
                    "type": "job.failed",
                    "job_id": self.job_id,
                    "attempt_id": self.attempt_id,
                    "error": (
                        f"ffmpeg 退出码 {exit_code}：{tail}"
                        if tail
                        else f"ffmpeg 退出码 {exit_code}"
                    ),
                }
            )

    def _on_artifact_abandoned(self, name: str, status: int, reason: str) -> None:
        self.client._send_safe(
            {
                "type": "job.artifact_failed",
                "job_id": self.job_id,
                "attempt_id": self.attempt_id,
                "name": name,
                "status": status,
                "error": reason,
            }
        )

    def _on_artifact_rejected(self, name: str) -> None:
        self.stop(force=True)
        self.client._send_safe(
            {
                "type": "job.failed",
                "job_id": self.job_id,
                "attempt_id": self.attempt_id,
                "error": f"产物 {name} 不在白名单内，已拒收（两端版本不一致？）",
            }
        )

    def _stderr_tail_copy(self) -> list[str]:
        with self._stderr_lock:
            return list(self.stderr_tail)

    def _pump_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        pending = b""
        while True:
            chunk = process.stderr.read(4096)
            if not chunk:
                break
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                self._note_stderr(line.decode("utf-8", "replace").rstrip())
        if pending:
            self._note_stderr(pending.decode("utf-8", "replace").rstrip())

    def _pump_stdout_and_wait(self) -> int:
        """解析 ffmpeg 的 -progress pipe:1 行并节流上报进度；返回退出码。"""
        process = self.process
        if process is None or process.stdout is None:
            return -1
        values: dict[str, str] = {}
        pending = b""
        while True:
            chunk = process.stdout.read(512)
            if not chunk:
                break
            self._gate_on_pause()
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                text = line.decode("utf-8", "replace").strip()
                if "=" not in text:
                    continue
                key, _, value = text.partition("=")
                values[key] = value
                if key != "progress":
                    continue
                out_time_ms = self._out_time_ms(values)
                speed = values.get("speed")
                phase = value
                now = time.time()
                if phase == "end" or now - self._last_progress_sent >= 0.8:
                    self._last_progress_sent = now
                    if out_time_ms is not None:
                        self._debug_log(
                            f"progress out_time_ms={out_time_ms} speed={speed or '?'}"
                        )
                    payload: dict[str, Any] = {
                        "type": "job.progress",
                        "job_id": self.job_id,
                    }
                    if out_time_ms is not None:
                        payload["out_time_ms"] = out_time_ms
                    if speed:
                        payload["speed"] = speed
                    payload["phase"] = phase
                    self.client._send_safe(payload)
        return process.wait()

    @staticmethod
    def _out_time_ms(values: dict[str, str]) -> int | None:
        text = values.get("out_time_us") or values.get("out_time_ms")
        if text is None:
            micros_text = values.get("out_time")
            if micros_text is None:
                return None
            try:
                return int(float(micros_text) * 1000)
            except ValueError:
                return None
        try:
            # out_time_us 是微秒；out_time_ms 名不符实也是微秒（ffmpeg 历史）
            return int(text) // 1000
        except ValueError:
            return None


class RealtimeClient:
    """实时远程转码的 WS 控制连接：重连、心跳、任务分派。

    状态消息（accepted/progress/finished/…）经 ``_outbox`` 交给持有当前
    WebSocket 的读循环线程发送——任务线程不直接碰 socket，避免发到已被
    重连替换的旧连接上。连接状态（connecting / online / offline）经 ``status``
    回调交出去，GUI 据此显示真实的连接情况。
    """

    def __init__(
        self,
        *,
        server: str,
        token: str,
        worker_id: str,
        ffmpeg: str,
        caps: dict,
        max_jobs: int = 1,
        log: Callable[[str], None] | None = None,
        preload: Callable[[dict], bool] | None = None,
        status: Callable[..., None] | None = None,
    ) -> None:
        self.server = server.rstrip("/")
        self.token = token
        self.worker_id = worker_id
        self.ffmpeg = ffmpeg
        self.caps = caps
        self.max_jobs = max_jobs
        self.log = log or (lambda _msg: None)
        self.status = status or (lambda _kind, **_fields: None)
        # 预转码执行器（worker.process_task 的包装）：None=本客户端
        # 只报 realtime 角色，收到 class=preload 的 job.start 直接拒掉
        self.preload = preload
        self.jobs: dict[str, RealtimeJob] = {}
        self.jobs_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.last_problem: str | None = None
        self._outbox: list[dict] = []
        self._outbox_lock = threading.Lock()
        self._current_ws: _WebSocket | None = None

    # -- 主循环 -------------------------------------------------------------

    def run_forever(self) -> None:
        backoff = 2.0
        while not self.stop_event.is_set():
            started = time.time()
            try:
                self._connect_once()
            except OSError as exc:
                self.last_problem = str(exc)
                self.log(f"实时连接断开：{exc}")
                if isinstance(exc, ServerClosed) and exc.code == 1008:
                    # 服务端明确拒绝：按理由定间隔，不走退避
                    wait = _REJECT_RETRY_S[rejection_kind(exc.reason)]
                elif time.time() - started < 30:
                    # 刚连上就断的按退避等，长连过的立即重连
                    wait, backoff = backoff, min(backoff * 2, 30.0)
                else:
                    wait = 0.0
                self.status("offline", reason=self.last_problem, retry_seconds=wait)
                self.stop_event.wait(wait)
            else:
                backoff = 2.0

    def stop(self) -> None:
        self.stop_event.set()
        with self.jobs_lock:
            for job in list(self.jobs.values()):
                job.stop(force=True)

    def _connect_once(self) -> None:
        parsed = urllib.parse.urlsplit(self.server)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        path = parsed.path.rstrip("/") + "/api/v1/transcode-worker/ws"
        self.status("connecting", server=self.server)
        ws = _WebSocket(host, port, path, self.token)
        self.log("实时通道已连上，握手中…")
        last_seen_box = [time.time()]
        self._heartbeat(ws, last_seen_box)
        try:
            hello = {
                "type": "worker.hello",
                "protocol_version": PROTOCOL_VERSION,
                "worker_version": "win-worker-2",
                "worker_id": self.worker_id,
                "draining": False,
                "capabilities": {
                    "platform": "windows",
                    "arch": os.environ.get("PROCESSOR_ARCHITECTURE", "x86_64").lower(),
                    "ffmpeg_version": self.caps.get("ffmpeg_version"),
                    "encoders": self.caps.get("encoders", []),
                    "backends": ["nvenc"],
                    "max_jobs": self.max_jobs,
                    "roles": ["realtime", "preload"] if self.preload else ["realtime"],
                    "server_config": True,
                    "hardware": self.caps.get("hardware", {}),
                    "segment_types": ["fmp4"],
                    # 本程序没有面板消费 job.playback；申报 true 会让服务端
                    # 每 3 秒白发一条播放位置
                    "playback_progress": False,
                    "disc_sources": False,
                    "hw_decoders": self.caps.get("hw_decoders", []),
                    "filters": self.caps.get("filters", []),
                    "progressive_segments": True,
                    "read_options": [],
                },
            }
            ws.send_json(hello)
            self._current_ws = ws
            while not self.stop_event.is_set():
                self._flush_outbox(ws)
                # 短超时空转：任务线程投进 outbox 的状态消息最长要等一个超时
                # 周期才发出去，而 job.accepted 服务端开会话时是**阻塞等待**
                # 的——这里等多久，用户起播的「正在判断播放方式」就白转多久
                # （2026-10-16 NAS 实测：5s 超时下远程首播稳定多转约 5 秒，
                # 全部耗在派单→接单回执之间）。0.5s 让接单回执与进度及时出户；
                # 判断半开连接靠下面的静默窗口与心跳线程，不靠这个值
                message = ws.recv_json(timeout=0.5)
                if message is None:
                    if time.time() - last_seen_box[0] > SERVER_SILENCE_SECONDS:
                        raise OSError(f"{int(SERVER_SILENCE_SECONDS)} 秒没有服务端消息")
                    continue
                last_seen_box[0] = time.time()
                self._handle(ws, message)
        finally:
            with self._outbox_lock:
                # 换连接时丢掉旧 in-flight 状态：终态未达 NAS 会话层自会清理
                self._outbox.clear()
            self._current_ws = None
            ws.close()
            # 断线后服务端已把这些任务判失败重派；本地立即收掉 ffmpeg 释放
            # 并发位——旧进程占着 self.jobs 会把重派回来的新任务顶成
            # 「Worker 并发已满」，播放直接失败
            with self.jobs_lock:
                orphaned = list(self.jobs.values())
                self.jobs.clear()
            for job in orphaned:
                job.stop(force=True)

    def _flush_outbox(self, ws: _WebSocket) -> None:
        while True:
            with self._outbox_lock:
                if not self._outbox:
                    return
                payload = self._outbox.pop(0)
            try:
                ws.send_json(payload)
            except OSError:
                with self._outbox_lock:
                    self._outbox.insert(0, payload)
                raise

    def _handle(self, ws: _WebSocket, message: dict) -> None:
        message_type = str(message.get("type", ""))
        if message_type == "worker.accepted":
            limit = message.get("max_jobs")
            # 服务端按 hello 申报的原值回传（上限 8 与 GUI 旋钮一致）；1..4
            # 的旧钳制会把并发 5-8 的首轮实时任务顶成「并发已满」拒单
            if isinstance(limit, int) and 1 <= limit <= 8:
                self.max_jobs = limit
            self.last_problem = None
            self.log("实时通道握手完成，可以接单")
            self.status("online", server=self.server)
        elif message_type == "job.start":
            # v2 统一调度：同一连接里实时与预转码任务按 class 分流
            if message.get("class") == "preload":
                threading.Thread(
                    target=self._start_preload, args=(message,), name="preload-start", daemon=True
                ).start()
            else:
                threading.Thread(
                    target=self._start_job, args=(ws, message), name="job-start", daemon=True
                ).start()
        elif message_type == "job.stop":
            job_id = str(message.get("job_id", ""))
            force = message.get("force") is True
            if message.get("class") == "preload":
                self._stop_preload(message)
                return
            with self.jobs_lock:
                job = self.jobs.pop(job_id, None)
            if job is not None:
                # force=True 是拖进度条重启（马上有新单接上）；不透传协议细节
                suffix = "（拖动进度，重启）" if force else ""
                self.log(f"停止播放转码 {job_id[-6:]}{suffix}")
                job.stop(force)
        elif message_type == "job.pause":
            with self.jobs_lock:
                job = self.jobs.get(str(message.get("job_id", "")))
            if job is not None:
                job.pause()
        elif message_type == "job.resume":
            with self.jobs_lock:
                job = self.jobs.get(str(message.get("job_id", "")))
            if job is not None:
                job.resume()
        elif message_type in ("worker.heartbeat.ack", "job.playback"):
            # job.playback 是观众播放位置（服务端只发给声明了 playback_progress
            # 的 Worker）；本 Worker 无面板，hello 已不再申报，收到的都是旧服务端
            pass
        elif message_type == "worker.config":
            # v2 起服务端不再下发并发上限；收到说明对端还是旧版，忽略即可
            self.log("服务端仍按 v1 下发并发上限，已忽略（并发以本机配置为准）")
        else:
            self.log(f"忽略未知消息：{message_type}")

    def _heartbeat(self, ws: _WebSocket, last_seen_box: list[float]) -> None:
        def loop() -> None:
            while not self.stop_event.is_set():
                time.sleep(HEARTBEAT_SECONDS)
                if time.time() - last_seen_box[0] > SERVER_SILENCE_SECONDS:
                    self.log("服务端长期无响应，断开重连")
                    ws.close()
                    return
                try:
                    ws.send_json({"type": "worker.heartbeat", "load": {}})
                except OSError:
                    return

        threading.Thread(target=loop, name="rt-heartbeat", daemon=True).start()

    def _start_job(self, ws: _WebSocket, message: dict) -> None:
        job_id = str(message.get("job_id") or "")
        if not job_id:
            return
        # 连接已被替换/断开（finally 清 jobs 与这里的注册存在竞态）：不再
        # 接单，服务端会在断线后把任务判失败重派
        if self._current_ws is not ws or self.stop_event.is_set():
            return
        attempt_id = str(message.get("attempt_id") or job_id)
        with self.jobs_lock:
            existing = self.jobs.get(job_id)
        if existing is not None:
            if existing.attempt_id == attempt_id:
                self._send_safe(
                    {"type": "job.accepted", "job_id": job_id, "attempt_id": attempt_id}
                )
                return
            existing.stop(force=True)
            with self.jobs_lock:
                self.jobs.pop(job_id, None)
        if len(self.jobs) >= self.max_jobs:
            self._send_safe(
                {
                    "type": "job.failed",
                    "job_id": job_id,
                    "attempt_id": attempt_id,
                    "error": "Worker 并发已满",
                }
            )
            return
        self.log(f"接单 {job_id[-6:]}：{message.get('display_name') or '（未命名）'}")
        job = RealtimeJob(job_id, message, self.ffmpeg, self)
        with self.jobs_lock:
            self.jobs[job_id] = job
        # 播放优先：实时任务一进来，本机在跑的预转码全部让位（断点保留，服务端当场回队）
        yield_slots = getattr(self, "preload_yield", None)
        if yield_slots is not None:
            yield_slots(0)
        threading.Thread(
            target=job.run, args=(message,), name=f"job-{job_id[-6:]}", daemon=True
        ).start()

    def _send_safe(self, payload: dict) -> None:
        """任务线程投递状态消息；由主循环发到当前连接。"""
        with self._outbox_lock:
            self._outbox.append(payload)

    # -- 预转码任务（v2 统一调度的 class=preload 分支）-----------------------

    def _start_preload(self, message: dict) -> None:
        """执行一个推送来的预转码任务（分段转码 + REST 数据面）。"""
        task_id = message.get("task_id")
        display = message.get("display_name") or f"#{task_id}"
        if self.preload is None:
            # 没报 preload 角色却被派了单（服务端bug 或旧版服务端）：明确拒绝
            self._send_safe(
                {
                    "type": "job.failed",
                    "job_id": f"preload-{task_id}",
                    "class": "preload",
                    "task_id": task_id,
                    "error": "本 Worker 未开启预转码角色",
                }
            )
            return
        self.log(f"接预转码单 {task_id}：{display}")
        try:
            ok = self.preload(message)
        except Exception as exc:  # noqa: BLE001
            self.log(f"[{task_id}] 预转码异常：{exc}")
            ok = False
        self.log(f"预转码单 {task_id} {'完成' if ok else '失败/中断'}：{display}")

    def _stop_preload(self, message: dict) -> None:
        """服务端取消预转码任务：停掉对应任务的分段 ffmpeg，并删掉本地半成品。"""
        task_id = message.get("task_id")
        stopper = getattr(self, "preload_stop", None)
        if stopper is not None and task_id is not None:
            cancel = message.get("reason") == "cancel"
            self.log(f"预转码 {task_id} 停止（{'取消，删除本地半成品' if cancel else '断点保留'}）")
            stopper(task_id, cancel=cancel)

    def send_preload_yielded(self, task_id) -> None:
        """本机为播放让位停掉了预转码：告诉服务端，它当场退回队列（断点台账保留）。"""
        self._send_safe({"type": "preload.yielded", "task_id": task_id})
