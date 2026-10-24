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


def _disconnect_shown(app) -> bool:
    return app.btn_disconnect.winfo_manager() == "pack"


def test_disconnect_button_shows_when_paired_and_relabels_on_rejection(gui):
    """有凭证就给出口：稳态是「断开并重新配置…」，凭证失效时改叫「重新配对」。

    配对入口是这个窗口的「启动」（没令牌时启动即进配对）；配对之后若没有出口，
    用户既断不开也重配不了——这正是「只有启动/停止」时的窟窿。
    """
    app, _ = gui  # fixture 的配置里带着 token（已配对）
    app.root.update()
    assert _disconnect_shown(app)
    assert app.btn_disconnect.cget("text") == "断开并重新配置…"

    # 与凭证无关的掉线（服务端没开远程转码等）：还是那句「断开并重新配置…」
    app._on_event(
        "offline",
        {
            "reason": "服务端关闭了连接（1008）：服务端尚未启用远程转码",
            "retry_seconds": 60,
            "rejection": "remote_disabled",
        },
    )
    app.root.update()
    assert _disconnect_shown(app)
    assert app.btn_disconnect.cget("text") == "断开并重新配置…"
    assert "凭证已失效" not in app.lbl_connection.cget("text")

    app._on_event("offline", dict(_AUTH_REJECTION))
    app.root.update()
    assert app.btn_disconnect.cget("text") == "重新配对"
    assert "凭证已失效" in app.lbl_connection.cget("text")

    # 重新连上：回到「断开并重新配置…」
    app._on_event("online", {"server": "http://nas:3000"})
    app.root.update()
    assert app.btn_disconnect.cget("text") == "断开并重新配置…"


def test_idle_status_says_whether_paired(gui):
    """空闲状态行要把「配没配对」写在脸上，并指出配对入口是「启动」。"""
    app, config_file = gui
    app.config["token"] = None
    app._set_conn(app._idle_text("未启动"))
    app.root.update()
    assert "未配对" in app.lbl_connection.cget("text")
    assert "启动" in app.lbl_connection.cget("text"), "要说清点哪儿去配对"
    assert not _disconnect_shown(app), "没凭证就没有可断开的东西"

    app.config["token"] = "mclaw_x"
    app.config["name"] = "study-pc"
    app._set_conn(app._idle_text("未启动"))
    app.root.update()
    assert "已配对" in app.lbl_connection.cget("text")
    assert "study-pc" in app.lbl_connection.cget("text"), "要显示网页上看到的名字"


def test_repair_drops_token_and_restarts_into_pairing(gui, monkeypatch):
    import worker_gui

    app, config_file = gui
    started: list[dict] = []
    revoked: list[tuple[str, str]] = []

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

    monkeypatch.setattr(worker_gui, "WorkerRuntime", FakeRuntime)
    monkeypatch.setattr(worker_gui, "check_ffmpeg", lambda path: None)
    # 注销走真网络就会在这里等 5 秒（测试机没有 http://nas:3000）：换成记账
    monkeypatch.setattr(
        worker_gui,
        "revoke_worker_credential",
        lambda server, token, **kw: revoked.append((server, token)) or True,
    )

    app._start()
    assert started[-1]["token"] == "old-token"
    app._on_event("offline", dict(_AUTH_REJECTION))
    app.root.update()
    assert app.btn_disconnect.cget("text") == "重新配对"

    app._disconnect()
    deadline = time.monotonic() + 5
    while len(started) < 2 and time.monotonic() < deadline:
        app.root.update()
        time.sleep(0.05)

    # 断开时先在服务端注销旧凭证，再按原配置重新启动（不带令牌 → 直接进配对）
    assert revoked == [("http://nas:3000", "old-token")]
    assert len(started) == 2
    assert started[-1]["token"] is None
    assert started[-1]["server"] == "http://nas:3000"
    assert "token" not in json.loads(config_file.read_text())


