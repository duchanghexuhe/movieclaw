"""WorkerRuntime 的最小回归：进度台账（GUI 状态栏）、worker_id、配对事件、睡眠占用、启动校验。"""

from __future__ import annotations

import shutil
import sys
import threading
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "worker"))

import worker as worker_mod  # noqa: E402
from worker import WorkerRuntime  # noqa: E402


def test_progress_view_returns_display_and_progress_snapshot():
    rt = WorkerRuntime(server="http://nas", token="t", ffmpeg="ffmpeg")
    rt.tasks[7] = "a.mkv"
    rt._progress[7] = {"phase": "transcoding", "segment_index": 3, "segment_total": 12}
    rt.tasks[9] = "b.mkv"
    view = rt.progress_view()
    assert [name for name, _ in view] == ["a.mkv", "b.mkv"]
    assert view[0][1] == {"phase": "transcoding", "segment_index": 3, "segment_total": 12}
    # 未登记进度的任务（刚接单还没进 process_task）给空 dict，GUI 显示「准备中」
    assert view[1][1] == {}
    # 快照语义：之后原 dict 再变，已返回的视图不受影响
    rt._progress[7]["segment_index"] = 4
    assert view[0][1]["segment_index"] == 3


def test_run_preload_wires_progress_dict_and_clears_it(monkeypatch):
    rt = WorkerRuntime(server="http://nas", token="t", ffmpeg="ffmpeg")
    rt.api = object()  # 假执行器不触网，仅过 _run_preload 的断言
    seen: dict = {}

    def fake_process_task(api, ffmpeg, task, workdir, *, stop=None, register=None,
                          progress=None):
        seen["progress"] = progress
        progress["phase"] = "transcoding"
        return True

    monkeypatch.setattr(worker_mod, "process_task", fake_process_task)
    ok = rt._run_preload({"task_id": 7, "display_name": "a.mkv"})
    assert ok is True
    # process_task 拿到的就是台账 dict 本体（原地更新，不是副本）
    assert seen["progress"]["phase"] == "transcoding"
    # 收尾摘除：任务结束后状态栏不再显示
    assert 7 not in rt.tasks and 7 not in rt._progress
    assert rt.progress_view() == []


def test_default_worker_id_is_hostname_based_and_server_valid(monkeypatch):
    """默认 worker_id 取主机名并净化成服务端认的样子；带空格的旧默认名会被 1008 拒收、无限重连。"""
    from movieclaw_api.services.playback.remote_worker import _WORKER_ID

    monkeypatch.setattr(worker_mod.socket, "gethostname", lambda: "My Win PC 2")
    rt = WorkerRuntime(server="http://nas", token="t", ffmpeg="ffmpeg")
    assert rt.worker_id == "My-Win-PC-2"
    assert _WORKER_ID.fullmatch(rt.worker_id)
    # 配对显示名保持人类可读，不拿来当 id
    assert rt.name == "Preload Worker（My Win PC 2）"


def test_configured_name_keeps_worker_id(monkeypatch):
    """配置里填过 name 的部署照旧：id 不变（设备记录不断），非法字符才换成 "-"。"""
    from movieclaw_api.services.playback.remote_worker import _WORKER_ID

    assert worker_mod.worker_id_for("windows-preload-worker") == "windows-preload-worker"
    assert worker_mod.worker_id_for("Preload Worker") == "Preload-Worker"
    assert _WORKER_ID.fullmatch(worker_mod.worker_id_for("  --Preload Worker!!  "))
    rt = WorkerRuntime(
        server="http://nas", token="t", ffmpeg="ffmpeg", name="windows-preload-worker"
    )
    assert rt.worker_id == "windows-preload-worker" and rt.name == "windows-preload-worker"


def test_worker_id_falls_back_to_hostname_when_name_has_no_ascii(monkeypatch):
    monkeypatch.setattr(worker_mod.socket, "gethostname", lambda: "DESKTOP-AB12")
    assert worker_mod.worker_id_for("我的电脑") == "DESKTOP-AB12"
    monkeypatch.setattr(worker_mod.socket, "gethostname", lambda: "")
    assert worker_mod.worker_id_for(None) == "windows-worker"


#: FakeApi 回给 revoke_worker_credential 的状态码（用例里逐段改）
_STATUS = [204]


