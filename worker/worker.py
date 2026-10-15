"""MovieClaw Windows Worker——NAS 的转码执行器（预转码 + 实时，统一调度 v2）。

独立后台程序，一个进程两种活：连 NAS 的 WS 控制面，预转码任务由服务端
推送进来（本地/远程自动调度的远程臂），播放转码任务实时接单。**不在
客户端里**：MovieClaw 客户端不含任何转码执行逻辑。

用法::

    python worker.py --server http://nas:3000 [--ffmpeg ffmpeg.exe] [--once]

- 首次运行走设备配对（浏览器开 NAS 的 /activate 页，批准 Worker 的请求）；
- 配对令牌存本地 worker.json（与配置同文件）；
- 实时任务：WS job.start → 本机 ffmpeg → 分片回传 NAS（engine.py）；
- 预转码任务：WS 推送 job.start(class=preload) → 分段转码 + REST 数据面
  （断点续转：按段对齐服务端台账 resume_parts，中断重跑自动续）；
- 图形界面用 worker_gui.py（托盘常驻，同一份配置）。

依赖：仅 Python 3.11+ 标准库（urllib），零第三方包——可直接拷到任何
Windows 机器跑，不要求装 MovieClaw 客户端。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# 允许直接以脚本方式运行（python worker/worker.py）：engine
# 与本文件同目录，加入 sys.path 保证 import 在任何 cwd 下都能命中。
sys.path.insert(0, str(Path(__file__).resolve().parent))

#: 配置文件：新名 worker.json；旧部署的 preload_worker.json 自动沿用
#: （改名迁移不做——两份并存时以旧名为准，那台机器还在用旧令牌）
_LEGACY_CONFIG_FILE = Path(__file__).with_name("preload_worker.json")
CONFIG_FILE = (
    _LEGACY_CONFIG_FILE if _LEGACY_CONFIG_FILE.exists() else Path(__file__).with_name("worker.json")
)
#: 失败上报的退避间隔（推送派单后 REST 调用失败时用）
POLL_SECONDS = 30
PROGRESS_SECONDS = 60
PART_BYTES_DEFAULT = 64 * 1024 * 1024


class Api:
    """NAS 控制面/数据面客户端（设备令牌鉴权）。"""

    def __init__(self, server: str, token: str | None) -> None:
        self.server = server.rstrip("/")
        self.token = token

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        data: bytes | None = None,
        headers: dict | None = None,
        raw: bool = False,
    ):
        if path.startswith("http"):
            url = path
        else:
            # 控制面路径带前导 /；claim 返回的 source_url/artifact_url 是
            # 协议相对路径（无前导 /）。两者都拼到 server 上
            url = f"{self.server}/{path.lstrip('/')}"
        # 每请求独立连接：段转码期间 keep-alive 会被服务端空闲超时（5s）关闭，
        # 复用死连接 PUT 大段体 → WinError 10053（2026-10-14 本地流程实测）
        head = {"Accept": "application/json", "Connection": "close"}
        if self.token and "token=" not in url:
            head["Authorization"] = f"Bearer {self.token}"
        if json_body is not None:
            data = json.dumps(json_body).encode()
            head["Content-Type"] = "application/json"
        if headers:
            head.update(headers)
        req = urllib.request.Request(url, data=data, method=method, headers=head)
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                body = resp.read()
                if raw:
                    return resp.status, body
                return resp.status, json.loads(body) if body else None
        except urllib.error.HTTPError as exc:
            body = exc.read()
            try:
                payload = json.loads(body) if body and not raw else None
            except json.JSONDecodeError:
                payload = None
            return exc.code, payload if not raw else body

    # -- 配对 ---------------------------------------------------------------

    def pair(self, name: str, *, status_cb=None) -> str:
        """设备配对流：拿配对码 → 轮询兑换（scope=transcode 凭证）。

        ``status_cb(kind, **fields)``：GUI 接管展示时传入；CLI 缺省打印。
        """
        from typing import Callable

        emit = status_cb or (lambda kind, **fields: print(fields.get("line") or f"（{kind}）"))

        def log(line: str) -> None:
            emit("log", line=line)

        status, view = self.request(
            "POST",
            "/api/v1/auth/device/authorize",
            json_body={
                "client_type": "worker",
                "client_name": name,
                "platform": "windows",
                "client_version": "1.0.0",
            },
        )
        if status != 200 or not view:
            raise RuntimeError(f"配对发起失败：{status} {view}")
        data = view["data"]
        log(f"请在浏览器打开并批准：{data['verification_uri_complete']}")
        log(f"配对码：{data['user_code']}（{data['expires_in']} 秒内有效）")
        interval = data.get("interval", 5)
        while True:
            time.sleep(interval)
            status, view = self.request(
                "POST",
                "/api/v1/auth/device/token",
                json_body={"device_code": data["device_code"]},
            )
            if status == 200 and view and view.get("data"):
                token = view["data"]["token"]
                log("配对成功。")
                return token
            if status == 202:
                continue
            raise RuntimeError(f"配对失败（{status}），请重新运行")


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------


def run_ffmpeg(ffmpeg: str, argv: list[str], workdir: Path) -> int:
    """执行 ffmpeg（argv 不含 ffmpeg 本身），返回退出码。"""
    proc = subprocess.Popen(
        [ffmpeg, *argv], cwd=workdir, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    tail: list[bytes] = []
    assert proc.stderr is not None
    for line in proc.stderr:
        tail.append(line)
        if len(tail) > 40:
            tail.pop(0)
    return proc.wait(), b"".join(tail).decode(errors="replace")


def probe_caps(ffmpeg: str) -> dict:
    """探测本机 ffmpeg 的硬件能力，随 claim 上报，服务端据此选编码链。

    - encoders：h264_nvenc 等硬件编码器；
    - filters：scale_cuda / tonemapx / tonemap_cuda 等滤镜（jellyfin-ffmpeg
      才有 tonemapx/tonemap_cuda）；
    - hw_decoders：cuda 可硬解的编码。
    探测结果缓存同目录 caps.json，换 ffmpeg 路径后自动重探。
    """
    caps_file = Path(__file__).with_name("caps.json")
    cache_key = f"{ffmpeg}|{Path(ffmpeg).stat().st_mtime_ns if Path(ffmpeg).exists() else 0}"
    if caps_file.exists():
        try:
            cached = json.loads(caps_file.read_text())
            if cached.get("key") == cache_key:
                return cached
        except (json.JSONDecodeError, OSError):
            pass
    caps: dict = {"encoders": [], "filters": [], "hw_decoders": []}
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=30).stdout
        caps["encoders"] = sorted(set(out.split()) & {"h264_nvenc", "hevc_nvenc"})
        out = subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True, timeout=30).stdout
        tokens = set(out.split())
        caps["filters"] = sorted(
            {"scale_cuda", "hwupload_cuda", "hwdownload", "tonemapx", "tonemap_cuda"} & tokens
        )
        if "h264_nvenc" in caps["encoders"]:
            caps["hw_decoders"] = ["h264", "hevc", "vp9", "av1", "mpeg2video", "vc1"]
    except (OSError, subprocess.SubprocessError):
        pass
    caps["key"] = cache_key
    try:
        caps_file.write_text(json.dumps(caps))
    except OSError:
        pass
    return caps


def process_task(api: Api, ffmpeg: str, task: dict, workdir: Path, *,
                 stop: threading.Event | None = None) -> bool:
    """执行一个任务；返回是否成功（失败已上报）。

    转码按段推进（§7 断点续转）：每段独立 ffmpeg（-ss/-t），传完一段服务端
    台账 +1；重领时 resume_parts 之前的段直接跳过——被打断只重转缺的段。
    """
    task_id = task["task_id"]
    display = task.get("display_name", f"#{task_id}")
    print(f"[{task_id}] 领到任务：{display}")
    if task.get("unusable_reason"):
        # 服务端装不出可执行的命令（如 HDR 源但本机 ffmpeg 缺 tonemapx）：
        # 不下载不转码，原文透传给服务端，网页端直接可见原因
        api.request(
            "POST",
            f"/api/v1/preload-worker/tasks/{task_id}/fail",
            json_body={"error": task["unusable_reason"]},
        )
        print(f"[{task_id}] 任务不可执行：{task['unusable_reason']}")
        return False
    workdir.mkdir(parents=True, exist_ok=True)
    source = workdir / "source.mkv"
    artifact_url = "/" + task["artifact_url"]  # claim 已带 api/v1 前缀
    segment_seconds = task.get("segment_seconds") or 0
    duration = task.get("duration_seconds") or 0
    resume = task.get("resume_parts") or 0
    base_argv = task["argv"]  # 末两个元素是 -f matroska <output>

    # 1) 取源（签名 URL；整文件下载对断线重试与 NAS 磁盘更友好）。
    # 已下载且大小与台账一致才复用；中断的半截文件直接重下
    expected = task.get("source_size_bytes") or 0
    if source.exists() and (not expected or source.stat().st_size != expected):
        source.unlink()
    if not source.exists():
        url = api.server + "/" + task["source_url"]
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=600) as resp, open(source, "wb") as out:
            while chunk := resp.read(4 * 1024 * 1024):
                out.write(chunk)
    print(f"[{task_id}] 源已就绪（{source.stat().st_size / 1024**2:.0f} MB）")

    # 2) 计算段数并跳过已收段
    if segment_seconds and duration:
        total_parts = -(-duration // segment_seconds)  # ceil
    else:
        total_parts = 1  # 直拷/单段
    print(f"[{task_id}] 共 {total_parts} 段，断点在第 {resume} 段后")

    # 3) 进度上报线程（续约）
    progress_stop = threading.Event()

    def reporter() -> None:
        while not progress_stop.wait(PROGRESS_SECONDS):
            api.request(
                "POST", f"/api/v1/preload-worker/tasks/{task_id}/progress", json_body={}
            )

    thread = threading.Thread(target=reporter, daemon=True)
    thread.start()
    try:
        for part in range(total_parts):
            if stop is not None and stop.is_set():
                print(f"[{task_id}] 收到停止信号，中断（断点保留在第 {part} 段）")
                return False
            if part < resume:
                continue  # 已在服务端，跳过
            seg_path = workdir / f"seg{part:05d}.mkv"
            seg_argv = list(base_argv)
            # argv 尾部是 [-f, matroska, output] → 输出改为本段文件
            seg_argv[-1] = str(seg_path)
            input_idx = seg_argv.index("-i")
            url_idx = input_idx + 1  # -i 的值（源文件）所在位置
            seek = part * segment_seconds
            if segment_seconds:
                # 每段都要截时长（含第 0 段！漏了会把整部片转进第一段）。
                # -ss 插在 -i 前做快速寻址；**-t 必须插在 -i 的值之后（输出
                # 选项）**：输入侧 -t 切不动字幕包，每段的 SRT 轨会带上整部片
                # 的时间戳，concat 时长直接爆表（2026-10-14 真机实测 514% 偏差）。
                # 只有末段转到 EOF
                if seek > 0:
                    seg_argv[input_idx:input_idx] = ["-ss", str(seek)]
                    url_idx += 2
                if part < total_parts - 1:
                    seg_argv[url_idx + 1:url_idx + 1] = ["-t", str(segment_seconds)]
            code, stderr_tail = run_ffmpeg(ffmpeg, seg_argv, workdir)
            if code != 0:
                api.request(
                    "POST",
                    f"/api/v1/preload-worker/tasks/{task_id}/fail",
                    json_body={"error": f"ffmpeg 退出码 {code}（第 {part + 1}/{total_parts} 段）：{stderr_tail[-600:]}"},
                )
                print(f"[{task_id}] ffmpeg 失败（{code}，第 {part + 1}/{total_parts} 段）")
                return False
            sep = "&" if "?" in artifact_url else "?"
            with open(seg_path, "rb") as piece:
                status, body = api.request(
                    "PUT",
                    f"{artifact_url}{sep}part={part}",
                    data=piece.read(),
                    headers={"Content-Type": "application/octet-stream"},
                )
            if status not in (200, 201):
                api.request(
                    "POST",
                    f"/api/v1/preload-worker/tasks/{task_id}/fail",
                    json_body={"error": f"分段 {part} 上传失败：{status} {body}"},
                )
                print(f"[{task_id}] 第 {part + 1}/{total_parts} 段上传失败：{status}")
                return False
            seg_path.unlink(missing_ok=True)
            print(f"[{task_id}] 第 {part + 1}/{total_parts} 段完成")
    finally:
        progress_stop.set()
        thread.join()

    # 4) 完成（服务端 concat + 校验 + 登记）
    status, view = api.request(
        "POST", f"/api/v1/preload-worker/tasks/{task_id}/complete", json_body={}
    )
    if status != 200:
        api.request(
            "POST",
            f"/api/v1/preload-worker/tasks/{task_id}/fail",
            json_body={"error": f"complete 被拒：{status} {view}"},
        )
        print(f"[{task_id}] complete 失败：{status}")
        return False
    print(f"[{task_id}] 完成：登记为 file #{view['data']['file_id']}")
    source.unlink(missing_ok=True)
    return True




# ---------------------------------------------------------------------------
# 运行时（GUI 与 CLI 共用）：并发槽位、电源策略、状态回调
# ---------------------------------------------------------------------------


def load_config(overrides: dict | None = None) -> dict:
    """读配置文件并应用覆盖项；写入端用 save_config。"""
    config = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
    for key, value in (overrides or {}).items():
        if value is not None:
            config[key] = value
    config.setdefault("concurrency", 1)
    config.setdefault("ac_only", False)
    config.setdefault("realtime", True)
    return config


def save_config(config: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(config, indent=2))


def on_battery() -> bool:
    """Windows：是否在用电池（未接电源）。非 Windows 恒 False。"""
    if sys.platform != "win32":
        return False
    import ctypes

    class POWERSTATUS(ctypes.Structure):
        _fields_ = [
            ("ACLineStatus", ctypes.c_byte),
            ("BatteryFlag", ctypes.c_byte),
            ("BatteryLifePercent", ctypes.c_byte),
            ("Reserved0", ctypes.c_byte),
            ("BatteryLifeTime", ctypes.c_uint32),
            ("BatteryFullLifeTime", ctypes.c_uint32),
        ]

    status = POWERSTATUS()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        return False
    return status.ACLineStatus == 0


class WorkerRuntime:
    """预转码运行时：WS 推送派单 + 电源策略 + 状态回调（v2 统一调度）。

    任务不再轮询 claim，而是随 realtime 客户端的 WS 连接推送过来
    （``class=preload`` 的 job.start），由 ``_run_preload`` 在独立线程里执行
    ``process_task``；数据面（分段上传/续约/complete/fail）照旧走 REST。

    ``status_cb(kind, **fields)`` 是 GUI/CLI 的唯一状态出口（线程安全：回调
    里只入队，不碰 Tk）。kind ∈ log/task/pairing/stopped。
    """

    def __init__(
        self,
        *,
        server: str,
        token: str | None,
        ffmpeg: str,
        name: str = "Preload Worker",
        concurrency: int = 1,
        ac_only: bool = False,
        once: bool = False,
        realtime: bool = False,
        on_token=None,  # Callable[[str], None] | None
        status_cb=None,  # Callable[..., None] | None
    ) -> None:
        self.server = server
        self.token = token
        self.ffmpeg = ffmpeg
        self.name = name
        self.concurrency = max(1, min(int(concurrency), 8))
        self.ac_only = ac_only
        self.once = once
        self.realtime = realtime
        self.on_token = on_token
        self.status_cb = status_cb or (lambda *a, **k: None)
        self.stop_event = threading.Event()
        self.api: Api | None = None
        self.caps: dict = {}
        self.tasks: dict[int, str] = {}  # slot -> display（GUI 显示当前任务）
        self._tasks_lock = threading.Lock()
        # 预转码并发闸：同时在跑的分段转码数。注意这是**本机**闸（电源/停止
        # 之外的唯一本地限制）——服务端按 max_concurrency−1 派单，这里再拦
        # 一道防派单与断线重连交错时短暂超发
        self._preload_slots = threading.Semaphore(max(1, self.concurrency - 1) or 1)
        self._preload_stop: dict[int, threading.Event] = {}
        self._realtime_client = None

    def _emit(self, kind: str, **fields: object) -> None:
        try:
            self.status_cb(kind, **fields)
        except Exception:
            pass  # 状态回调绝不能拖垮转码循环

    def log(self, line: str) -> None:
        self._emit("log", line=line)

    def ensure_token(self) -> None:
        if self.token:
            self.api = Api(self.server, self.token)
            return
        # 无令牌：先空 Api 发起配对，拿到令牌后必须把**带令牌的 Api** 装回
        # （否则槽位循环一直 401、无限重新配对——2026-10-14 本地流程实测）
        self._emit("pairing")
        api = Api(self.server, None)
        self.token = api.pair(self.name, status_cb=self._emit)
        self.api = Api(self.server, self.token)
        if self.on_token is not None:
            self.on_token(self.token)

    def start(self) -> None:
        """阻塞直到 stop_event 置位（或 --once 队列清空）。"""
        self.ensure_token()
        self.caps = probe_caps(self.ffmpeg)
        nvenc = "h264_nvenc" in self.caps["encoders"]
        self.log(
            f"Worker 已连接 {self.server}（并发 {self.concurrency}，"
            f"NVENC: {'有' if nvenc else '无'}，仅接电: {'开' if self.ac_only else '关'}"
            f"{'，实时转码: 开' if self.realtime else '，实时转码: 关（只跑预转码）'}）"
        )
        # v2 统一调度：一条 WS 连接同时报 realtime+preload 角色（GUI 勾了实时
        # 才报 realtime；预转码永远是本程序的主职）。REST 轮询 claim 已删除，
        # 任务由服务端推送。
        client = self._start_realtime()
        try:
            while not self.stop_event.is_set():
                # stop_event 只拦"还没进 ffmpeg 的下一单"；进行中的 ffmpeg
                # 由 stop()/preload 停止钩子收尾
                self.stop_event.wait(0.5)
        finally:
            client.stop()
            self._emit("stopped")

    def _start_realtime(self):
        """拉起统一 WS 客户端（实时任务 + 预转码推送派单同一条连接）。"""
        from engine import RealtimeClient

        client = RealtimeClient(
            server=self.server,
            token=self.token or "",
            worker_id=self.name,
            ffmpeg=self.ffmpeg,
            caps=self.caps,
            max_jobs=self.concurrency,
            log=lambda line: self.log(f"[WS] {line}"),
            preload=self._run_preload,
            realtime=self.realtime,
        )
        client.preload_stop = self._stop_preload_task
        self._realtime_client = client

        def watch() -> None:
            while not self.stop_event.is_set():
                client.run_forever()
                client.stop_event.clear()  # run_forever 返回后允许下一轮重连
                time.sleep(2.0)

        thread = threading.Thread(target=watch, daemon=True, name="realtime")
        thread.start()
        self.log(
            "统一调度已连接：预转码任务由服务端推送"
            + ("（同时接实时播放任务）" if self.realtime else "（实时转码未开，只跑预转码）")
        )
        return client

    def _run_preload(self, message: dict) -> bool:
        """RealtimeClient 的 class=preload 执行器：跑 process_task。

        返回是否成功（失败已通过 REST fail 上报，主循环只记日志）。
        """
        assert self.api is not None
        task = message  # 派单载荷与 claim 响应同构（dispatcher._payload_for）
        task_id = task["task_id"]
        if self.ac_only and on_battery():
            # 电源策略不满足：不上报失败（不是任务的错），服务端租约 10 分钟
            # 后自动回队。本地记一条，GUI 能看到为什么没在转
            self.log(f"[{task_id}] 使用电池中，跳过执行（任务将回队）")
            return False
        with self._preload_slots:
            if self.stop_event.is_set():
                return False
            stop = threading.Event()
            with self._tasks_lock:
                self._preload_stop[task_id] = stop
                self.tasks[task_id] = task.get("display_name", f"#{task_id}")
            self._emit(
                "task", slot=task_id, task_id=task_id,
                name=task.get("display_name", f"#{task_id}"), phase="claimed",
            )
            try:
                # 服务端抢占（job.stop class=preload）置位 stop：process_task
                # 在段间检查，断点台账在服务端，重派自动续转
                stop.wait(0)  # 占位让 stop 变量在闭包里可见
                ok = process_task(
                    self.api, self.ffmpeg, task,
                    Path(__file__).parent / "work" / str(task_id),
                    stop=stop,
                )
                self._emit(
                    "task", slot=task_id, task_id=task_id,
                    name=task.get("display_name", f"#{task_id}"),
                    phase="done" if ok else "failed",
                )
                return ok
            except Exception as exc:  # 任何意外：上报失败，继续接下一单
                import traceback

                self.log(f"[{task_id}] 异常：{exc}")
                self.log(traceback.format_exc()[-900:])
                self.api.request(
                    "POST",
                    f"/api/v1/preload-worker/tasks/{task_id}/fail",
                    json_body={"error": str(exc)[:2000]},
                )
                return False
            finally:
                with self._tasks_lock:
                    self.tasks.pop(task_id, None)
                    self._preload_stop.pop(task_id, None)

    def _stop_preload_task(self, task_id) -> None:
        """服务端抢占时停掉对应任务的 ffmpeg（断点保留在服务端台账）。"""
        with self._tasks_lock:
            stop = self._preload_stop.get(task_id)
        if stop is not None:
            stop.set()


def main() -> None:
    parser = argparse.ArgumentParser(description="MovieClaw Windows Worker（预转码+实时统一调度）")
    parser.add_argument("--server", help="NAS 地址；缺省读配置文件")
    parser.add_argument("--ffmpeg", help="ffmpeg 可执行文件路径；缺省读配置文件")
    parser.add_argument("--name", help="配对显示名；缺省读配置文件")
    parser.add_argument("--concurrency", type=int, help="并发槽位数 1-8")
    parser.add_argument("--ac-only", action="store_true", help="仅接通电源时执行")
    parser.add_argument(
        "--realtime",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="是否接实时转码（默认开；--no-realtime 只跑预转码）",
    )
    parser.add_argument("--once", action="store_true", help="处理完队列里现有任务后退出")
    parser.add_argument("--reset-token", action="store_true", help="丢弃已存令牌重新配对")
    parser.add_argument("--autostart", action="store_true", help="开机自启调用；已有实例时静默退出")
    args = parser.parse_args()

    # 单实例防呆锁：同机第二个 Worker 只会互顶 WS 连接、抢同一块 GPU
    from single_instance import acquire_singleton_lock

    if acquire_singleton_lock() is None:
        if not args.autostart:
            print("已有 MovieClaw Worker 在运行，本进程退出（同机只需一个 Worker）。")
        sys.exit(0)

    config = load_config({
        "server": args.server,
        "ffmpeg": args.ffmpeg,
        "name": args.name,
        "concurrency": args.concurrency,
    })
    if args.ac_only:
        config["ac_only"] = True
    if args.realtime is not None:
        config["realtime"] = args.realtime
    if args.reset_token:
        config.pop("token", None)
    save_config(config)

    runtime = WorkerRuntime(
        server=config["server"],
        token=config.get("token"),
        ffmpeg=config.get("ffmpeg") or "ffmpeg",
        name=config.get("name") or "Preload Worker",
        concurrency=config.get("concurrency") or 1,
        ac_only=bool(config.get("ac_only")),
        once=args.once,
        realtime=bool(config.get("realtime")),
        on_token=lambda token: save_config({**config, "token": token}),
        status_cb=lambda kind, **fields: print(fields.get("line") or f"（{kind}）"),
    )
    try:
        runtime.start()
    except KeyboardInterrupt:
        print("\n已停止。")
        sys.exit(0)


if __name__ == "__main__":
    main()
