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