def test_revoke_worker_credential_is_best_effort(monkeypatch):
    """「断开并重新配置」要在服务端注销这枚凭证——只清本机的话，那枚凭证依然有效。

    Mac 转码器的同一步（DevicePairing.revokeCurrentDevice）注释写了原因：不注销的话
    「设置 → 设备」里会一直挂着一台已经断开、却还能连上来的机器。服务器连不上也必须
    能断开，所以失败只返回 False，由调用方照常清本机。
    """
    calls: list[tuple] = []

    class FakeApi:
        def __init__(self, server: str, token: str | None) -> None:
            calls.append(("init", server, token))

        def request(self, method: str, path: str, **kwargs):
            calls.append((method, path, kwargs.get("timeout")))
            return _STATUS[0], None

    monkeypatch.setattr(worker_mod, "Api", FakeApi)

    # 没令牌：不用发请求
    calls.clear()
    assert worker_mod.revoke_worker_credential("http://nas:3000", None) is False
    assert calls == []

    # 成功：DELETE 打到「注销自己」，且用短超时（服务器连不上不能拖住断开）
    calls.clear()
    _STATUS[0] = 204
    assert worker_mod.revoke_worker_credential("http://nas:3000", "mclaw_x") is True
    assert calls == [
        ("init", "http://nas:3000", "mclaw_x"),
        ("DELETE", "/api/v1/auth/devices/current", 5.0),
    ]

    # 服务端说不认识这枚凭证（401）：没注销成功
    _STATUS[0] = 401
    assert worker_mod.revoke_worker_credential("http://nas:3000", "mclaw_x") is False

    # 连不上/超时：抛异常也不能把断开的收尾带崩
    def boom(*_a, **_k):
        raise OSError("network down")

    monkeypatch.setattr(worker_mod, "Api", boom)
    assert worker_mod.revoke_worker_credential("http://nas:3000", "mclaw_x") is False


def test_check_worker_name_rejects_instead_of_silently_sanitizing():
    """名称要当 worker_id 用，就得先通过服务端那条规则——不然会被静默清洗掉。

    ``worker_id_for`` 对非法字符静默清洗、洗没了退主机名：用户在 GUI「名称」里填
    中文名，界面上看着改了，网页上还是主机名，查不出为什么。所以入口先拦一道，
    说清楚哪里不行。留空合法（那正是「用主机名」）。
    """
    from movieclaw_api.services.playback.remote_worker import _WORKER_ID

    # 合法：留空 = 用主机名；ASCII 名原样可用
    assert worker_mod.check_worker_name(None) is None
    assert worker_mod.check_worker_name("") is None
    for good in ("study-pc", "a", "a" * 64, "Mac-mini.local", "0start", "has:colon"):
        assert worker_mod.check_worker_name(good) is None, good
        assert _WORKER_ID.fullmatch(good)

    # 非法：会改变 worker_id 的一律拦下，并在理由里带上原名
    for bad in ("书房那台", "中文abc", "my pc", "-lead", ".dot", "a" * 65):
        problem = worker_mod.check_worker_name(bad)
        assert problem and bad in problem, bad
        # 校验规则与服务端同一条：被拒的名字确实过不了 _WORKER_ID
        assert not _WORKER_ID.fullmatch(bad)


def test_pairing_emits_code_and_approval_url(monkeypatch):
    """配对码与批准页地址以 pairing 事件交出（GUI 据此显示配对码、提供「打开批准页」）。"""

    def fake_request(self, method, path, **_kwargs):
        if path.endswith("/device/authorize"):
            return 200, {"data": {
                "device_code": "dc",
                "user_code": "ABCD-1234",
                "verification_uri_complete": "http://nas:3000/activate?user_code=ABCD-1234",
                "expires_in": 600,
                "interval": 0,
            }}
        return 200, {"data": {"token": "tok"}}

    monkeypatch.setattr(worker_mod.Api, "request", fake_request)
    events: list[tuple[str, dict]] = []
    token = worker_mod.Api("http://nas:3000", None).pair(
        "Preload Worker（X）", status_cb=lambda kind, **fields: events.append((kind, fields))
    )
    assert token == "tok"
    kind, fields = events[0]
    assert kind == "pairing"
    assert fields["user_code"] == "ABCD-1234"
    assert fields["verify_url"].endswith("user_code=ABCD-1234")
    assert fields["expires_in"] == 600


