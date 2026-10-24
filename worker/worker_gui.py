"""MovieClaw Worker 的图形界面（tkinter + ctypes 托盘，零第三方依赖）。

用法：``pythonw worker/worker_gui.pyw``（或 python worker/worker_gui.py）。

- 主窗口：连接状态、各槽位当前任务、滚动日志、配置（服务器 / ffmpeg / 并发 /
  仅接电 / 开机自启），启动与停止；
- 托盘：关闭/最小化收进托盘，左键恢复，右键菜单（打开 / 退出）；托盘图标用
  Shell_NotifyIconW 自建消息窗实现，不碰 Tk 的窗口过程（跨线程只经队列）。

Worker 循环跑在守护线程（``WorkerRuntime``），Tk 主线程只做展示——所有
runtime → UI 的信息都经 ``queue.Queue``，由 ``root.after`` 轮询取用。
"""

from __future__ import annotations

import contextlib
import os
import queue
import sys
import threading
import time
import tkinter as tk
import traceback
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))

from worker import (  # noqa: E402
    WorkerRuntime,
    check_ffmpeg,
    check_worker_name,
    load_config,
    on_battery,
    revoke_worker_credential,
    save_config,
    worker_id_for,
)

APP_TITLE = "MovieClaw Worker"
STARTUP_BAT_NAME = "MovieClaw Worker.bat"


# ---------------------------------------------------------------------------
# 托盘（ctypes，零依赖）：自建隐藏窗口收 Explorer 的回调消息，不碰 Tk 窗口过程
# ---------------------------------------------------------------------------

TRAY_CLASS_NAME = "MovieClawPreloadTrayWnd"
WM_APP_TRAY = 0x8000  # WM_APP：Explorer 点击托盘图标时投到我们的回调消息
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_COMMAND = 0x0111
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4
IDI_APPLICATION = 32512
TPM_RETURNCMD = 0x0100
MENU_OPEN, MENU_EXIT = 1, 2


