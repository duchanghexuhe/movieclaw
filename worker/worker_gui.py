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

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from worker import (  # noqa: E402
    WorkerRuntime,
    load_config,
    on_battery,
    save_config,
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

    def __init__(self, tooltip: str, actions: "queue.Queue[tuple]") -> None:
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
            nid = self._ct.windll.user32
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
        self.ui_queue: "queue.Queue[tuple]" = queue.Queue()
        self.tray: TrayIcon | None = None
        self._build_ui()
        self._load_config_to_ui()
        root.protocol("WM_DELETE_WINDOW", self._hide_to_tray)
        self._install_tray()
        root.after(120, self._poll_queue)

    # -- UI 构建 -------------------------------------------------------------

    def _build_ui(self) -> None:
        pad = {"padx": 10, "pady": 6}
        frame = ttk.Frame(self.root)
        frame.pack(fill=tk.BOTH, expand=True)

        status = ttk.LabelFrame(frame, text="状态")
        status.pack(fill=tk.X, **pad)
        self.lbl_connection = ttk.Label(status, text="未启动")
        self.lbl_connection.grid(row=0, column=0, columnspan=2, sticky=tk.W, padx=8, pady=4)
        self.lbl_slots = ttk.Label(status, text="任务：—")
        self.lbl_slots.grid(row=1, column=0, columnspan=2, sticky=tk.W, padx=8, pady=(0, 6))

        config = ttk.LabelFrame(frame, text="配置")
        config.pack(fill=tk.X, **pad)
        ttk.Label(config, text="服务器").grid(row=0, column=0, sticky=tk.E, padx=6, pady=3)
        self.var_server = tk.StringVar()
        ttk.Entry(config, textvariable=self.var_server, width=52).grid(row=0, column=1, sticky=tk.W)
        ttk.Label(config, text="ffmpeg").grid(row=1, column=0, sticky=tk.E, padx=6, pady=3)
        self.var_ffmpeg = tk.StringVar()
        entry_ff = ttk.Entry(config, textvariable=self.var_ffmpeg, width=52)
        entry_ff.grid(row=1, column=1, sticky=tk.W)
        ttk.Button(config, text="浏览…", width=8,
                   command=self._browse_ffmpeg).grid(row=1, column=2, padx=4)
        ttk.Label(config, text="并发").grid(row=2, column=0, sticky=tk.E, padx=6, pady=3)
        self.var_concurrency = tk.IntVar(value=1)
        ttk.Spinbox(config, from_=1, to=8, width=5, textvariable=self.var_concurrency).grid(
            row=2, column=1, sticky=tk.W)
        ttk.Label(
            config, text="（播放转码优先，预转码占并发−1）",
            foreground="#666",
        ).grid(row=2, column=1, sticky=tk.W, padx=(52, 0))
        self.var_ac_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(config, text="仅接通电源时执行", variable=self.var_ac_only).grid(
            row=2, column=2, sticky=tk.W)
        self.var_realtime = tk.BooleanVar(value=True)
        ttk.Checkbutton(config, text="接实时转码（边播边转；取消则只跑预转码）",
                        variable=self.var_realtime).grid(
            row=3, column=0, columnspan=2, sticky=tk.W, padx=(6, 0))
        self.var_autostart = tk.BooleanVar(value=False)
        ttk.Checkbutton(config, text="开机自启", variable=self.var_autostart,
                        command=self._toggle_autostart).grid(row=3, column=2, sticky=tk.W)

        buttons = ttk.Frame(frame)
        buttons.pack(fill=tk.X, **pad)
        self.btn_start = ttk.Button(buttons, text="启动", command=self._start)
        self.btn_start.pack(side=tk.LEFT, padx=(10, 4))
        self.btn_stop = ttk.Button(buttons, text="停止", command=self._stop, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=4)

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
        self.var_ffmpeg.set(self.config.get("ffmpeg") or "")
        self.var_concurrency.set(int(self.config.get("concurrency") or 1))
        self.var_ac_only.set(bool(self.config.get("ac_only")))
        # v2 默认开实时（统一调度主推形态）；老配置里明确存过 false 的尊重原值
        self.var_realtime.set(
            bool(self.config.get("realtime", True)) if "realtime" in self.config else True
        )
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
            "ffmpeg": self.var_ffmpeg.get().strip(),
            "concurrency": max(1, min(int(self.var_concurrency.get() or 1), 8)),
            "ac_only": bool(self.var_ac_only.get()),
            "realtime": bool(self.var_realtime.get()),
        }

    def _start(self) -> None:
        if self.runtime is not None:
            return
        config = self._collect_config()
        if not config["server"]:
            messagebox.showwarning(APP_TITLE, "请先填服务器地址（如 http://192.168.31.10:8096）")
            return
        save_config({**self.config, **config})
        self.config = load_config()
        runtime = WorkerRuntime(
            server=config["server"],
            token=self.config.get("token"),
            ffmpeg=config["ffmpeg"] or "ffmpeg",
            name=self.config.get("name") or "Preload Worker",
            concurrency=config["concurrency"],
            ac_only=config["ac_only"],
            realtime=config["realtime"],
            on_token=lambda token: save_config({**self.config, "token": token}),
            status_cb=self._on_runtime_event,
        )
        self.runtime = runtime
        self.btn_start.configure(state=tk.DISABLED)
        self.btn_stop.configure(state=tk.NORMAL)
        self.lbl_connection.configure(text="连接中…")
        self._start_thread = threading.Thread(target=runtime.start, daemon=True)
        self._start_thread.start()

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

    def _log(self, line: str) -> None:
        self.txt_log.configure(state=tk.NORMAL)
        self.txt_log.insert(tk.END, line + "\n")
        if float(self.txt_log.index("end-1c").split(".")[0]) > 400:
            self.txt_log.delete("1.0", "100.0")  # 超过 400 行滚动裁剪
        self.txt_log.see(tk.END)
        self.txt_log.configure(state=tk.DISABLED)

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
                    kind, fields = item
                    if kind == "log":
                        self._log(fields["line"])
                    elif kind == "pairing":
                        self.lbl_connection.configure(text="等待配对：请在浏览器批准配对码")
                    elif kind == "task":
                        name = fields.get("name") or ""
                        phase = fields.get("phase") or ""
                        suffix = {
                            "claimed": "（转码中）", "done": "（完成）", "failed": "（失败）",
                        }.get(phase, "")
                        if phase == "claimed":
                            self.lbl_slots.configure(text=f"任务：槽 {fields.get('slot')} → {name} {suffix}")
                        else:
                            self.lbl_slots.configure(text=f"任务：{name} {suffix}")
        except queue.Empty:
            pass
        # 运行态刷新
        if self.runtime is not None:
            if self.runtime.token and self.lbl_connection["text"].startswith(("未启动", "连接中")):
                self.lbl_connection.configure(
                    text=f"已连接 {self.runtime.server}"
                         + ("（使用电池，已暂停）" if self.runtime.ac_only and on_battery() else "")
                )
            # 停止收尾完成：复位按钮与标签，允许再次启动
            if (
                self.runtime.stop_event.is_set()
                and self._start_thread is not None
                and not self._start_thread.is_alive()
            ):
                self.runtime = None
                self._start_thread = None
                self.btn_start.configure(state=tk.NORMAL)
                self.btn_stop.configure(state=tk.DISABLED)
                self.lbl_connection.configure(text="已停止")
        # 退出等待：收尾完成（或 30s 兜底）后真正关窗
        if self._exiting:
            if self.runtime is None or time.monotonic() - self._exit_at > 30:
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
    try:
        ttk.Style(root).theme_use("vista")
    except tk.TclError:
        pass
    WorkerGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