def test_stop_cancels_pairing_wait(monkeypatch):
    """配对等待中点了停止：立即取消，不干等到配对码过期。"""

    def fake_request(self, method, path, **_kwargs):
        if path.endswith("/device/authorize"):
            return 200, {"data": {
                "device_code": "dc",
                "user_code": "X",
                "verification_uri_complete": "u",
                "expires_in": 600,
                "interval": 0,
            }}
        return 202, None  # 还没批准

    monkeypatch.setattr(worker_mod.Api, "request", fake_request)
    stop = threading.Event()
    stop.set()
    with pytest.raises(RuntimeError, match="已取消配对"):
        worker_mod.Api("http://nas:3000", None).pair("x", stop=stop)


def test_runtime_busy_counts_preload_and_realtime_jobs():
    """睡眠占用的判据：预转码在跑（tasks）或实时任务在跑（客户端 jobs）任一即忙。"""
    rt = WorkerRuntime(server="http://nas", token="t", ffmpeg="ffmpeg")
    assert rt._busy() is False
    rt.tasks[7] = "a.mkv"
    assert rt._busy() is True
    rt.tasks.pop(7)
    client = types.SimpleNamespace(jobs={"j": object()}, jobs_lock=threading.Lock())
    rt._realtime_client = client
    assert rt._busy() is True
    client.jobs.clear()
    assert rt._busy() is False


def test_check_ffmpeg_reports_missing_and_broken_binaries(tmp_path):
    """点「启动」时的校验：找不到、非零退出都要给出原因（不是静默上线后任务一单单失败）。"""
    assert "找不到 ffmpeg" in worker_mod.check_ffmpeg(str(tmp_path / "no-such-ffmpeg.exe"))
    # 能启动、但非零退出的「ffmpeg」（脚本顶替）：退出码要说出来
    if sys.platform == "win32":
        fake = tmp_path / "ffmpeg.cmd"
        fake.write_text("@exit /b 3\r\n")
    else:
        fake = tmp_path / "ffmpeg"
        fake.write_text("#!/bin/sh\nexit 3\n")
        fake.chmod(0o755)
    problem = worker_mod.check_ffmpeg(str(fake))
    assert problem is not None and "退出码 3" in problem


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="需要 PATH 里有 ffmpeg")
def test_check_ffmpeg_accepts_working_binary():
    assert worker_mod.check_ffmpeg(shutil.which("ffmpeg")) is None


class _BusyClient:
    """有一个实时任务在跑的 RealtimeClient 替身：记下让位回报。"""

    def __init__(self) -> None:
        self.jobs = {"rt-1": object()}
        self.jobs_lock = threading.Lock()
        self.yielded: list[int] = []

    def send_preload_yielded(self, task_id) -> None:
        self.yielded.append(task_id)


def test_preload_slots_follow_concurrency_when_idle():
    """空闲时并发位用满：并发 2 的 Worker 能同时跑两段预转码（不再是并发−1）。"""
    rt = WorkerRuntime(server="http://nas", token="t", ffmpeg="ffmpeg", concurrency=2)
    assert rt._preload_slots.acquire(blocking=False)
    assert rt._preload_slots.acquire(blocking=False)
    assert not rt._preload_slots.acquire(blocking=False)


def test_preload_yields_back_when_playback_is_running(monkeypatch):
    """推送与播放抢位的竞态：这台在播，预转码不开跑，当场让位回报服务端。"""
    rt = WorkerRuntime(server="http://nas", token="t", ffmpeg="ffmpeg")
    rt.api = object()
    client = _BusyClient()
    rt._realtime_client = client
    ran: list[int] = []
    monkeypatch.setattr(worker_mod, "process_task", lambda *a, **k: ran.append(1) or True)
    assert rt._run_preload({"task_id": 7, "display_name": "a.mkv"}) is False
    assert client.yielded == [7] and ran == []