def test_pairing_token_counts_in_the_same_session(gui, monkeypatch):
    """配对成功的令牌要当场生效：界面用的 self.config 也得拿到它。

    否则本次运行里配对完「断开并重新配置…」按钮、「已配对」状态都不出现，
    凭证被注销时「重新配对」也看不见，直到重启 GUI（2026-10-24 端到端实测）。
    """
    import worker_gui

    app, config_file = gui
    app.config = {"server": "http://nas:3000", "ffmpeg": "ffmpeg"}
    app._sync_disconnect_button()
    assert not _disconnect_shown(app), "没凭证就没有可断开的东西"

    class PairingRuntime:
        """配对成功：与 WorkerRuntime.ensure_token 一样，在 runtime 线程里回调 on_token。"""

        def __init__(self, **kwargs):
            self.on_token = kwargs["on_token"]
            self.stop_event = threading.Event()
            self.ac_only = False

        def start(self):
            self.on_token("fresh-token")
            self.stop_event.wait(10)

        def progress_view(self):
            return []

    monkeypatch.setattr(worker_gui, "WorkerRuntime", PairingRuntime)
    monkeypatch.setattr(worker_gui, "check_ffmpeg", lambda path: None)

    def saved_token() -> str | None:
        try:
            return json.loads(config_file.read_text()).get("token")
        except (OSError, ValueError):
            return None  # 正好在写：下一拍再读

    app._start()
    deadline = time.monotonic() + 5
    while saved_token() != "fresh-token" and time.monotonic() < deadline:
        app.root.update()
        time.sleep(0.05)
    assert saved_token() == "fresh-token"
    assert app.config.get("token") == "fresh-token"

    app._on_event("online", {"server": "http://nas:3000"})
    app.root.update()
    assert _disconnect_shown(app), "配对成功后这一次运行里就该看得见「断开并重新配置…」"
    assert "已配对" in app._idle_text("已停止")

    app._on_event("offline", dict(_AUTH_REJECTION))
    app.root.update()
    assert _disconnect_shown(app)
    assert app.btn_disconnect.cget("text") == "重新配对"


def test_manual_disconnect_revokes_and_stays_unpaired(gui, monkeypatch):
    """稳态「断开并重新配置…」：注销服务端凭证、清本机令牌，停在未配对不再自动重配。

    与凭证失效那条路（点的是「重新配对」，断完接着配）分开：这里用户要的是「断开」。
    """
    import worker_gui

    app, config_file = gui
    started: list[dict] = []
    revoked: list[tuple[str, str]] = []

    class FakeRuntime:
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
    monkeypatch.setattr(
        worker_gui,
        "revoke_worker_credential",
        lambda server, token, **kw: revoked.append((server, token)) or True,
    )
    monkeypatch.setattr(worker_gui.messagebox, "askyesno", lambda *a, **k: True)

    app._start()
    app._disconnect()
    deadline = time.monotonic() + 5
    while app.config.get("token") and time.monotonic() < deadline:
        app.root.update()
        time.sleep(0.05)
    for _ in range(20):  # 等注销结果回到界面
        app.root.update()
        time.sleep(0.05)

    assert revoked == [("http://nas:3000", "old-token")]
    assert len(started) == 1, "「断开」不该自动重新配对"
    assert "token" not in json.loads(config_file.read_text())
    assert not _disconnect_shown(app), "没凭证了，出口收起"
    assert "未配对" in app.lbl_connection.cget("text")
    assert "已在服务端注销" in app.txt_log.get("1.0", tk.END)


def test_disconnect_cancel_keeps_everything(gui, monkeypatch):
    """确认框里点「否」== 什么都没发生：凭证留着、不注销、不重启。"""
    import worker_gui

    app, config_file = gui
    revoked: list[tuple[str, str]] = []
    monkeypatch.setattr(
        worker_gui,
        "revoke_worker_credential",
        lambda server, token, **kw: revoked.append((server, token)) or True,
    )
    monkeypatch.setattr(worker_gui.messagebox, "askyesno", lambda *a, **k: False)

    app._disconnect()
    app.root.update()

    assert revoked == []
    assert json.loads(config_file.read_text())["token"] == "old-token"
    assert _disconnect_shown(app)