class TrayIcon:
    """托管一个托盘图标；消息循环跑在独立线程，动作经 ``actions`` 队列交还 Tk。"""

    def __init__(self, tooltip: str, actions: queue.Queue[tuple]) -> None:
        import ctypes
        from ctypes import wintypes

        self._ct = ctypes
        self._wt = wintypes
        self.tooltip = tooltip[:127]
        self.actions = actions
        self._hicon = None
        self._created = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()
        self._created.wait(timeout=3)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        ct = self._ct
        wparam, lparam = int(wparam), int(lparam)
        if msg == WM_APP_TRAY:
            if lparam == WM_LBUTTONUP:
                self.actions.put(("restore",))
            elif lparam == WM_RBUTTONUP:
                cmd = self._popup_menu(hwnd)
                if cmd == MENU_OPEN:
                    self.actions.put(("restore",))
                elif cmd == MENU_EXIT:
                    self.actions.put(("exit",))
            return 0
        if msg == WM_DESTROY:
            self._remove(hwnd)
            ct.windll.user32.PostQuitMessage(0)
            return 0
        return ct.windll.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _popup_menu(self, hwnd) -> int:
        ct = self._ct
        menu = ct.windll.user32.CreatePopupMenu()
        ct.windll.user32.AppendMenuW(menu, 0, MENU_OPEN, "打开主窗口")
        ct.windll.user32.AppendMenuW(menu, 0, MENU_EXIT, "退出")
        ct.windll.user32.SetForegroundWindow(hwnd)
        # TrackPopupMenu 阻塞在托盘线程里，返回选中的命令 id
        pt = self._wt.POINT()
        ct.windll.user32.GetCursorPos(ct.byref(pt))
        cmd = ct.windll.user32.TrackPopupMenu(
            menu, TPM_RETURNCMD, pt.x, pt.y, 0, hwnd, None
        )
        ct.windll.user32.DestroyMenu(menu)
        return cmd

    def _run(self) -> None:  # noqa: C901 —— ctypes 结构体样板较长
        import ctypes
        from ctypes import wintypes

        ct = self._ct
        user32 = ct.windll.user32
        kernel32 = ct.windll.kernel32

        WNDPROC = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, wintypes.HWND, ctypes.c_size_t,
            ctypes.c_size_t, ctypes.c_ssize_t,
        )
        self._wndproc_ref = WNDPROC(self._wndproc)  # 防 GC

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", ctypes.c_uint),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("hWnd", wintypes.HWND),
                ("uID", ctypes.c_uint),
                ("uFlags", ctypes.c_uint),
                ("uCallbackMessage", ctypes.c_uint),
                ("hIcon", wintypes.HICON),
                ("szTip", ctypes.c_wchar * 128),
                ("dwState", ctypes.c_uint),
                ("dwStateMask", ctypes.c_uint),
                ("szInfo", ctypes.c_wchar * 256),
                ("uVersion", ctypes.c_uint),
                ("szInfoTitle", ctypes.c_wchar * 64),
                ("dwInfoFlags", ctypes.c_uint),
            ]

        shell32 = ct.windll.shell32
        # 64 位下 lParam 是 LONG_PTR：不设 argtypes 时 ctypes 按 c_int 转发会溢出
        user32.DefWindowProcW.argtypes = [
            wintypes.HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t,
        ]
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        hinstance = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc_ref
        wc.lpszClassName = TRAY_CLASS_NAME
        wc.hInstance = hinstance
        if not user32.RegisterClassW(ct.byref(wc)):
            return
        # 隐藏的弹窗形态窗口（message-only 窗口收不到 TrackPopupMenu 的路由）
        self._hwnd = user32.CreateWindowExW(
            0, TRAY_CLASS_NAME, "tray", 0, 0, 0, 0, 0, None, None, hinstance, None
        )
        self._hicon = user32.LoadIconW(None, IDI_APPLICATION)
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(nid)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_APP_TRAY
        nid.hIcon = self._hicon
        nid.szTip = self.tooltip
        if not shell32.Shell_NotifyIconW(NIM_ADD, ct.byref(nid)):
            return
        self._created.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ct.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ct.byref(msg))
            user32.DispatchMessageW(ct.byref(msg))

    def remove(self) -> None:
        import ctypes

        if getattr(self, "_hwnd", None):
            class NID(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint), ("hWnd", ctypes.c_void_p),
                            ("uID", ctypes.c_uint)]
            data = NID(ctypes.sizeof(NID), self._hwnd, 1)
            self._ct.windll.shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))
            self._ct.windll.user32.DestroyWindow(self._hwnd)
            self._hwnd = None


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------


def _startup_dir() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup"


#: GUI 日志的落盘文件：与 worker.json 同目录。文本框超 400 行就滚动裁剪，
#: 重启即清空——排查「起播慢」这类时序问题要对着服务端日志比时间戳，
#: 留不住现场就无从查起。
LOG_FILE = Path(__file__).with_name("worker-gui.log")
_LOG_ROTATE_BYTES = 5 * 1024 * 1024


def _append_log_file(line: str) -> None:
    """一行日志带时间戳追加进文件；写失败静默——日志绝不能拖垮 GUI。"""
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > _LOG_ROTATE_BYTES:
            LOG_FILE.replace(LOG_FILE.with_name("worker-gui.old.log"))
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with LOG_FILE.open("a", encoding="utf-8", errors="replace") as f:
            f.write(f"{stamp} | {line}\n")
    except OSError:
        pass