def _run_until_stopped(rt, task_id: int, *, cancel: bool) -> None:
    """让 _run_preload 跑起来，等它登记停止钩子后按 cancel 叫停，并等它收尾。"""
    thread = threading.Thread(
        target=rt._run_preload, args=({"task_id": task_id, "display_name": "a.mkv"},),
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 5
    while task_id not in rt._preload_stop and time.monotonic() < deadline:
        time.sleep(0.01)
    rt._stop_preload_task(task_id, cancel=cancel)
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_yield_keeps_local_work_but_cancel_removes_it(monkeypatch, tmp_path):
    """让位保留本地源文件与分段（回来接着用）；服务端取消才删掉整个工作目录。"""
    monkeypatch.setattr(worker_mod, "WORK_DIR", tmp_path / "work")

    def fake_process_task(api, ffmpeg, task, workdir, *, stop=None, register=None, progress=None):
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / "source.mkv").write_bytes(b"x")
        stop.wait(5)  # 等停止钩子（让位或取消）
        return False

    monkeypatch.setattr(worker_mod, "process_task", fake_process_task)
    for task_id, cancel, kept in ((11, False, True), (12, True, False)):
        rt = WorkerRuntime(server="http://nas", token="t", ffmpeg="ffmpeg")
        rt.api = object()
        _run_until_stopped(rt, task_id, cancel=cancel)
        assert (tmp_path / "work" / str(task_id) / "source.mkv").exists() is kept


class _ChunkedResp:
    """假响应：按块吐字节，读空即结束（对应 urllib 的 read 语义）。"""

    def __init__(self, total: int, chunk: int) -> None:
        self._left = total
        self._chunk = chunk

    def read(self, _size: int) -> bytes:
        take = min(self._chunk, self._left)
        self._left -= take
        return b"x" * take


class _FakeClock:
    """每次取时间前进固定步长，让 1 秒测速窗口在确定块数后落值。"""

    def __init__(self, step: float) -> None:
        self._now = 0.0
        self._step = step

    def __call__(self) -> float:
        self._now += self._step
        return self._now


def test_copy_source_stream_reports_speed_and_eta(tmp_path):
    """取源时上报滑动平均速度与预计完成时间（页面的「速度 + 剩余」就靠这条链）。"""
    chunk = 1024
    total = 10 * chunk
    # 时钟每取一次前进 1 秒 ≈ 每块耗时 1 秒：窗口稳定落在 1 块/秒
    clock = _FakeClock(1.0)
    progress: dict = {}
    dest = tmp_path / "source.mkv"
    with open(dest, "wb") as out:
        received = worker_mod.copy_source_stream(
            _ChunkedResp(total, chunk), out, progress, expected=total, clock=clock
        )
    assert received == total and dest.stat().st_size == total
    assert progress["downloaded_bytes"] == total
    assert progress["speed_bps"] == chunk  # 1 秒窗口正好收到 1 块
    # ETA 每个窗口按「剩余字节 ÷ 当时速度」重算，最后一窗只剩 1 块 = 1 秒
    assert progress["eta_seconds"] == 1


def test_copy_source_stream_without_speed_leaves_eta_absent(tmp_path):
    """源太短（不到一个测速窗口）时不编造速度：页面只显示「取源中」。"""
    progress: dict = {}
    dest = tmp_path / "source.mkv"
    with open(dest, "wb") as out:
        received = worker_mod.copy_source_stream(
            _ChunkedResp(4, 4), out, progress, expected=4, clock=_FakeClock(0.0)
        )
    assert received == 4
    assert "speed_bps" not in progress and "eta_seconds" not in progress


class _FakeApi:
    """只记账、不触网的 Api 替身。"""

    def __init__(self, server: str = "http://nas:3000") -> None:
        self.server = server
        self.calls: list[tuple[str, str]] = []

    def request(self, method, path, **_kwargs):  # noqa: ANN001
        self.calls.append((method, path))
        return 200, {"data": {"file_id": 1}}


def _preload_task(**overrides) -> dict:
    task = {
        "task_id": 7,
        "display_name": "a.mkv",
        "argv": ["-hide_banner", "-i", "source.mkv", "-c:v", "copy",
                 "-f", "matroska", "artifact.mkv"],
        "source_url": "api/v1/preload-worker/preload-source/7?token=t",
        "artifact_url": "api/v1/preload-worker/tasks/7/artifacts/artifact.mkv?token=t",
        "segment_seconds": 600,
        "duration_seconds": 1200,
        "source_size_bytes": 1000,
        "resume_parts": 0,
    }
    task.update(overrides)
    return task


