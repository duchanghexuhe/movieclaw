"""实时转码 Worker 的单元测试：分片器切段规则 + 产物白名单 + 进度解析。

分片化 MP4 的测试样本用 ffmpeg 现造（机器有 ffmpeg 时）；切段规则断言与
Mac 端 ProgressiveSegments.swift 的语义一致（设计文档 transcode-latency.md §5）。
"""

from __future__ import annotations

import shutil
import struct
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from realtime_worker import (
    ArtifactUploadProxy,
    FragmentOutput,
    FragmentSegmenter,
    RealtimeJob,
    SourceReadProxy,
    _allowed_artifact_name,
)


class _FakeResponse:
    """urlopen 的替身：按 Range 头返回 8MiB，并记录每次请求的块号。"""

    def __init__(self, calls: list[int], block: int) -> None:
        self._calls = calls
        self._block = block
        self.headers = {
            "Content-Range": f"bytes 0-0/{8 * 1024 * 1024 * 64}",
            "Content-Length": str(8 * 1024 * 1024),
        }

    def read(self) -> bytes:
        self._calls.append(self._block)
        return b"\0" * (8 * 1024 * 1024)

    def __enter__(self):  # noqa: D105
        return self

    def __exit__(self, *args):  # noqa: D105
        return False


def test_source_proxy_prefetch_does_not_cascade(monkeypatch):
    """预取绝不级联：按需取一块 → 只预取后两块，预取自身不再派生。

    曾经预取成功也再派生两块，链式翻倍往前冲 + FIFO 缓存被冲垮，深 seek
    首帧被拖到 40 秒开外，播放器等不到分片直接降档（2026-10-15 真机）。
    """
    calls: list[int] = []

    def fake_urlopen(request, timeout=0):  # noqa: ARG001
        range_header = request.headers.get("Range", "")
        block = int(range_header.split("=")[1].split("-")[0]) // (8 * 1024 * 1024)
        return _FakeResponse(calls, block)

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    proxy = SourceReadProxy("job-x", "http://upstream/api/v1/transcode-worker/sessions/s/source?token=t")
    assert proxy._block(0) is not None
    # 等预取线程跑完：块 0 按需 + 块 1/2 预取，且数字稳定（不继续增长）
    import time

    for _ in range(20):
        time.sleep(0.1)
        if len(calls) >= 3:
            break
    time.sleep(0.5)
    assert sorted(set(calls)) == [0, 1, 2]
    stable = len(calls)
    time.sleep(0.5)
    assert len(calls) == stable, "预取还在继续派生——级联没堵住"


def _make_fragmented_mp4(tmp_path: Path, seconds: int = 10) -> bytes:
    """用 ffmpeg 现造一条分片化 MP4（与服务端 _progressive_args 同 movflags）。"""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("需要 ffmpeg")
    out = tmp_path / "frag.mp4"
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc2=duration={seconds}:size=320x180:rate=10",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-g", "10",
         "-movflags", "+frag_keyframe+delay_moov+default_base_moof+frag_discont+skip_sidx",
         "-frag_duration", "500000",  # 0.5s
         str(out)],
        check=True, timeout=120,
    )
    return out.read_bytes()


def _segment(data: bytes, *, segment_seconds: float = 4.0, start_segment: int | None = None):
    segmenter = FragmentSegmenter(segment_seconds, start_segment)
    # 按 64 字节碎块喂入，模拟网络流式到达
    outputs: list[FragmentOutput] = []
    for offset in range(0, len(data), 64):
        outputs.extend(segmenter.append(data[offset : offset + 64]))
    outputs.extend(segmenter.append(b""))
    return segmenter, outputs


def test_segmenter_emits_init_and_grid_segments(tmp_path):
    """init 恰好一个；4 秒栅格切段：10 秒片从 0 号起转是 0,1,2 三段。"""
    data = _make_fragmented_mp4(tmp_path)
    segmenter, outputs = _segment(data)
    assert segmenter.failure is None
    inits = [o for o in outputs if o.kind == "init"]
    assert len(inits) == 1 and inits[0].data.startswith(b"\x00\x00\x00\x1cftyp")
    frags = [o for o in outputs if o.kind == "fragment"]
    assert frags, "没有任何片段产出"
    # 段号从起转号开始连续递增；缺省起转时按首帧时间取整（=0）
    starts = [o.segment for o in frags if o.starts_segment]
    assert starts == sorted(starts)
    assert starts[0] == 0
    assert set(o.segment for o in frags) == set(range(starts[0], starts[-1] + 1))
    # 每个片段的数据非空；最后一段可能不满 4 秒
    assert all(o.data for o in frags)


