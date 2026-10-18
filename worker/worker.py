"""MovieClaw Windows Worker——NAS 的转码执行器（预转码 + 实时，统一调度 v2）。

独立后台程序，一个进程两种活：连 NAS 的 WS 控制面，预转码任务由服务端
推送进来（本地/远程自动调度的远程臂），播放转码任务实时接单。**不在
客户端里**：MovieClaw 客户端不含任何转码执行逻辑。

用法::

    python worker.py --server http://nas:3000 [--ffmpeg ffmpeg.exe]

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
import contextlib
import json
import os
import re
import shutil
import socket
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
#: 预转码的本地工作目录（源文件与分段）：取消时整目录删掉，让位时原样保留
WORK_DIR = Path(__file__).with_name("work")
PROGRESS_SECONDS = 60
PART_BYTES_DEFAULT = 64 * 1024 * 1024
# ffmpeg 子进程不弹控制台窗：pythonw 启动时子进程无可继承的控制台，
# 不加这个每段 ffmpeg 都会自建黑窗。常量仅 Windows 有，其它平台取 0
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: 内网 NAS 地址直连 opener：urllib 默认读进程环境里的 http_proxy——挂机的
#: 机器带着 Clash 等代理环境变量时，控制面 REST 与大文件传输会被绕去代理
#:（实测内网吞吐掉到 1.7MB/s，2026-10-15 真机）。与 engine.py 的
#: _NO_PROXY_OPENER 同一取缔：全部内网请求显式直连。
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


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
        # 控制面路径带前导 /；claim 返回的 source_url/artifact_url 是
        # 协议相对路径（无前导 /）。两者都拼到 server 上
        url = path if path.startswith("http") else f"{self.server}/{path.lstrip('/')}"
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
            # 显式直连（见 _NO_PROXY_OPENER）：预转码的大文件上行/下行同实时
            # 一样不能被代理环境变量劫持
            with _NO_PROXY_OPENER.open(req, timeout=600) as resp:
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

    def pair(self, name: str, *, status_cb=None, stop: threading.Event | None = None) -> str:
        """设备配对流：拿配对码 → 轮询兑换（scope=transcode 凭证）。

        ``status_cb(kind, **fields)``：GUI 接管展示时传入；CLI 缺省打印。配对码以
        ``pairing`` 事件交出（GUI 据此显示配对码并提供「打开批准页」）。``stop`` 置位即
        取消——点了「停止」就不能干等到配对码过期。
        """

        emit = status_cb or (lambda kind, **fields: print(fields.get("line") or f"（{kind}）"))

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
        emit(
            "pairing",
            user_code=data["user_code"],
            verify_url=data["verification_uri_complete"],
            expires_in=data["expires_in"],
            line=(
                f"请在浏览器打开并批准：{data['verification_uri_complete']}"
                f"（配对码 {data['user_code']}，{data['expires_in']} 秒内有效）"
            ),
        )
        wait = (stop or threading.Event()).wait
        interval = data.get("interval", 5)
        while True:
            if wait(interval):
                raise RuntimeError("已取消配对")
            status, view = self.request(
                "POST",
                "/api/v1/auth/device/token",
                json_body={"device_code": data["device_code"]},
            )
            if status == 200 and view and view.get("data"):
                token = view["data"]["token"]
                emit("log", line="配对成功。")
                return token
            if status == 202:
                continue
            if status == 429:
                # SLOW_DOWN：OAuth 设备流约定客户端放慢轮询（+5s），不退避
                # 会一直撞到配对码过期
                interval = min(interval + 5, 30)
                continue
            raise RuntimeError(f"配对失败（{status}），请重新运行")


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------


def run_ffmpeg(
    ffmpeg: str,
    argv: list[str],
    workdir: Path,
    *,
    stop: threading.Event | None = None,
    register=None,  # Callable[[subprocess.Popen], None] | None
) -> int:
    """执行 ffmpeg（argv 不含 ffmpeg 本身），返回退出码。

    ``stop`` 是停止钩子：置位后立即杀掉在跑的进程（抢占/取消/退出不等
    600s 的段跑完）。``register`` 在进程起来时交出句柄——调用方据此能在
    停止路径之外（进程退出兜底）杀掉它。
    """
    proc = subprocess.Popen(
        [ffmpeg, *argv], cwd=workdir, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        creationflags=_NO_WINDOW,
    )
    if register is not None:
        register(proc)
    if stop is not None:
        def watchdog() -> None:
            stop.wait()
            with contextlib.suppress(OSError):
                proc.kill()

        threading.Thread(target=watchdog, name="ff-stop", daemon=True).start()
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
    # v2：key 带字段 schema 版本——新增上报字段（ffmpeg_version/hardware）时
    # 存量部署的旧缓存必须失效重探，否则 hello 的新字段恒空
    cache_key = (
        f"v2|{ffmpeg}|{Path(ffmpeg).stat().st_mtime_ns if Path(ffmpeg).exists() else 0}"
    )
    if caps_file.exists():
        try:
            cached = json.loads(caps_file.read_text())
            if cached.get("key") == cache_key:
                return cached
        except (json.JSONDecodeError, OSError):
            pass
    caps: dict = {"encoders": [], "filters": [], "hw_decoders": []}
    try:
        probe = subprocess.run(
            [ffmpeg, "-hide_banner", "-version"], capture_output=True, text=True,
            timeout=15, creationflags=_NO_WINDOW,
        )
        out = probe.stdout
        first = (out.splitlines() or [""])[0]
        if first.lower().startswith("ffmpeg version"):
            # hello 的 ffmpeg_version 字段：状态页 Worker 列表按它展示
            caps["ffmpeg_version"] = first[len("ffmpeg version "):].split()[0]
        caps["hardware"] = {"cpu_cores": os.cpu_count() or 0, "memory_bytes": _total_memory()}
        out = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True,
            timeout=30, creationflags=_NO_WINDOW,
        ).stdout
        caps["encoders"] = sorted(set(out.split()) & {"h264_nvenc", "hevc_nvenc"})
        out = subprocess.run(
            [ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True,
            timeout=30, creationflags=_NO_WINDOW,
        ).stdout
        tokens = set(out.split())
        caps["filters"] = sorted(
            {"scale_cuda", "hwupload_cuda", "hwdownload", "tonemapx", "tonemap_cuda"} & tokens
        )
        if "h264_nvenc" in caps["encoders"]:
            caps["hw_decoders"] = ["h264", "hevc", "vp9", "av1", "mpeg2video", "vc1"]
    except (OSError, subprocess.SubprocessError):
        pass
    caps["key"] = cache_key
    with contextlib.suppress(OSError):
        caps_file.write_text(json.dumps(caps))
    return caps


def check_ffmpeg(ffmpeg: str) -> str | None:
    """点「启动」时校验 ffmpeg：跑一次 ``-version``。None = 可用，否则是给人看的原因。

    probe_caps 对失败是静默的（探测不到能力照常上线），不在这里拦住的话，用户只会看到
    任务一单单失败、却不知道是 ffmpeg 没找到。
    """
    try:
        subprocess.run(
            [ffmpeg, "-hide_banner", "-version"], capture_output=True, timeout=10,
            creationflags=_NO_WINDOW, check=True,
        )
    except FileNotFoundError:
        return f"找不到 ffmpeg：{ffmpeg}。请在配置里指定 ffmpeg.exe 的路径（推荐 jellyfin-ffmpeg）"
    except subprocess.CalledProcessError as exc:
        return f"ffmpeg 无法运行（退出码 {exc.returncode}）：{ffmpeg}"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"ffmpeg 无法运行：{exc}"
    return None


def _total_memory() -> int:
    """物理内存字节数（Windows GlobalMemoryStatusEx；其他平台 0=未知）。"""
    if sys.platform != "win32":
        return 0
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return 0
    return int(status.ullTotalPhys)


def process_task(
    api: Api,
    ffmpeg: str,
    task: dict,
    workdir: Path,
    *,
    stop: threading.Event | None = None,
    register=None,  # Callable[[subprocess.Popen], None] | None
    progress: dict | None = None,
) -> bool:
    """执行一个任务；返回是否成功（失败已上报）。

    ``progress`` 是调用方的进度台账（如 WorkerRuntime 传给 GUI 轮询的
    dict）：本函数原地更新 phase/段位/ETA，调用方随时可读；不传则自用。

    转码按段推进（§7 断点续转）：每段独立 ffmpeg（-ss/-t），传完一段服务端
    台账 +1；重领时 resume_parts 之前的段直接跳过——被打断只重转缺的段。
    停止钩子（``stop``）置位后的所有失败都**不上报**：中断不是失败，断点
    台账保留，租约到期自动回队。
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

    # 1) 进度上报线程（续约 + 活动页实时进度）：共享 dict 主线程更新、
    #    上报线程快照发送（CPython dict 读写在 GIL 下原子，不需要锁）。
    #    先于下载启动——慢网络下大源下载也要续约，下载进度活动页才看得到
    if progress is None:
        progress = {}
    seg_times: list[float] = []
    progress_stop = threading.Event()

    def reporter() -> None:
        while not progress_stop.wait(PROGRESS_SECONDS):
            api.request(
                "POST",
                f"/api/v1/preload-worker/tasks/{task_id}/progress",
                json_body=dict(progress),
            )

    thread = threading.Thread(target=reporter, daemon=True)
    thread.start()
    try:
        # 2) 取源（签名 URL；整文件下载对断线重试与 NAS 磁盘更友好）。
        # 已下载且大小与台账一致才复用；中断的半截文件直接重下。
        # 下载带重试：17GB 源一次网络抖动就整任务 FAILED、断点归零，太脆
        expected = task.get("source_size_bytes") or 0
        if source.exists() and (not expected or source.stat().st_size != expected):
            source.unlink()
        if not source.exists():
            url = api.server + "/" + task["source_url"]
            progress.update(phase="downloading", downloaded_bytes=0, total_bytes=expected)
            last_error: Exception | None = None
            for attempt in range(3):
                if stop is not None and stop.is_set():
                    print(f"[{task_id}] 停止中断于下载（断点保留）")
                    return False
                try:
                    req = urllib.request.Request(url)
                    with _NO_PROXY_OPENER.open(req, timeout=600) as resp, open(source, "wb") as out:
                        received = 0
                        while chunk := resp.read(4 * 1024 * 1024):
                            out.write(chunk)
                            received += len(chunk)
                            progress["downloaded_bytes"] = received
                    last_error = None
                    break
                except (urllib.error.URLError, OSError) as exc:
                    last_error = exc
                    source.unlink(missing_ok=True)
                    progress["downloaded_bytes"] = 0
                    print(f"[{task_id}] 源下载失败（第 {attempt + 1}/3 次）：{exc}")
                    time.sleep(2 * (attempt + 1))
            if last_error is not None:
                api.request(
                    "POST",
                    f"/api/v1/preload-worker/tasks/{task_id}/fail",
                    json_body={"error": f"源文件下载失败（重试 3 次）：{last_error}"},
                )
                return False
        progress.update(phase="transcoding", downloaded_bytes=expected, total_bytes=expected)

        # 3) 计算段数并跳过已收段
        # 向上取整；无分段（直拷/单段）就是 1 段
        total_parts = -(-duration // segment_seconds) if segment_seconds and duration else 1
        progress["segment_total"] = total_parts
        print(f"[{task_id}] 共 {total_parts} 段，断点在第 {resume} 段后")

        for part in range(total_parts):
            if stop is not None and stop.is_set():
                print(f"[{task_id}] 收到停止信号，中断（断点保留在第 {part} 段）")
                return False
            if part < resume:
                continue  # 已在服务端，跳过
            progress["segment_index"] = part + 1  # 正在转的第几段（1 起）
            seg_started = time.monotonic()
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
            code, stderr_tail = run_ffmpeg(
                ffmpeg, seg_argv, workdir, stop=stop, register=register
            )
            if code != 0:
                if stop is not None and stop.is_set():
                    # 停止中断（抢占/取消/退出）杀掉了 ffmpeg：不是任务失败，
                    # 不上报——断点台账保留，租约到期自动回队
                    print(f"[{task_id}] 停止中断在第 {part + 1} 段（断点保留）")
                    return False
                api.request(
                    "POST",
                    f"/api/v1/preload-worker/tasks/{task_id}/fail",
                    json_body={
                        "error": (
                            f"ffmpeg 退出码 {code}（第 {part + 1}/{total_parts} 段）："
                            f"{stderr_tail[-600:]}"
                        )
                    },
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
                if stop is not None and stop.is_set():
                    print(f"[{task_id}] 停止中断于第 {part + 1} 段上传（断点保留）")
                    return False
                if status == 400:
                    # 任务已不在执行态（取消/被重领）：本地半成品一并清掉，
                    # 源文件与分段不会有人再来续写
                    shutil.rmtree(workdir, ignore_errors=True)
                api.request(
                    "POST",
                    f"/api/v1/preload-worker/tasks/{task_id}/fail",
                    json_body={"error": f"分段 {part} 上传失败：{status} {body}"},
                )
                print(f"[{task_id}] 第 {part + 1}/{total_parts} 段上传失败：{status}")
                return False
            seg_path.unlink(missing_ok=True)
            seg_times.append(time.monotonic() - seg_started)
            remaining = total_parts - (part + 1)
            if remaining > 0:
                # 预计剩余：已完成段的平均耗时 × 剩余段数
                progress["eta_seconds"] = int(sum(seg_times) / len(seg_times) * remaining)
            print(f"[{task_id}] 第 {part + 1}/{total_parts} 段完成")
        # 段全部传完：拼接+校验+登记由服务端做（约 1 分钟），UI 标收尾
        progress["phase"] = "finalizing"
        progress["eta_seconds"] = 0
    finally:
        progress_stop.set()
        thread.join()

    # 4) 完成（服务端 concat + 校验 + 登记）
    status, view = api.request(
        "POST", f"/api/v1/preload-worker/tasks/{task_id}/complete", json_body={}
    )
    if status != 200:
        if stop is not None and stop.is_set():
            print(f"[{task_id}] 停止中断于 complete（断点保留）")
            return False
        if status == 400:
            shutil.rmtree(workdir, ignore_errors=True)  # 任务已终态：本地半成品一并清
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
    return config


def save_config(config: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(config, indent=2))


def worker_id_for(name: str | None) -> str:
    """WS hello 的 worker_id。服务端只认 ``[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}``（_WORKER_ID）。

    配置里填过 name 就沿用它（老部署的 id 不变）；没填取本机主机名——每台机器各一个，
    两台同名 Worker 会在服务端按 worker_id 互相顶号（1012）。非法字符换成 "-"；
    净化后什么都不剩（如纯中文名）就退到主机名。
    """
    for raw in (name, socket.gethostname()):
        clean = re.sub(r"[^A-Za-z0-9_.:-]+", "-", raw or "")[:64].strip("-_.:")
        if clean:
            return clean
    return "windows-worker"


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


#: SetThreadExecutionState 的标志（WinBase.h）；ES_CONTINUOUS 让要求保持到下一次调用收回
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def keep_system_awake(on: bool) -> None:
    """Windows：``on`` 时挡住系统睡眠，否则释放（Mac 转码器用 beginActivity 做的就是这件事）。

    要求挂在调用线程上，所以只由 WorkerRuntime.start() 的主循环调用——那条线程活到收尾，
    不在任务线程里调，免得任务线程退出后要求悬空。非 Windows 无操作。
    """
    if sys.platform != "win32":
        return
    import ctypes

    flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
    ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint32(flags))


class WorkerRuntime:
    """预转码运行时：WS 推送派单 + 电源策略 + 状态回调（v2 统一调度）。

    任务不再轮询 claim，而是随 realtime 客户端的 WS 连接推送过来
    （``class=preload`` 的 job.start），由 ``_run_preload`` 在独立线程里执行
    ``process_task``；数据面（分段上传/续约/complete/fail）照旧走 REST。

    ``status_cb(kind, **fields)`` 是 GUI/CLI 的唯一状态出口（线程安全：回调
    里只入队，不碰 Tk）。kind：log（line）；连接状态 connecting / online /
    offline（reason、retry_seconds）；pairing（user_code、verify_url、expires_in）；
    stopped。
    """

    def __init__(
        self,
        *,
        server: str,
        token: str | None,
        ffmpeg: str,
        name: str | None = None,
        concurrency: int = 1,
        ac_only: bool = False,
        on_token=None,  # Callable[[str], None] | None
        status_cb=None,  # Callable[..., None] | None
    ) -> None:
        self.server = server
        self.token = token
        self.ffmpeg = ffmpeg
        # name 是配对时给人看的设备名；worker_id 是 WS hello 里的机器标识（见 worker_id_for）
        self.name = name or f"Preload Worker（{socket.gethostname()}）"[:64]
        self.worker_id = worker_id_for(name)
        self.concurrency = max(1, min(int(concurrency), 8))
        self.ac_only = ac_only
        self.on_token = on_token
        self.status_cb = status_cb or (lambda *a, **k: None)
        self.stop_event = threading.Event()
        self.api: Api | None = None
        self.caps: dict = {}
        self.tasks: dict[int, str] = {}  # task_id -> display（GUI 显示当前任务）
        self._tasks_lock = threading.Lock()
        # 在跑任务的进度台账（task_id -> process_task 原地更新的 dict）：
        # progress_view() 给 GUI 状态栏轮询
        self._progress: dict[int, dict] = {}
        # 预转码并发闸：同时在跑的分段转码数，空闲时用满并发。播放优先——实时任务一进来
        # 在跑的预转码全部让位（不占这里的位）；服务端按余量派单，这里再拦一道防竞态
        self._preload_slots = threading.Semaphore(self.concurrency)
        self._preload_stop: dict[int, threading.Event] = {}
        # 服务端取消（job.stop reason=cancel）的任务：收尾时删掉本地工作目录
        self._preload_cancelled: set[int] = set()
        # 在跑分段 ffmpeg 的句柄（task_id → 最近一段的进程）：停止/退出路径
        # 用来立即杀进程，不等 600s 的段自然跑完
        self._preload_procs: dict[int, subprocess.Popen] = {}
        self._realtime_client = None

    def _emit(self, kind: str, **fields: object) -> None:
        # 状态回调绝不能拖垮转码循环
        with contextlib.suppress(Exception):
            self.status_cb(kind, **fields)

    def log(self, line: str) -> None:
        self._emit("log", line=line)

    def ensure_token(self) -> None:
        if self.token:
            self.api = Api(self.server, self.token)
            return
        # 无令牌：先空 Api 发起配对，拿到令牌后必须把**带令牌的 Api** 装回
        # （否则槽位循环一直 401、无限重新配对——2026-10-14 本地流程实测）
        api = Api(self.server, None)
        self.token = api.pair(self.name, status_cb=self._emit, stop=self.stop_event)
        self.api = Api(self.server, self.token)
        if self.on_token is not None:
            self.on_token(self.token)

    def start(self) -> None:
        """阻塞直到 stop_event 置位；配对或启动失败时抛异常（GUI 线程负责展示）。"""
        client = None
        try:
            self.ensure_token()
            self.caps = probe_caps(self.ffmpeg)
            nvenc = "h264_nvenc" in self.caps["encoders"]
            self.log(
                f"Worker 启动：服务器 {self.server}（并发 {self.concurrency}，"
                f"NVENC: {'有' if nvenc else '无'}，仅接电: {'开' if self.ac_only else '关'}）"
            )
            # v2 统一调度：一条 WS 连接同时报 realtime+preload 角色（预转码与实时
            # 播放都是本程序的主职）。REST 轮询 claim 已删除，任务由服务端推送。
            client = self._start_realtime()
            awake = False
            while not self.stop_event.is_set():
                # 有任务在跑就挡住睡眠，空闲即释放（每半秒看一次）
                busy = self._busy()
                if busy != awake:
                    keep_system_awake(busy)
                    awake = busy
                self.stop_event.wait(0.5)
        finally:
            keep_system_awake(False)
            if client is not None:
                client.stop()
            # 退出不留后事：杀掉所有在跑的分段 ffmpeg（否则进程退了、ffmpeg
            # 成孤儿继续烧 GPU，还锁着源文件），等任务线程收尾
            self._stop_all_preloads()
            deadline = time.time() + 15
            while time.time() < deadline:
                with self._tasks_lock:
                    if not self._preload_stop:
                        break
                time.sleep(0.2)
            self._emit("stopped")

    def _stop_all_preloads(self) -> None:
        """置位所有预转码停止钩子并杀掉在跑的 ffmpeg。"""
        with self._tasks_lock:
            stops = list(self._preload_stop.values())
            procs = list(self._preload_procs.values())
        for stop in stops:
            stop.set()
        for proc in procs:
            with contextlib.suppress(OSError):
                proc.kill()

    def _busy(self) -> bool:
        """有实时或预转码任务正在跑（此刻要挡住系统睡眠）。"""
        with self._tasks_lock:
            if self.tasks:
                return True
        return self._realtime_busy()

    def _realtime_busy(self) -> bool:
        """本机有实时任务（播放转码、离线导出）在跑：预转码此刻一个都不该跑。"""
        client = self._realtime_client
        if client is None:
            return False
        with client.jobs_lock:
            return bool(client.jobs)

    def _start_realtime(self):
        """拉起统一 WS 客户端（实时任务 + 预转码推送派单同一条连接）。"""
        from engine import RealtimeClient

        client = RealtimeClient(
            server=self.server,
            token=self.token or "",
            worker_id=self.worker_id,
            ffmpeg=self.ffmpeg,
            caps=self.caps,
            max_jobs=self.concurrency,
            log=lambda line: self.log(f"[WS] {line}"),
            preload=self._run_preload,
            status=self._emit,
        )
        client.preload_stop = self._stop_preload_task
        # 实时任务进入时让位钩子：本机预转码占用超出「并发 − 实时任务数」的
        # 部分立即停掉（断点台账在服务端，租约到期回队续转）
        client.preload_yield = self._yield_preload_slots
        self._realtime_client = client

        def watch() -> None:
            while not self.stop_event.is_set():
                client.run_forever()
                client.stop_event.clear()  # run_forever 返回后允许下一轮重连
                time.sleep(2.0)

        thread = threading.Thread(target=watch, daemon=True, name="realtime")
        thread.start()
        self.log("统一调度已连接：预转码+实时播放任务均由服务端推送")
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
        if self._realtime_busy():
            # 播放优先：推送与播放抢位的竞态（推送时还没看到这台在播）——当场让位回队
            self.log(f"[{task_id}] 播放中，预转码让位（回队）")
            self._realtime_client.send_preload_yielded(task_id)
            return False
        # 停止钩子**先注册再等槽位**：等槽位期间服务端取消/让位经
        # _stop_preload_task 才找得到这个任务（查不到的叫停会被静默丢掉）
        stop = threading.Event()
        with self._tasks_lock:
            if task_id in self._preload_stop:
                # 同一任务重复派单（服务端重启/租约回收后重派到同一台机）：
                # 本机已有线程在跑，这条直接丢弃——跑着的那条会持续续约并
                # 最终 complete；起第二个线程会双写同一个源文件
                self.log(f"[{task_id}] 任务已在本机执行，忽略重复派单")
                return False
            self._preload_stop[task_id] = stop
        try:
            return self._run_preload_locked(task, task_id, stop)
        finally:
            with self._tasks_lock:
                self.tasks.pop(task_id, None)
                self._progress.pop(task_id, None)
                if self._preload_stop.get(task_id) is stop:
                    self._preload_stop.pop(task_id, None)
                self._preload_procs.pop(task_id, None)
                cancelled = task_id in self._preload_cancelled
                self._preload_cancelled.discard(task_id)
            if cancelled:
                # 服务端取消：源文件与分段（几十 GB）一并删掉。让位不走这里，断点原样留着
                shutil.rmtree(WORK_DIR / str(task_id), ignore_errors=True)

    def _run_preload_locked(self, task: dict, task_id: int, stop: threading.Event) -> bool:
        with self._preload_slots:
            if self.stop_event.is_set() or stop.is_set():
                return False
            with self._tasks_lock:
                self.tasks[task_id] = task.get("display_name", f"#{task_id}")
                # 进度台账先于 process_task 登记：下载阶段状态栏就有进度可看
                progress: dict = {}
                self._progress[task_id] = progress

            def _register_proc(proc: subprocess.Popen) -> None:
                with self._tasks_lock:
                    self._preload_procs[task_id] = proc

            try:
                # 服务端抢占（job.stop class=preload）置位 stop：run_ffmpeg 的
                # 停止看门狗立即杀掉在跑的分段，断点台账在服务端，重派自动续转
                ok = process_task(
                    self.api, self.ffmpeg, task,
                    WORK_DIR / str(task_id),
                    stop=stop,
                    register=_register_proc,
                    progress=progress,
                )
                return ok
            except Exception as exc:  # 任何意外：上报失败，继续接下一单
                import traceback

                self.log(f"[{task_id}] 异常：{exc}")
                self.log(traceback.format_exc()[-900:])
                if not stop.is_set():
                    self.api.request(
                        "POST",
                        f"/api/v1/preload-worker/tasks/{task_id}/fail",
                        json_body={"error": str(exc)[:2000]},
                    )
                return False

    def _stop_preload_task(self, task_id, cancel: bool = False) -> None:
        """停掉对应预转码：置位停止钩子并杀掉在跑的分段。``cancel`` 时收尾删本地半成品。"""
        with self._tasks_lock:
            stop = self._preload_stop.get(task_id)
            proc = self._preload_procs.get(task_id)
            if cancel and stop is not None:
                self._preload_cancelled.add(task_id)
        if stop is not None:
            stop.set()
        if proc is not None:
            with contextlib.suppress(OSError):
                proc.kill()

    def _yield_preload_slots(self, keep: int) -> None:
        """实时任务进入时让位：预转码占用超出 keep 的部分（按开始先后）停掉，并告诉服务端。

        服务端收到 preload.yielded 后任务当场回队；本地工作目录保留，播放结束后续转。
        """
        with self._tasks_lock:
            running = list(self._preload_stop)
            over = running[: max(0, len(running) - keep)]
        for task_id in over:
            self.log(f"[播放优先] 预转码 {task_id} 让位（断点保留，播放结束后续转）")
            self._stop_preload_task(task_id)
            if self._realtime_client is not None:
                self._realtime_client.send_preload_yielded(task_id)

    def progress_view(self) -> list[tuple[str, dict]]:
        """GUI 状态栏轮询：在跑任务的 [(显示名, 进度快照)]，按接单先后。

        快照是浅拷贝（dict 读取在 GIL 下原子，与 process_task 的原地更新
        共用同一前提）；任务未登记进度（刚接单还没进 process_task）给空
        dict，GUI 显示「准备中」。
        """
        with self._tasks_lock:
            return [
                (name, dict(self._progress.get(task_id) or {}))
                for task_id, name in self.tasks.items()
            ]


def _print_status(kind: str, **fields: object) -> None:
    """CLI 的状态出口：只打印带 line 的事件（连接状态的文字同时经 log 行输出）。"""
    if fields.get("line"):
        print(fields["line"])


def main() -> None:
    parser = argparse.ArgumentParser(description="MovieClaw Windows Worker（预转码+实时统一调度）")
    parser.add_argument("--server", help="NAS 地址；缺省读配置文件")
    parser.add_argument("--ffmpeg", help="ffmpeg 可执行文件路径；缺省读配置文件")
    parser.add_argument("--name", help="配对显示名；缺省读配置文件")
    parser.add_argument("--concurrency", type=int, help="并发槽位数 1-8")
    parser.add_argument("--ac-only", action="store_true", help="仅接通电源时执行")
    parser.add_argument(
        "--once",
        action="store_true",
        help="（轮询时代遗留）推送派单模式下无效果，仅保留参数兼容",
    )
    parser.add_argument("--reset-token", action="store_true", help="丢弃已存令牌重新配对")
    parser.add_argument("--autostart", action="store_true", help="开机自启调用；已有实例时静默退出")
    args = parser.parse_args()

    # 单实例防呆锁：同机第二个 Worker 只会互顶 WS 连接、抢同一块 GPU。
    # 句柄留在模块级（返回值丢弃的话锁的语义会随实现漂移）
    import single_instance

    if single_instance.acquire_singleton_lock() is None:
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
    if args.reset_token:
        config.pop("token", None)
    problem = check_ffmpeg(config.get("ffmpeg") or "ffmpeg")
    if problem:
        print(problem)
        sys.exit(1)
    save_config(config)

    if args.once:
        print("提示：--once 是轮询领取时代的参数，推送派单模式下没有可判定的"
              "「队列清空」时刻，已忽略。")

    runtime = WorkerRuntime(
        server=config["server"],
        token=config.get("token"),
        ffmpeg=config.get("ffmpeg") or "ffmpeg",
        name=config.get("name"),
        concurrency=config.get("concurrency") or 1,
        ac_only=bool(config.get("ac_only")),
        on_token=lambda token: save_config({**config, "token": token}),
        status_cb=_print_status,
    )
    try:
        runtime.start()
    except KeyboardInterrupt:
        print("\n已停止。")
        sys.exit(0)


if __name__ == "__main__":
    main()