def test_name_field_edits_the_worker_id_that_web_shows(gui, monkeypatch):
    """「名称」就是 WS hello 的 worker_id：服务端握手时拿它同步设备名。

    Mac 转码器的同名项在「这台 Mac → 名称」，两端必须一个口径——否则一端能改名、
    另一端改不了，网页上看到的名字就取决于哪台机器在跑。
    """
    import worker_gui

    app, config_file = gui
    started: list[dict] = []

    class FakeRuntime:
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

    # 配置里没名字过：输入框是空的，留空就沿用 worker_id_for 的主机名回退
    assert app.var_name.get() == ""
    app.var_name.set("study-pc")
    app._start()
    assert started[-1]["name"] == "study-pc"
    assert json.loads(config_file.read_text())["name"] == "study-pc"

    # 改了名字再启动：新名字进 runtime，服务端握手时据此同步设备名
    app.runtime.stop_event.set()
    app.runtime = None
    app.var_name.set("study-pc-2")
    app._start()
    assert started[-1]["name"] == "study-pc-2"
    assert json.loads(config_file.read_text())["name"] == "study-pc-2"

    # 清空 = 回到主机名（不写一个假的默认值进去）
    app.runtime.stop_event.set()
    app.runtime = None
    app.var_name.set("   ")
    app._start()
    assert started[-1]["name"] is None


def test_gui_refuses_chinese_name_before_it_silently_becomes_hostname(gui, monkeypatch):
    """中文名会被 worker_id_for 静默洗成主机名——启动前拦住，别让用户以为改成了。"""
    import worker_gui

    app, config_file = gui
    started: list[dict] = []
    refusals: list[str] = []

    class FakeRuntime:
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
    monkeypatch.setattr(app, "_refuse_start", refusals.append)

    app.var_name.set("书房那台")
    app._start()
    assert not started, "非法名称不该启动"
    assert refusals and "书房那台" in refusals[-1]
    # 配置里也不能留下这个会被静默改写的名字
    assert json.loads(config_file.read_text()).get("name") is None

    # 换成一个能用的名字就正常启动
    app.var_name.set("study-pc")
    app._start()
    assert started[-1]["name"] == "study-pc"


def test_task_progress_rows_show_speed_and_eta_while_downloading():
    """取源阶段的状态行要带速度和剩余时间：大源在千兆上也要跑几分钟，
    只显示百分比用户会以为卡住了（2026-10-20 用户反馈的直接诉求）。"""
    import worker_gui

    rows = worker_gui.task_progress_rows(
        [
            (
                "痴迷 (2026).mkv",
                {
                    "phase": "downloading",
                    "downloaded_bytes": 15 * 1024**3,
                    "total_bytes": 20 * 1024**3,
                    "speed_bps": int(113.09 * 1024**2),
                    "eta_seconds": 300,
                },
            )
        ]
    )
    assert len(rows) == 1
    assert "下载源 75%" in rows[0]
    assert "113.1MB/s" in rows[0]
    assert "剩余约 5 分" in rows[0]


def test_task_progress_rows_fall_back_when_worker_has_no_speed():
    """旧版 Worker 不发速度/ETA 时不能显示空括号——退回原来的纯百分比口径。"""
    import worker_gui

    rows = worker_gui.task_progress_rows(
        [
            (
                "a.mkv",
                {
                    "phase": "downloading",
                    "downloaded_bytes": 5 * 1024**2,
                    "total_bytes": 10 * 1024**2,
                },
            )
        ]
    )
    assert rows == ["· a.mkv — 下载源 50%（5MB/10MB）"]