def test_segment_argv_puts_t_after_the_input_and_ss_before_it(tmp_path, monkeypatch):
    """`-t` 必须落在 -i 的取值之后（输出侧），`-ss` 在 -i 之前。

    2026-10-14 真机实测：输入侧 -t 切不动字幕包，每段的 SRT 轨带上整部片的
    时间戳，concat 后时长 514% 偏差。边下边转给 -i 前插了网络选项，位置一旦
    算错就会把它推回输入侧——这里把两个位置钉死。
    """
    monkeypatch.delenv("MOVIECLAW_WORKER_STREAM_SOURCE", raising=False)
    seen: list[list[str]] = []

    def fake_run_ffmpeg(ffmpeg, argv, workdir, *, stop=None, register=None, env=None):
        seen.append(list(argv))
        Path(argv[-1]).write_bytes(b"seg")
        return 0, ""

    monkeypatch.setattr(worker_mod, "run_ffmpeg", fake_run_ffmpeg)
    worker_mod.process_task(
        _FakeApi(), "ffmpeg", _preload_task(segment_seconds=600, duration_seconds=1800),
        tmp_path / "work" / "7", progress={},
    )

    assert len(seen) == 3
    first, middle, last = seen
    # 第 0 段 seek=0 不出 -ss；-t 必须落在 -i 的取值之后
    assert "-ss" not in first
    assert first.index("-t") == first.index("-i") + 2
    # 中间段：-ss 在 -i 之前，-t 紧贴 -i 的取值之后
    assert middle.index("-ss") < middle.index("-i") < middle.index("-t")
    assert middle.index("-t") == middle.index("-i") + 2
    # 末段转到 EOF：不出 -t
    assert "-t" not in last


def test_process_task_reports_finalizing_before_complete(tmp_path, monkeypatch):
    """收尾阶段必须真被上报出去，而且是在 complete 之前。

    2026-10-20 实测缺陷：`finalizing` 写完就 `progress_stop.set()`，上报线程
    按 60 秒周期轮——收尾只有一个瞬间，基本发不出去，活动页一直挂着最后一段
    看着像卡死。这里钉死三件事：finalizing 发过、发在 complete 之前、
    上报线程活到 complete 返回（拼接是分钟级，要靠它续约）。
    """
    monkeypatch.delenv("MOVIECLAW_WORKER_STREAM_SOURCE", raising=False)
    events: list[tuple[str, str]] = []
    alive_at_complete: list[bool] = []

    def fake_run_ffmpeg(ffmpeg, argv, workdir, *, stop=None, register=None, env=None):
        Path(argv[-1]).write_bytes(b"seg")
        return 0, ""

    class RecordingApi(_FakeApi):
        def request(self, method, path, **kwargs):  # noqa: ANN001
            body = kwargs.get("json_body") or {}
            if path.endswith("/complete"):
                # complete 是分钟级大活，期间必须有上报线程在续约，
                # 否则 10 分钟租约会把任务判超时、重排，白拼一次
                alive_at_complete.append(
                    any(
                        t.name == "progress-reporter" and t.is_alive()
                        for t in threading.enumerate()
                    )
                )
                events.append(("complete", ""))
            elif path.endswith("/progress"):
                events.append(("progress", str(body.get("phase"))))
            return super().request(method, path, **kwargs)

    monkeypatch.setattr(worker_mod, "run_ffmpeg", fake_run_ffmpeg)
    ok = worker_mod.process_task(
        RecordingApi(), "ffmpeg", _preload_task(segment_seconds=0),
        tmp_path / "work" / "7", progress={},
    )

    assert ok is True
    phases = [phase for kind, phase in events if kind == "progress"]
    assert "finalizing" in phases, f"收尾阶段没上报出去：{events}"
    # finalizing 必须早于 complete——complete 之后任务就进终态了
    assert events.index(("progress", "finalizing")) < events.index(("complete", ""))
    # 上报线程活到 complete 返回
    assert alive_at_complete == [True]