def test_segmenter_start_segment_offsets_numbering(tmp_path):
    """start_segment=37：段号从 37 起转（VOD 续转场景）。"""
    data = _make_fragmented_mp4(tmp_path, seconds=6)
    segmenter, outputs = _segment(data, start_segment=37)
    assert segmenter.failure is None
    frags = [o for o in outputs if o.kind == "fragment"]
    assert frags and frags[0].segment == 37 and frags[0].starts_segment
    assert set(o.segment for o in frags) == {37, 38}


def test_segmenter_rejects_corrupt_box():
    """长度不合理（<8）的盒子：置 failure 并清空缓冲，不再产出。"""
    segmenter = FragmentSegmenter(4.0)
    outputs = segmenter.append(b"\x00\x00\x00\x02moov")
    assert outputs == []
    assert segmenter.failure is not None


def test_artifact_name_whitelist_matches_server():
    """白名单必须与 NAS 端 _ARTIFACT_NAME 同批（issue #444：TS 分片被本机拒收）。"""
    assert _allowed_artifact_name("init.mp4")
    assert _allowed_artifact_name("stream.mp4")
    assert _allowed_artifact_name("live.m3u8")
    assert _allowed_artifact_name("index.m3u8")
    assert _allowed_artifact_name("seg00042.m4s")
    assert _allowed_artifact_name("seg00042.ts")
    assert not _allowed_artifact_name("seg42.m4s")
    assert not _allowed_artifact_name("../../etc/passwd")
    assert not _allowed_artifact_name("evil.mp4")


def test_artifact_base_url_extraction():
    """从 ffmpeg 参数里定位产物基址与 token。"""
    arguments = [
        "-i", "http://nas:3000/api/v1/transcode-worker/sessions/s1/source?token=a",
        "-f", "mp4",
        "http://nas:3000/api/v1/transcode-worker/sessions/s1/artifacts/stream.mp4?token=b",
    ]
    found = ArtifactUploadProxy.remote_artifact_base_url(arguments)
    assert found is not None
    base, query = found
    assert base == "http://nas:3000/api/v1/transcode-worker/sessions/s1/artifacts"
    assert query == "token=b"


def test_out_time_parsing():
    """-progress 行的 out_time_us/out_time_ms 都按微秒解读成毫秒。"""
    assert RealtimeJob._out_time_ms({"out_time_us": "1234567"}) == 1234
    assert RealtimeJob._out_time_ms({"out_time_ms": "1234567"}) == 1234
    assert RealtimeJob._out_time_ms({"out_time": "1.5"}) == 1500
    assert RealtimeJob._out_time_ms({}) is None


class _StubClient:
    """_start_job 注册表的最小客户端替身：捕捉上行消息与槽位表。"""

    def __init__(self) -> None:
        self.jobs: dict[str, RealtimeJob] = {}
        self.jobs_lock = threading.Lock()
        self._outbox: list[dict] = []
        self._outbox_lock = threading.Lock()
        self.sent: list[dict] = []

    def _send_safe(self, payload: dict) -> None:
        self.sent.append(payload)


def _run_job_and_release(job_id: str, exit_code: int) -> _StubClient:
    """跑一轮立刻退出的"ffmpeg"（用 python 进程顶替），返回捕获到的客户端。"""
    client = _StubClient()
    message = {"ffmpeg_args": ["-c", f"import sys; sys.exit({exit_code})"]}
    job = RealtimeJob(job_id, message, sys.executable, client)
    with client.jobs_lock:
        client.jobs[job_id] = job  # 模拟 _start_job 的注册
    job.run(message)
    return client


def test_job_releases_concurrency_slot_on_finish():
    """ffmpeg 正常结束必须释放并发槽位：条目原本只在 NAS 显式 job.stop 或同
    job 重派时移除，自然结束的条目一直占着表，之后所有接单都被
    "Worker 并发已满"挡住（2026-10-15 真机：两个失败任务占死并发 5 分钟）。"""
    client = _run_job_and_release("job-ok", 0)
    assert "job-ok" not in client.jobs
    assert any(m["type"] == "job.finished" for m in client.sent)


def test_job_releases_concurrency_slot_on_failure():
    """失败同样释放：失败是最常见的槽位泄漏现场（ffmpeg 起不来→任务失败→
    槽位占死→用户重试全被拒）。"""
    client = _run_job_and_release("job-bad", 3)
    assert "job-bad" not in client.jobs
    failed = [m for m in client.sent if m["type"] == "job.failed"]
    assert failed and "退出码 3" in failed[0]["error"]