def _fmt_eta(seconds: object) -> str:
    try:
        secs = int(seconds or 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    if secs <= 0:
        return ""
    if secs < 60:
        return "剩余不足 1 分"
    minutes = secs // 60
    if minutes < 60:
        return f"剩余约 {minutes} 分"
    return f"剩余约 {minutes // 60} 时 {minutes % 60} 分"


def _fmt_speed(bps: object) -> str:
    """取源速度：MB/s（Worker 上报的是字节/秒）。无值返回空串。"""
    try:
        value = float(bps or 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    return f"{value / 1024 ** 2:.1f}MB/s"


def _fmt_bytes(nbytes: float) -> str:
    if nbytes >= 1024 ** 3:
        return f"{nbytes / 1024 ** 3:.1f}GB"
    return f"{nbytes / 1024 ** 2:.0f}MB"


def task_progress_rows(view: list[tuple[str, dict]]) -> list[str]:
    """进度台账 → 状态栏任务行（纯函数：GUI 与会话外的单测共用一套口径）。

    取源阶段带速度和剩余时间（大源在千兆上也要几分钟，没有这两项用户只会觉得
    「卡住了」）；转码阶段按段位进度 + 已完成段的平均耗时估剩余。
    """
    rows: list[str] = []
    for name, prog in view:
        phase = prog.get("phase")
        if phase == "downloading":
            done = int(prog.get("downloaded_bytes") or 0)
            total = int(prog.get("total_bytes") or 0)
            tail = "，".join(
                part for part in (_fmt_speed(prog.get("speed_bps")),
                                  _fmt_eta(prog.get("eta_seconds"))) if part
            )
            detail = f"，{tail}" if tail else ""
            if total:
                rows.append(
                    f"· {name} — 下载源 {done / total * 100:.0f}%"
                    f"（{_fmt_bytes(done)}/{_fmt_bytes(total)}{detail}）"
                )
            else:
                rows.append(
                    f"· {name} — 下载源文件{('（' + tail + '）') if tail else '…'}"
                )
        elif phase == "transcoding":
            idx = max(int(prog.get("segment_index") or 0), 1)
            total_seg = int(prog.get("segment_total") or 0)
            pct = (idx - 1) / total_seg * 100 if total_seg else 0
            eta = _fmt_eta(prog.get("eta_seconds"))
            eta_text = f"，{eta}" if eta else ""
            rows.append(f"· {name} — 转码 {pct:.0f}%（第 {idx}/{total_seg} 段{eta_text}）")
        elif phase == "finalizing":
            rows.append(f"· {name} — 拼接收尾（服务端校验登记）")
        else:
            rows.append(f"· {name} — 准备中")
    return rows


class WorkerGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title(APP_TITLE)
        root.geometry("720x560")
        root.minsize(620, 480)
        self.config = load_config()
        self.runtime: WorkerRuntime | None = None
        self._start_thread: threading.Thread | None = None
        self._exiting = False
        self._exit_at = 0.0
        #: 连接状态行的文字（不含电池提示，电池条件每次刷新现算）；批准页地址只在配对时有
        self._conn_text = "未启动"
        self._approve_url: str | None = None
        #: 点了「断开并重新配置…」/「重新配对」、正等当前运行收尾：收尾后注销并清本机令牌
        self._disconnect_pending = False
        #: 上一次断连是服务端说凭证失效（点的是「重新配对」）：断完接着配，别让用户再点一次
        self._restart_after_disconnect = False
        #: 凭证被服务端判失效过——按钮据此改叫「重新配对」
        self._auth_failed = False
        self.ui_queue: queue.Queue[tuple] = queue.Queue()
        self.tray: TrayIcon | None = None
        self._build_ui()
        self._load_config_to_ui()
        self._set_conn(self._idle_text("未启动"))
        root.protocol("WM_DELETE_WINDOW", self._hide_to_tray)
        self._install_tray()
        root.after(120, self._poll_queue)
        _append_log_file(f"—— GUI 启动（PID {os.getpid()}）——")
        if "--autostart" in sys.argv and self.config.get("server") and self.config.get("token"):
            # 开机自启且已配对：直接收进托盘、开始工作，不弹窗打扰
            root.withdraw()
            self._start()

    # -- UI 构建 -------------------------------------------------------------

    def _build_ui(self) -> None:
        pad = {"padx": 10, "pady": 6}
        frame = ttk.Frame(self.root)
        frame.pack(fill=tk.BOTH, expand=True)

        status = ttk.LabelFrame(frame, text="状态")
        status.pack(fill=tk.X, **pad)
        self.lbl_connection = ttk.Label(status, text="未启动", wraplength=640)
        self.lbl_connection.grid(row=0, column=0, columnspan=2, sticky=tk.W, padx=8, pady=4)
        self.lbl_slots = ttk.Label(status, text="任务：—")
        self.lbl_slots.grid(row=1, column=0, columnspan=2, sticky=tk.W, padx=8, pady=(0, 6))

        config = ttk.LabelFrame(frame, text="配置")
        config.pack(fill=tk.X, **pad)
        ttk.Label(config, text="服务器").grid(row=0, column=0, sticky=tk.E, padx=6, pady=3)
        self.var_server = tk.StringVar()
        ttk.Entry(config, textvariable=self.var_server, width=52).grid(row=0, column=1, sticky=tk.W)
        # 名称就是 WS hello 的 worker_id（worker.worker_id_for）：握手时服务端把它同步成
        # 网页「设置 → 设备」里的设备名，「设置 → 转码」页的 Worker 行也显示它。Mac
        # 转码器同一项在「这台 Mac → 名称」，两端一个口径（docs/design/login-devices.md）。
        ttk.Label(config, text="名称").grid(row=1, column=0, sticky=tk.E, padx=6, pady=3)
        self.var_name = tk.StringVar()
        ttk.Entry(config, textvariable=self.var_name, width=32).grid(row=1, column=1, sticky=tk.W)
        # 提示单独占一行：跟在输入框右侧会被窗口右边缘截断（截断的说明等于没说）
        ttk.Label(
            config,
            text="显示在网页的设备列表与「转码」页；改完点「停止」再「启动」生效",
            foreground="#666",
        ).grid(row=2, column=1, columnspan=3, sticky=tk.W, padx=6)
        ttk.Label(config, text="ffmpeg").grid(row=3, column=0, sticky=tk.E, padx=6, pady=3)
        self.var_ffmpeg = tk.StringVar()
        entry_ff = ttk.Entry(config, textvariable=self.var_ffmpeg, width=52)
        entry_ff.grid(row=3, column=1, sticky=tk.W)
        ttk.Button(config, text="浏览…", width=8,
                   command=self._browse_ffmpeg).grid(row=3, column=2, padx=4)
        ttk.Label(config, text="并发").grid(row=4, column=0, sticky=tk.E, padx=6, pady=3)
        self.var_concurrency = tk.IntVar(value=1)
        ttk.Spinbox(config, from_=1, to=8, width=5, textvariable=self.var_concurrency).grid(
            row=4, column=1, sticky=tk.W)
        ttk.Label(
            config, text="（播放优先：有人播放时预转码暂停，空闲时用满并发）",
            foreground="#666",
        ).grid(row=4, column=1, sticky=tk.W, padx=(52, 0))
        self.var_ac_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(config, text="仅接通电源时执行", variable=self.var_ac_only).grid(
            row=5, column=0, sticky=tk.W, padx=(6, 0))
        self.var_autostart = tk.BooleanVar(value=False)
        ttk.Checkbutton(config, text="开机自启", variable=self.var_autostart,
                        command=self._toggle_autostart).grid(row=5, column=1, sticky=tk.W)

        buttons = ttk.Frame(frame)
        buttons.pack(fill=tk.X, **pad)
        self.btn_start = ttk.Button(buttons, text="启动", command=self._start)
        self.btn_start.pack(side=tk.LEFT, padx=(10, 4))
        self.btn_stop = ttk.Button(buttons, text="停止", command=self._stop, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=4)
        # 配对等待时才可点：打开批准页（地址来自 runtime 的配对事件）
        self.btn_approve = ttk.Button(
            buttons, text="打开批准页", command=self._open_approve_page, state=tk.DISABLED
        )
        self.btn_approve.pack(side=tk.LEFT, padx=4)
        # 稳态下唯一的「推倒重来」出口（与 Mac 转码器同一个动作）：断开连接，并在
        # 服务端注销这枚凭证。凭证失效时它改叫「重新配对」——配对入口就是这个窗口的
        # 「启动」（没令牌时启动即进配对），不给出口的话用户只能干看着。
        self.btn_disconnect = ttk.Button(
            buttons, text="断开并重新配置…", command=self._disconnect
        )

        log_frame = ttk.LabelFrame(frame, text="日志")
        log_frame.pack(fill=tk.BOTH, expand=True, **pad)
        self.txt_log = tk.Text(log_frame, height=12, state=tk.DISABLED, wrap=tk.NONE,
                               font=("Consolas", 9))
        scroll = ttk.Scrollbar(log_frame, command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=scroll.set)
        self.txt_log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0), pady=6)
        scroll.pack(side=tk.RIGHT, fill=tk.Y, pady=6)

    def _load_config_to_ui(self) -> None:
        self.var_server.set(self.config.get("server") or "")
        self.var_name.set(self.config.get("name") or "")
        self.var_ffmpeg.set(self.config.get("ffmpeg") or "")
        self.var_concurrency.set(int(self.config.get("concurrency") or 1))
        self.var_ac_only.set(bool(self.config.get("ac_only")))
        self.var_autostart.set((_startup_dir() / STARTUP_BAT_NAME).exists())

    # -- 动作 ----------------------------------------------------------------

    def _browse_ffmpeg(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 ffmpeg.exe", filetypes=[("ffmpeg", "ffmpeg*.exe"), ("可执行文件", "*.exe")]
        )
        if path:
            self.var_ffmpeg.set(path.replace("/", "\\"))

    def _collect_config(self) -> dict:
        return {
            "server": self.var_server.get().strip().rstrip("/"),
            # 名称留空 = 沿用 worker_id_for 的回退（本机主机名），与 CLI 的
            # --name 缺省同一条路；不在这里替用户编一个默认值
            "name": self.var_name.get().strip() or None,
            "ffmpeg": self.var_ffmpeg.get().strip(),
            "concurrency": max(1, min(int(self.var_concurrency.get() or 1), 8)),
            "ac_only": bool(self.var_ac_only.get()),
        }

    def _start(self) -> None:
        if self.runtime is not None:
            return
        config = self._collect_config()
        if not config["server"]:
            self._refuse_start("请先填服务器地址（如 http://192.168.31.10:8096）")
            return
        problem = check_worker_name(config["name"])
        if problem:
            self._refuse_start(problem)
            return
        save_config({**self.config, **config})
        self.config = load_config()
        problem = check_ffmpeg(config["ffmpeg"] or "ffmpeg")
        if problem:
            self._refuse_start(problem)
            return
        runtime = WorkerRuntime(
            server=config["server"],
            token=self.config.get("token"),
            ffmpeg=config["ffmpeg"] or "ffmpeg",
            name=self.config.get("name"),
            concurrency=config["concurrency"],
            ac_only=config["ac_only"],
            on_token=self._remember_token,
            status_cb=self._on_runtime_event,
        )
        self.runtime = runtime
        self.btn_start.configure(state=tk.DISABLED)
        self.btn_stop.configure(state=tk.NORMAL)
        self._set_conn("启动中…")
        self._start_thread = threading.Thread(
            target=self._run_runtime, args=(runtime,), daemon=True
        )
        self._start_thread.start()

    def _remember_token(self, token: str) -> None:
        """配对成功拿到令牌：写进配置文件，也同步进界面用的 self.config。

        状态行的「已配对」、按钮是「断开并重新配置…」还是「重新配对」都看 self.config；
        只写文件的话，本次运行里配对完按钮永远不出现，直到重启 GUI。
        """
        self.config["token"] = token
        save_config(self.config)

    def _refuse_start(self, reason: str) -> None:
        """启动前校验不过：不启动，给出原因（窗口可能正收在托盘里，先唤出来）。"""
        self.root.deiconify()
        self._log(reason)
        messagebox.showwarning(APP_TITLE, reason)

    def _run_runtime(self, runtime: WorkerRuntime) -> None:
        try:
            runtime.start()
        except Exception as exc:
            # 线程里的异常在 pythonw 下会静默消失，界面就卡在「连接中」；用户主动停止时
            # 的取消（如配对等待中点了停止）不算故障
            if not runtime.stop_event.is_set():
                self._on_runtime_event("error", reason=str(exc), trace=traceback.format_exc())

    def _open_approve_page(self) -> None:
        if self._approve_url:
            webbrowser.open(self._approve_url)

    def _idle_text(self, prefix: str) -> str:
        """空闲时的状态行：把「配没配对」写在脸上。

        配对入口就是这个窗口的「启动」（没令牌时启动即进配对），可此前状态行只有
        「未启动」——用户看不出自己配没配过、更看不出点哪里能去配对。
        """
        if self.config.get("token"):
            return f"{prefix} · 已配对（名称 {worker_id_for(self.config.get('name'))}）"
        return f"{prefix} · 未配对——点「启动」开始配对"

    def _sync_disconnect_button(self) -> None:
        """有凭证就给出口：稳态叫「断开并重新配置…」，凭证失效时叫「重新配对」。"""
        if not self.config.get("token"):
            self.btn_disconnect.pack_forget()
            return
        self.btn_disconnect.configure(
            text="重新配对" if self._auth_failed else "断开并重新配置…"
        )
        self.btn_disconnect.pack(side=tk.LEFT, padx=4)

    def _disconnect(self) -> None:
        """断开连接，并在服务端注销这枚凭证（与 Mac 转码器的「断开并重新配置」同义）。

        只删本机令牌的话，服务端那枚凭证依然有效，「设置 → 设备」里会一直挂着一台
        已经断开、却还能连上来的 Worker（Mac 端 DevicePairing 的注释专门讲过）。
        """
        # 稳态断开要确认（会丢掉配对）；凭证已经失效时没必要问——点它就是要重配
        needs_confirm = bool(self.config.get("token")) and not self._auth_failed
        if needs_confirm and not messagebox.askyesno(
            APP_TITLE,
            "断开并重新配置？\n\n"
            "这会在服务端注销这台 Worker 的授权，需要重新配对才能继续转码。\n"
            "服务器暂时连不上时只清本机，之后可以到网页「设置 → 设备」里注销它。\n"
            "服务器地址、名称、并发这些本机配置会保留。",
            default=messagebox.NO,
            icon=messagebox.WARNING,
        ):
            return
        self._restart_after_disconnect = self._auth_failed
        self._auth_failed = False
        self._disconnect_pending = True
        self._set_conn("正在断开并注销这台 Worker…")
        if self.runtime is not None:
            self._stop()
        else:
            self._finish_disconnect()

    def _finish_disconnect(self) -> None:
        """断开收尾：先尽力在服务端注销，再清本机令牌（顺序不能反——注销要用旧令牌）。"""
        self._disconnect_pending = False
        token = self.config.get("token")
        server = (self.config.get("server") or "").strip().rstrip("/")
        self.config.pop("token", None)
        save_config(self.config)
        self._sync_disconnect_button()
        if not token or not server:
            self._on_event("disconnected", {"revoked": False})
            return
        self._set_conn("正在断开：在服务端注销这枚凭证…")
        threading.Thread(
            target=self._revoke_in_background, args=(server, token), daemon=True
        ).start()

    def _revoke_in_background(self, server: str, token: str) -> None:
        # 网络调用不进 Tk 线程：注销最多等 5 秒，卡住界面比断开本身还难受
        revoked = revoke_worker_credential(server, token)
        self.ui_queue.put(("disconnected", {"revoked": revoked}))

    def _stop(self) -> None:
        if self.runtime is not None and not self.runtime.stop_event.is_set():
            self.runtime.stop_event.set()
            self._log("正在停止（在跑的分段 ffmpeg 会被杀掉，断点保留）…")

    def _toggle_autostart(self) -> None:
        bat = _startup_dir() / STARTUP_BAT_NAME
        if self.var_autostart.get():
            script = Path(__file__).resolve()
            python = Path(sys.executable).with_name("pythonw.exe")
            python = python if python.exists() else Path(sys.executable)
            bat.write_text(
                f'@echo off\r\nstart "" "{python}" "{script}" --autostart\r\n', encoding="utf-8"
            )
            # 旧版自启 bat 指向 preload_worker_gui.py：一并清掉，避免双开
            legacy = _startup_dir() / "MovieClaw 预转码 Worker.bat"
            if legacy.exists():
                legacy.unlink(missing_ok=True)
        elif bat.exists():
            bat.unlink()

    def _hide_to_tray(self) -> None:
        self.root.withdraw()

    # -- 托盘与事件泵 ----------------------------------------------------------

    def _install_tray(self) -> None:
        if sys.platform != "win32":
            return
        try:
            self.tray = TrayIcon(APP_TITLE, self.ui_queue)
        except Exception:
            self.tray = None  # 托盘失败不拦 GUI（极少数精简系统）

    def _on_runtime_event(self, kind: str, **fields: object) -> None:
        self.ui_queue.put((kind, fields))

    def _on_event(self, kind: str, fields: dict) -> None:
        """runtime 事件 → 界面。连接状态以 runtime 报告的为准，不靠猜。"""
        if kind == "log":
            self._log(fields["line"])
        elif kind == "pairing":
            self._auth_failed = False
            self._log(fields["line"])
            self._set_conn(
                f"等待配对：配对码 {fields['user_code']}（{fields['expires_in']} 秒内有效），"
                "请在浏览器批准",
                approve_url=fields["verify_url"],
            )
        elif kind == "connecting":
            self._auth_failed = False
            self._set_conn(f"连接中 {fields['server']}…")
        elif kind == "online":
            self._auth_failed = False
            self._set_conn(f"已连接 {fields['server']}")
        elif kind == "offline":
            retry = int(fields.get("retry_seconds") or 0)
            tail = f"，{retry} 秒后重试" if retry else "，正在重连"
            if fields.get("rejection") == "auth":
                self._auth_failed = True
                self._set_conn(
                    f"凭证已失效{tail}；在网页上注销过这台 Worker 的话，点「重新配对」"
                    f"（{fields['reason']}）"
                )
            else:
                self._set_conn(f"连接断开：{fields['reason']}{tail}")
        elif kind == "disconnected":
            # 服务端注销的结果：两种都要说清，别让用户以为「点了没反应」
            if fields.get("revoked"):
                self._log("已在服务端注销这枚凭证（网页「设置 → 设备」里不再有它）")
            else:
                self._log("服务端没连上，只清了本机凭证；可到网页「设置 → 设备」里注销它")
            self._set_conn(self._idle_text("已断开"))
            if self._restart_after_disconnect:
                # 凭证失效那条路：用户点的是「重新配对」，断完接着配，别让他再点一次
                self._restart_after_disconnect = False
                self._start()
        elif kind == "stopped":
            self._set_conn(self._idle_text("已停止"))
        elif kind == "error":
            self._log(f"启动失败：{fields['reason']}")
            _append_log_file(fields["trace"])
            self._set_conn(f"启动失败：{fields['reason']}")

    def _set_conn(self, text: str, approve_url: str | None = None) -> None:
        self._conn_text = text
        self._approve_url = approve_url
        self.btn_approve.configure(state=tk.NORMAL if approve_url else tk.DISABLED)
        self._sync_disconnect_button()
        self._refresh_status()

    def _refresh_status(self) -> None:
        """连接行 + 电池提示。电池条件每次刷新现算（拔电、接电都要跟上），不再只算一次。"""
        text = self._conn_text
        if self.runtime is not None and self.runtime.ac_only and on_battery():
            text += "（使用电池，预转码已暂停）"
        if text != self.lbl_connection.cget("text"):
            self.lbl_connection.configure(text=text)

    def _log(self, line: str) -> None:
        _append_log_file(line)
        self.txt_log.configure(state=tk.NORMAL)
        self.txt_log.insert(tk.END, line + "\n")
        if float(self.txt_log.index("end-1c").split(".")[0]) > 400:
            self.txt_log.delete("1.0", "100.0")  # 超过 400 行滚动裁剪
        self.txt_log.see(tk.END)
        self.txt_log.configure(state=tk.DISABLED)

    def _refresh_tasks_label(self) -> None:
        """状态栏的任务行：轮询 progress_view，显示每个在跑任务的实时进度。"""
        if self.runtime is None:
            return
        rows = task_progress_rows(self.runtime.progress_view())
        text = "任务：\n" + "\n".join(rows) if rows else "任务：—"
        if text != self.lbl_slots.cget("text"):
            self.lbl_slots.configure(text=text)

    def _poll_queue(self) -> None:
        try:
            while True:
                item = self.ui_queue.get_nowait()
                if item[0] == "restore":
                    self.root.deiconify()
                    self.root.lift()
                elif item[0] == "exit":
                    self._stop()
                    if self.tray is not None:
                        self.tray.remove()
                    # 等停止收尾（杀 ffmpeg / 断 WS）完成再关窗：直接 destroy
                    # 会把守护线程和 ffmpeg 一起撂在半路
                    self._exiting = True
                    self._exit_at = time.monotonic()
                    break
                else:
                    self._on_event(*item)
        except queue.Empty:
            pass
        # 运行态刷新
        if self.runtime is not None:
            self._refresh_status()
            self._refresh_tasks_label()
            # 运行线程收尾完成（正常停止或启动失败）：复位按钮，允许再次启动
            if self._start_thread is not None and not self._start_thread.is_alive():
                self.runtime = None
                self._start_thread = None
                self.btn_start.configure(state=tk.NORMAL)
                self.btn_stop.configure(state=tk.DISABLED)
                self.lbl_slots.configure(text="任务：—")
                if self._disconnect_pending and not self._exiting:
                    self._finish_disconnect()
        # 退出等待：收尾完成（或 30s 兜底）后真正关窗
        if self._exiting and (self.runtime is None or time.monotonic() - self._exit_at > 30):
            self.root.destroy()
            return
        self.root.after(150, self._poll_queue)


def main() -> None:
    # 单实例防呆锁要在 Tk 起来之前拿（避免窗口一闪）；撞锁时自启静默退、
    # 手动启动弹窗说明。弹窗用 Win32 MessageBox 而不是 messagebox：后者
    # 需要先初始化一个 Tk 实例，为一个提示不值当
    from single_instance import acquire_singleton_lock

    if acquire_singleton_lock() is None:
        if "--autostart" in sys.argv:
            sys.exit(0)
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            0,
            "已有 MovieClaw Worker 在运行。\n同机只需一个 Worker，本程序退出。",
            APP_TITLE,
            0x40,
        )
        sys.exit(0)
    root = tk.Tk()
    with contextlib.suppress(tk.TclError):
        ttk.Style(root).theme_use("vista")
    WorkerGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