def test_process_task_streams_source_without_downloading(tmp_path, monkeypatch):
    """默认边下边转：ffmpeg 的 -i 直接指向取源端点，本地不落整文件。

    2026-10-20 实测真实 argv 经 HTTP 直读该端点做整段转码 speed=5.93x——
    取源不是瓶颈，串行的整文件下载阶段纯属浪费。
    """
    monkeypatch.delenv("MOVIECLAW_WORKER_STREAM_SOURCE", raising=False)
    seen: list[list[str]] = []

    def fake_run_ffmpeg(ffmpeg, argv, workdir, *, stop=None, register=None, env=None):
        seen.append(list(argv))
        # 模拟 ffmpeg 产出本段
        Path(argv[-1]).write_bytes(b"seg")
        return 0, ""

    monkeypatch.setattr(worker_mod, "run_ffmpeg", fake_run_ffmpeg)
    workdir = tmp_path / "work" / "7"
    api = _FakeApi()
    ok = worker_mod.process_task(api, "ffmpeg", _preload_task(), workdir, progress={})

    assert ok is True
    assert len(seen) == 2  # 1200s / 600s = 2 段
    # -i 的取值是 HTTP 端点，不是本地 source.mkv
    first = seen[0]
    assert first[first.index("-i") + 1].startswith("http://nas:3000/api/v1/preload-worker/")
    # 网络自愈选项贴在 -i 之前
    assert "-reconnect" in first and first.index("-reconnect") < first.index("-i")
    # 没有整文件下载：服务端只收到分段 PUT、进度上报与 complete
    assert not (workdir / "source.mkv").exists()
    assert all(
        method == "PUT" or path.endswith(("/complete", "/progress"))
        for method, path in api.calls
    )


def test_process_task_falls_back_to_download_when_stream_cannot_open(tmp_path, monkeypatch):
    """直读打不开输入时退回整文件下载，不把「读不了源」变成任务终态失败。"""
    monkeypatch.delenv("MOVIECLAW_WORKER_STREAM_SOURCE", raising=False)
    seen: list[list[str]] = []

    def fake_run_ffmpeg(ffmpeg, argv, workdir, *, stop=None, register=None, env=None):
        seen.append(list(argv))
        input_value = argv[argv.index("-i") + 1]
        if input_value.startswith("http"):
            return 1, "[in#0] Error opening input: Server returned 404"
        Path(argv[-1]).write_bytes(b"seg")
        return 0, ""

    monkeypatch.setattr(worker_mod, "run_ffmpeg", fake_run_ffmpeg)

    class _Resp:
        def __init__(self) -> None:
            self._done = False

        def read(self, _size: int) -> bytes:
            if self._done:
                return b""
            self._done = True
            return b"x" * 1000

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(worker_mod._NO_PROXY_OPENER, "open", lambda *a, **k: _Resp())
    workdir = tmp_path / "work" / "7"
    ok = worker_mod.process_task(
        _FakeApi(), "ffmpeg", _preload_task(segment_seconds=0), workdir, progress={}
    )

    assert ok is True
    # 第一次是 HTTP 直读失败，第二次已经是本地文件
    assert seen[0][seen[0].index("-i") + 1].startswith("http")
    assert seen[-1][seen[-1].index("-i") + 1] == "source.mkv"
    # 任务成功后源文件照常清理，说明中途确实下过整文件（下载走 _NO_PROXY_OPENER）
    assert not (workdir / "source.mkv").exists()


def test_ffmpeg_env_keeps_internal_hosts_off_the_proxy(monkeypatch):
    """ffmpeg 会认 http_proxy：内网 NAS 地址必须进 no_proxy，否则被绕去代理。"""
    monkeypatch.setenv("no_proxy", "example.com")
    env = worker_mod.ffmpeg_env()
    assert "192.168.31.10" in env["no_proxy"]
    assert "example.com" in env["no_proxy"]  # 原有条目保留
    assert env["NO_PROXY"] == env["no_proxy"]


def test_stream_source_can_be_switched_off(monkeypatch):
    """排查时可用环境变量退回「先整文件下载再转」。"""
    monkeypatch.setenv("MOVIECLAW_WORKER_STREAM_SOURCE", "0")
    assert worker_mod.stream_source_enabled() is False
    monkeypatch.setenv("MOVIECLAW_WORKER_STREAM_SOURCE", "1")
    assert worker_mod.stream_source_enabled() is True
    monkeypatch.delenv("MOVIECLAW_WORKER_STREAM_SOURCE")
    assert worker_mod.stream_source_enabled() is True  # 默认开
