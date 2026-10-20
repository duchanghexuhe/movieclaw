"""Worker GUI 的「重新配对」入口：只在服务端说凭证失效时出现，点了丢掉旧令牌、重新配对。

用真 Tk 驱动（``root.update()`` 泵事件循环、读按钮的布局状态），不起托盘，配置与日志写到
临时目录——不碰仓库里 worker/ 旁边的 worker.json 与 worker-gui.log。
"""

from __future__ import annotations

import json
import threading
import time

import pytest

tk = pytest.importorskip("tkinter")

_AUTH_REJECTION = {
    "reason": "服务端关闭了连接（1008）：凭证无效或已被吊销，请在网页「设置 → 设备」重新配对",
    "retry_seconds": 300,
    "rejection": "auth",
}


@pytest.fixture(scope="module")
def tk_root():
    """整个模块共用一个 Tk 解释器：Windows 上同进程反复新建 Tk 会间歇报
    「Can't find a usable tk.tcl」，每个用例改用自己的 Toplevel。"""
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"没有可用的显示环境：{exc}")
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def gui(tk_root, tmp_path, monkeypatch):
    import worker_gui

    import worker

    config_file = tmp_path / "worker.json"
    config_file.write_text(
        json.dumps({"server": "http://nas:3000", "ffmpeg": "ffmpeg", "token": "old-token"})
    )
    monkeypatch.setattr(worker, "CONFIG_FILE", config_file)
    monkeypatch.setattr(worker_gui, "LOG_FILE", tmp_path / "worker-gui.log")
    monkeypatch.setattr(worker_gui.WorkerGUI, "_install_tray", lambda self: None)
    window = tk.Toplevel(tk_root)
    window.withdraw()
    app = worker_gui.WorkerGUI(window)
    yield app, config_file
    if app.runtime is not None:
        app.runtime.stop_event.set()
    # 让轮询循环下一拍自行收尾（走退出分支，不再续约 after），再销毁窗口
    app.runtime = None
    app._exiting = True
    window.destroy()


def _repair_shown(app) -> bool:
    return app.btn_repair.winfo_manager() == "pack"


def test_repair_button_only_on_credential_rejection(gui):
    app, _ = gui
    app.root.update()
    assert not _repair_shown(app)

    app._on_event(
        "offline",
        {
            "reason": "服务端关闭了连接（1008）：服务端尚未启用远程转码",
            "retry_seconds": 60,
            "rejection": "remote_disabled",
        },
    )
    app.root.update()
    assert not _repair_shown(app)

    app._on_event("offline", dict(_AUTH_REJECTION))
    app.root.update()
    assert _repair_shown(app)
    assert "凭证已失效" in app.lbl_connection.cget("text")

    # 状态一变（重连成功、停止、进入配对），按钮就收起
    app._on_event("online", {"server": "http://nas:3000"})
    app.root.update()
    assert not _repair_shown(app)


def test_repair_drops_token_and_restarts_into_pairing(gui, monkeypatch):
    import worker_gui

    app, config_file = gui
    started: list[dict] = []

    class FakeRuntime:
        """只记下启动参数、运行到被停止为止的假 runtime。"""

        def __init__(self, **kwargs):
            started.append(kwargs)
            self.stop_event = threading.Event()
            self.ac_only = False

        def start(self):
            self.stop_event.wait(10)

        def progress_view(self):
            return []

    monkeypatch.setattr(worker_gui, "WorkerRuntime", FakeRuntime)
    monkeypatch.setattr(worker_gui, "check_ffmpeg", lambda path: None)

    app._start()
    assert started[-1]["token"] == "old-token"
    app._on_event("offline", dict(_AUTH_REJECTION))
    app.root.update()
    assert _repair_shown(app)

    app._repair()
    deadline = time.monotonic() + 5
    while len(started) < 2 and time.monotonic() < deadline:
        app.root.update()
        time.sleep(0.05)

    # 旧运行收尾后按原配置重新启动，但不带令牌——runtime 据此直接进入配对
    assert len(started) == 2
    assert started[-1]["token"] is None
    assert started[-1]["server"] == "http://nas:3000"
    assert "token" not in json.loads(config_file.read_text())
    assert not _repair_shown(app)
