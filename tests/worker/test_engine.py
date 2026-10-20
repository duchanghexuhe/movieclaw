"""实时转码 Worker 的单元测试：分片器切段规则 + 产物白名单 + 进度解析。

分片化 MP4 的测试样本用 ffmpeg 现造（机器有 ffmpeg 时）；切段规则断言与
Mac 端 ProgressiveSegments.swift 的语义一致（设计文档 transcode-latency.md §5）。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from engine import (
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


class _FakeOpener:
    """_NO_PROXY_OPENER 的替身：把 open() 转给 fake_urlopen。"""

    def __init__(self, fn) -> None:
        self._fn = fn

    def open(self, request, timeout=0):  # noqa: ARG002
        return self._fn(request)


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

    import engine as rw

    monkeypatch.setattr(rw, "_NO_PROXY_OPENER", _FakeOpener(fake_urlopen))
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


def test_hello_declares_roles_by_preload_presence():
    """v2 统一调度：带 preload 执行器的客户端报 realtime+preload，纯实时只报 realtime。"""
    from engine import RealtimeClient

    pure = RealtimeClient(
        server="http://nas", token="t", worker_id="w1", ffmpeg="ffmpeg", caps={}
    )
    both = RealtimeClient(
        server="http://nas", token="t", worker_id="w2", ffmpeg="ffmpeg", caps={},
        preload=lambda message: True,
    )
    # hello 的 capabilities 由 _connect_once 构造；这里直接验证判定源
    assert pure.preload is None
    assert both.preload is not None


def test_preload_job_start_without_executor_is_rejected():
    """没报 preload 角色却收到 class=preload 的任务：明确上报失败，而不是误执行。"""
    from engine import RealtimeClient

    client = RealtimeClient(
        server="http://nas", token="t", worker_id="w1", ffmpeg="ffmpeg", caps={}
    )
    client._start_preload({"task_id": 7, "display_name": "x.mkv"})
    rejected = [m for m in client._outbox if m.get("class") == "preload"]
    assert rejected and rejected[0]["type"] == "job.failed"
    assert rejected[0]["task_id"] == 7


def test_preload_job_start_runs_executor():
    """带执行器时 class=preload 任务被交给执行器并回传结果。"""
    from engine import RealtimeClient

    ran: list[dict] = []
    client = RealtimeClient(
        server="http://nas", token="t", worker_id="w1", ffmpeg="ffmpeg", caps={},
        preload=lambda message: ran.append(message) or True,
    )
    client._start_preload({"task_id": 9, "display_name": "y.mkv"})
    assert ran and ran[0]["task_id"] == 9


def test_preload_stop_routes_to_registered_stopper():
    """job.stop(class=preload) 只停对应 task_id 的预转码，不碰实时任务表。

    reason=cancel 才告诉执行器要删本地半成品；没有 reason 的（让位类）原样保留。
    """
    from engine import RealtimeClient

    client = RealtimeClient(
        server="http://nas", token="t", worker_id="w1", ffmpeg="ffmpeg", caps={},
        preload=lambda message: True,
    )
    stopped: list[tuple[object, bool]] = []
    client.preload_stop = lambda task_id, cancel=False: stopped.append((task_id, cancel))
    client._handle(None, {"type": "job.stop", "class": "preload", "task_id": 42})
    client._handle(
        None, {"type": "job.stop", "class": "preload", "task_id": 43, "reason": "cancel"}
    )
    assert stopped == [(42, False), (43, True)]


def test_realtime_start_yields_every_preload_slot():
    """播放优先：实时任务一进来，本机在跑的预转码全部让位（keep=0）。"""
    from engine import RealtimeClient

    client = RealtimeClient(
        server="http://nas", token="t", worker_id="w1", ffmpeg="ffmpeg", caps={},
        preload=lambda message: True,
    )
    keeps: list[int] = []
    client.preload_yield = keeps.append
    ws = object()
    client._current_ws = ws
    # 无 ffmpeg_args：job 线程立刻以「任务缺少 ffmpeg_args」收尾，只关心让位钩子
    client._start_job(ws, {"job_id": "rt-1", "attempt_id": "a1"})
    assert keeps == [0]


def test_send_preload_yielded_queues_the_message():
    """让位要告诉服务端：task_id 随 preload.yielded 上行（服务端据此当场回队）。"""
    from engine import RealtimeClient

    client = RealtimeClient(
        server="http://nas", token="t", worker_id="w1", ffmpeg="ffmpeg", caps={},
        preload=lambda message: True,
    )
    client.send_preload_yielded(7)
    assert client._outbox == [{"type": "preload.yielded", "task_id": 7}]


def test_recv_json_timeout_is_not_disconnect():
    """帧边界读超时=没消息：返回 None 让主循环继续等，不当断线重连。

    回归锚点：服务端安静 5 秒（心跳间隔 15 秒必出现）Worker 自断重连，
    NAS 侧表现为每隔几秒「上线→离线」循环、断开码 1005。
    """
    import json
    import socket as _socket

    import engine as rw

    a, b = _socket.socketpair()
    try:
        ws = rw._WebSocket.__new__(rw._WebSocket)
        ws._sock = a
        ws._closed = False
        ws._lock = threading.Lock()
        ws._buffer = b""  # 握手残留缓冲（_recv_exact 先吃它再读 socket）

        # 空转超时：不抛异常，返回 None
        assert ws.recv_json(timeout=0.2) is None
        assert ws.recv_json(timeout=0.2) is None

        # 对端发来文本帧（服务端→客户端不掩码）：正常解析成 dict
        payload = json.dumps({"type": "x"}).encode()
        b.sendall(bytes([0x81, len(payload)]) + payload)
        assert ws.recv_json(timeout=1.0) == {"type": "x"}

        # 对端发关闭帧：必须抛 OSError——这才是真断线
        b.sendall(bytes([0x88, 0x00]))
        with pytest.raises(OSError):
            ws.recv_json(timeout=1.0)
    finally:
        a.close()
        b.close()


def _client_ws(sock):
    """只带 socket 的 _WebSocket 替身（同 test_recv_json_timeout_is_not_disconnect 的构造）。"""
    import engine as rw

    ws = rw._WebSocket.__new__(rw._WebSocket)
    ws._sock = sock
    ws._closed = False
    ws._lock = threading.Lock()
    ws._buffer = b""
    return ws


def test_server_close_frame_carries_code_and_reason():
    """服务端带理由关闭（1008）：拿到关闭码与理由，回一个关闭帧，再真正断开 socket。

    回归锚点：曾经只抛 OSError("服务端关闭了连接")，服务端「尚未启用远程转码」之类的理由
    整句丢掉，NAS 日志与 Worker 界面都看不出为什么连不上。
    """
    import socket as _socket

    import engine as rw

    a, b = _socket.socketpair()
    try:
        ws = _client_ws(a)
        reason = "服务端尚未启用远程转码，请在网页设置「转码」页打开远程转码开关"
        payload = (1008).to_bytes(2, "big") + reason.encode()
        b.sendall(bytes([0x88, len(payload)]) + payload)  # 服务端→客户端不掩码
        with pytest.raises(rw.ServerClosed) as caught:
            ws.recv_json(timeout=1.0)
        assert caught.value.code == 1008
        assert caught.value.reason == reason
        assert "1008" in str(caught.value) and reason in str(caught.value)
        # 回的关闭帧：客户端→服务端必须掩码（opcode 0x8，掩码位 0x80）
        echoed = b.recv(16)
        assert echoed[0] == 0x88 and echoed[1] & 0x80
        # 不是只标记：socket 真的关了，对端读到 EOF
        assert b.recv(16) == b""
    finally:
        a.close()
        b.close()


@pytest.mark.parametrize(
    ("reason", "kind"),
    [
        ("凭证无效或已被吊销，请在网页「设置 → 设备」重新配对", "auth"),
        ("转码器凭证已被注销，请重新配对", "auth"),
        ("服务端尚未启用远程转码，请在网页设置「转码」页打开远程转码开关", "remote_disabled"),
        ("Worker 协议版本（1）与服务端（2）不一致，请把 Worker 与服务端更新到同一版本", "other"),
        ("", "other"),
    ],
)
def test_rejection_reason_classification(reason, kind):
    """1008 理由的三类划分与 Mac 转码器的 NASRejection 一致（改服务端文案两边一起看）。"""
    from engine import rejection_kind

    assert rejection_kind(reason) == kind


@pytest.mark.parametrize(
    ("reason", "retry_seconds", "rejection"),
    [
        ("凭证无效或已被吊销，请在网页「设置 → 设备」重新配对", 300.0, "auth"),
        ("服务端尚未启用远程转码，请在网页设置「转码」页打开远程转码开关", 60.0, "remote_disabled"),
    ],
)
def test_run_forever_reports_rejection_and_waits_by_class(reason, retry_seconds, rejection):
    """服务端 1008 拒绝：状态事件带真实理由与分类，重试间隔按分类定（不走指数退避）。"""
    from engine import RealtimeClient, ServerClosed

    client = RealtimeClient(
        server="http://nas", token="t", worker_id="w1", ffmpeg="ffmpeg", caps={}
    )
    events: list[tuple[str, dict]] = []

    def status(kind, **fields):
        events.append((kind, fields))
        client.stop()  # 只看一轮：stop 之后 wait 立即返回，run_forever 随即退出

    def refuse():
        raise ServerClosed(1008, reason)

    client.status = status
    client._connect_once = refuse
    client.run_forever()
    assert events == [
        (
            "offline",
            {
                "reason": f"服务端关闭了连接（1008）：{reason}",
                "retry_seconds": retry_seconds,
                "rejection": rejection,
            },
        )
    ]


def test_connect_reports_connecting_then_online(monkeypatch):
    """连接状态来自真实握手：发起连接时报 connecting，收到 worker.accepted 才报 online。"""
    import engine as rw
    from engine import RealtimeClient

    sent: list[dict] = []

    class FakeSocket:
        def __init__(self, host, port, path, token, timeout=10.0):
            self.inbox = [{"type": "worker.accepted", "max_jobs": 1}]

        def send_json(self, payload):
            sent.append(payload)

        def recv_json(self, timeout):
            return self.inbox.pop(0) if self.inbox else None

        def close(self):
            pass

    monkeypatch.setattr(rw, "_WebSocket", FakeSocket)
    client = RealtimeClient(
        server="http://nas:3000", token="t", worker_id="DESKTOP-1", ffmpeg="ffmpeg", caps={}
    )
    events: list[tuple[str, dict]] = []

    def status(kind, **fields):
        events.append((kind, fields))
        if kind == "online":
            client.stop()

    client.status = status
    client._connect_once()
    assert [kind for kind, _ in events] == ["connecting", "online"]
    assert events[1][1] == {"server": "http://nas:3000"}
    # hello 用的就是 runtime 交进来的 worker_id（服务端据此登记，不再是带空格的显示名）
    assert sent[0]["type"] == "worker.hello" and sent[0]["worker_id"] == "DESKTOP-1"


def test_job_work_dir_falls_back_when_ffmpeg_cannot_write(monkeypatch, tmp_path):
    """默认临时目录对 ffmpeg 不可写时改用下一候选（2026-10-20 真机事故）。

    ffmpeg 二进制带 Low 完整性标签时写不进 Medium 的 %TEMP%，本进程却写得进——
    判定必须由 ffmpeg 自己跑一次来出，否则任务会被派到一个必失败的中转目录。
    """
    import engine as rw

    blocked = tmp_path / "blocked"
    usable = tmp_path / "usable"
    blocked.mkdir()
    user = tmp_path / "user-home"
    user.mkdir()
    monkeypatch.setenv("MOVIECLAW_WORKER_WORK_DIR", str(blocked))
    monkeypatch.setattr(rw, "_WORK_DIR_CACHE", {})
    monkeypatch.setattr(rw.Path, "home", staticmethod(lambda: user))
    monkeypatch.setattr(rw, "_work_dir_candidates", lambda: [blocked, usable])

    def can_write(ffmpeg, directory):
        return directory == usable

    monkeypatch.setattr(rw, "_ffmpeg_can_write", can_write)

    job_id = "01ABCDEFGHJKMNPQRSTVWXYZ12"
    picked = rw._job_work_dir(job_id, "ffmpeg")
    assert picked.parent == usable
    # 子目录名保持旧口径（job_id 末 8 位），排障文档里的路径指引不用改
    assert picked.name == f"mc-realtime-{job_id[-8:]}"
    picked.mkdir(parents=True, exist_ok=True)
    assert picked.is_dir()


def test_job_work_dir_prefers_first_writable_candidate(monkeypatch, tmp_path):
    """第一个写得进去的就用（默认路径不变），并且按 ffmpeg 分键缓存。"""
    import engine as rw

    first = tmp_path / "temp"
    second = tmp_path / "worker"
    monkeypatch.setattr(rw, "_WORK_DIR_CACHE", {})
    monkeypatch.setattr(rw, "_work_dir_candidates", lambda: [first, second])
    probes: list[tuple[str, str]] = []

    def can_write(ffmpeg, directory):
        probes.append((ffmpeg, str(directory)))
        return True

    monkeypatch.setattr(rw, "_ffmpeg_can_write", can_write)

    a = rw._job_work_dir("01JOBAAAAAAAAAAAAAAAAAAAA1", "ffmpeg-a")
    b = rw._job_work_dir("01JOBBBBBBBBBBBBBBBBBBBB2", "ffmpeg-a")
    assert a.parent == first and b.parent == first
    # 同一份 ffmpeg 只探一次，任务之间复用结论
    assert probes == [("ffmpeg-a", str(first))]

    # 换 ffmpeg（换二进制，标签可能不同）要重探，而不是沿用上一份的结论
    rw._job_work_dir("01JOBCCCCCCCCCCCCCCCCCCCC3", "ffmpeg-b")
    assert [probe[0] for probe in probes].count("ffmpeg-b") == 1


def test_job_work_dir_keeps_temp_path_when_nothing_writable(monkeypatch, tmp_path):
    """全部候选都写不进去：回落旧路径，让 ffmpeg 自己报 Permission denied。"""
    import engine as rw

    monkeypatch.setattr(rw, "_WORK_DIR_CACHE", {})
    monkeypatch.setattr(rw, "_work_dir_candidates", lambda: [tmp_path / "a", tmp_path / "b"])
    monkeypatch.setattr(rw, "_ffmpeg_can_write", lambda ffmpeg, directory: False)
    monkeypatch.setenv("TEMP", str(tmp_path / "temp-root"))

    picked = rw._job_work_dir("01JOBDDDDDDDDDDDDDDDDDDDD4", "ffmpeg")
    assert picked.parent == tmp_path / "temp-root"
