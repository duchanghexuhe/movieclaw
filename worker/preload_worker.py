"""MovieClaw 预转码 Windows Worker（docs/design/preload-transcode.md §7）。

独立后台程序：连 NAS 领任务 → 跑 ffmpeg → 分段上传产物。**不在客户端里**：
「把 Windows 从架构里拿掉，功能仍完整存在，只是暂时无人执行」。

用法::

    python preload_worker.py --server http://nas:3000 [--ffmpeg ffmpeg.exe] [--once]

- 首次运行走设备配对（浏览器开 NAS 的 /activate 页，批准 Worker 的请求）；
- 配对令牌存本地 preload_worker.json（与配置同文件）；
- 主循环：领任务 → 下载源 → ffmpeg 转码（定时上报进度续约）→ 分段上传 →
  complete → 领下一个；队列空时睡 POLL_SECONDS。
- 断点续转：上传按段对齐服务端台账 resume_parts；中断重跑自动续。

依赖：仅 Python 3.11+ 标准库（urllib），零第三方包——可直接拷到任何
Windows 机器跑，不要求装 MovieClaw 客户端。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CONFIG_FILE = Path(__file__).with_name("preload_worker.json")
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
        head = {"Accept": "application/json"}
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

    def pair(self, name: str) -> str:
        """设备配对流：拿配对码 → 轮询兑换（scope=transcode 凭证）。"""
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
            raise SystemExit(f"配对发起失败：{status} {view}")
        data = view["data"]
        print(f"请在浏览器打开并批准：{data['verification_uri_complete']}")
        print(f"配对码：{data['user_code']}（{data['expires_in']} 秒内有效）")
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
                print("配对成功。")
                return token
            if status == 202:
                continue
            raise SystemExit(f"配对失败（{status}），请重新运行")


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


def upload_parts(api: Api, task: dict, path: Path) -> None:
    """分段上传产物，按服务端 resume_parts 断点续转。"""
    task_id = task["task_id"]
    artifact_url = task["artifact_url"]  # 相对路径带 token
    part_bytes = task.get("part_bytes") or PART_BYTES_DEFAULT
    resume = task.get("resume_parts") or 0
    size = path.stat().st_size
    total_parts = (size + part_bytes - 1) // part_bytes
    for part in range(total_parts):
        if part < resume:
            continue
        start = part * part_bytes
        chunk = path.read_bytes()[start : start + part_bytes]  # 分段 64MB，内存可承受
        sep = "&" if "?" in artifact_url else "?"
        status, body = api.request(
            "PUT",
            f"{artifact_url}{sep}part={part}",
            data=chunk,
            headers={"Content-Type": "application/octet-stream"},
        )
        if status not in (200, 201):
            raise RuntimeError(f"分段 {part} 上传失败：{status} {body}")


def process_task(api: Api, ffmpeg: str, task: dict, workdir: Path) -> bool:
    """执行一个任务；返回是否成功（失败已上报）。"""
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
    artifact = workdir / task["artifact_name"]

    # 1) 取源（签名 URL，支持 Range；这里整文件下载——ffmpeg 直接读 HTTP 也行，
    #    但整文件下载对断线重试与 NAS 磁盘更友好）
    url = api.server + "/" + task["source_url"]
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=600) as resp, open(source, "wb") as out:
        while chunk := resp.read(4 * 1024 * 1024):
            out.write(chunk)
    print(f"[{task_id}] 源已就绪（{source.stat().st_size / 1024**2:.0f} MB）")

    # 2) 转码（进度上报线程续约）
    import threading

    stop = threading.Event()

    def reporter() -> None:
        while not stop.wait(PROGRESS_SECONDS):
            api.request("POST", f"/api/v1/preload-worker/tasks/{task_id}/progress", json_body={})

    thread = threading.Thread(target=reporter, daemon=True)
    thread.start()
    try:
        code, stderr_tail = run_ffmpeg(ffmpeg, task["argv"], workdir)
    finally:
        stop.set()
        thread.join()
    if code != 0:
        api.request(
            "POST",
            f"/api/v1/preload-worker/tasks/{task_id}/fail",
            json_body={"error": f"ffmpeg 退出码 {code}：{stderr_tail[-800:]}"},
        )
        print(f"[{task_id}] ffmpeg 失败（{code}）")
        return False

    # 3) 分段上传 + 完成
    print(f"[{task_id}] 上传产物（{artifact.stat().st_size / 1024**3:.2f} GB）")
    upload_parts(api, task, artifact)
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



def probe_caps(ffmpeg: str) -> dict:
    """探测本机 ffmpeg 的硬件能力，随 claim 上报，服务端据此选编码链。

    - encoders：h264_nvenc 等硬件编码器；
    - filters：scale_cuda / tonemapx 等滤镜（jellyfin-ffmpeg 才有 tonemapx）；
    - hw_decoders：cuda 可硬解的编码（探测不到就空——服务端退回软解输入）。
    探测结果缓存在同目录 caps.json，换 ffmpeg 路径后删除重探。
    """
    import re as _re

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
        caps["encoders"] = sorted(
            set(out.split()) & {"h264_nvenc", "hevc_nvenc"}
        )
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


def main() -> None:
    parser = argparse.ArgumentParser(description="MovieClaw 预转码 Worker")
    parser.add_argument("--server", required=True, help="NAS 地址，如 http://192.168.31.10:3000")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg 可执行文件路径")
    parser.add_argument("--name", default="Preload Worker (Windows)")
    parser.add_argument("--once", action="store_true", help="处理完队列里现有任务后退出")
    parser.add_argument("--reset-token", action="store_true", help="丢弃已存令牌重新配对")
    args = parser.parse_args()

    config = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
    if args.reset_token:
        config.pop("token", None)
    token = config.get("token")
    api = Api(args.server, token)
    if not token:
        token = api.pair(args.name)
        config["token"] = token
        config["server"] = args.server
        CONFIG_FILE.write_text(json.dumps(config, indent=2))

    caps = probe_caps(args.ffmpeg)
    print(
        f"Worker 已连接 {args.server}，开始轮询任务队列（每 {POLL_SECONDS} 秒）…"
        f"（NVENC: {'有' if 'h264_nvenc' in caps['encoders'] else '无'}）"
    )
    while True:
        status, view = api.request(
            "POST", "/api/v1/preload-worker/claim", json_body={"caps": caps}
        )
        if status == 401:
            print("令牌失效，重新配对…")
            api = Api(args.server, None)
            config.pop("token", None)
            token = api.pair(args.name)
            config["token"] = token
            CONFIG_FILE.write_text(json.dumps(config, indent=2))
            continue
        task = (view or {}).get("data", {}).get("task") if status == 200 else None
        if task is None:
            if args.once:
                print("队列已空，退出。")
                return
            time.sleep(POLL_SECONDS)
            continue
        try:
            workdir = Path(__file__).parent / "work" / str(task["task_id"])
            process_task(api, args.ffmpeg, task, workdir)
        except Exception as exc:  # 任何意外：上报失败，继续领下一个
            print(f"[{task.get('task_id')}] 异常：{exc}")
            api.request(
                "POST",
                f"/api/v1/preload-worker/tasks/{task['task_id']}/fail",
                json_body={"error": str(exc)[:2000]},
            )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已停止。")
        sys.exit(0)
