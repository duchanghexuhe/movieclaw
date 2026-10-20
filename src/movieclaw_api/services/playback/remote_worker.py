"""远程转码 Worker 的控制面注册表。

控制面使用 WebSocket，数据面仍使用普通 HTTPS：

* WebSocket 只发送任务参数、停止命令和状态，不承载媒体数据；
* 源文件由 Worker 通过带签名的 HTTP(S) URL Range 读取；
* HLS 分片由 ffmpeg 直接 HTTP PUT 回 NAS，避免 Worker 产生临时文件。

注册表是进程内状态，和现有播放会话一样要求单进程 uvicorn。Worker 断线后
不会自动恢复旧任务，避免两个 Worker 同时向同一分片写入；播放器下一次请求
会收到失败并走现有降档/重试回路。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from starlette.websockets import WebSocket

from movieclaw_api.services.playback.ffmpeg_args import (
    NVENC_TONEMAP,
    WorkerVideoCaps,
    register_native_tonemap,
    registered_native_tonemap,
)
from movieclaw_api.services.playback.remote_config import (
    RemoteTranscodeRuntimeConfig,
    remote_transcode_issues,
)
from movieclaw_api.services.playback.remote_config import (
    effective_remote_transcode_config as _effective_remote_transcode_config,
)
from movieclaw_playback.decide import PlaybackPlan

logger = logging.getLogger("movieclaw_api.playback.remote_worker")

#: v2（2026-10-15 Worker 统一调度）：hello 增报 ``roles``（realtime/preload）
#: 与 ``max_concurrency``（本机最大同时转码，替代 max_jobs），job.start 增
#: ``class``（realtime/preload）。v2 起并发只在 Worker 本机配置，服务端不再
#: 保存或下发任何并发上限。旧版 Worker 被握手版本检查挡掉（提示更新）。
REMOTE_WORKER_PROTOCOL_VERSION = 2
WORKER_IDLE_TIMEOUT_S = 45.0
JOB_ACCEPT_TIMEOUT_S = 8.0
LOAD_TIMEOUT_S = 15.0
_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
#: 远程 Worker 允许申报的硬件后端。videotoolbox 之外加了 nvenc（Windows
#: Worker 的 CUDA 链，命令构造器 HW_BACKENDS 里本就有它；hello 里申报
#: 其他名字一律丢弃）。segment_types/分片器等约束与后端无关。
_SUPPORTED_BACKENDS = frozenset({"videotoolbox", "nvenc"})
#: HLS 分片类型，取值同 ffmpeg ``-hls_segment_type``（见 ffmpeg_args.segment_type）。
_SUPPORTED_SEGMENT_TYPES = frozenset({"fmp4", "mpegts"})
#: 没申报 ``hw_decoders`` 的旧版 Worker 只按这两种算能硬解：所有支持的 Apple 芯片
#: Mac 都能硬解它们，其余编码一律走软解（见 ffmpeg_args.WorkerVideoCaps）
_DEFAULT_HW_DECODERS = ("h264", "hevc")


class RemoteWorkerUnavailable(RuntimeError):
    """没有可接单的远程 Worker。"""


def describe_job_failure(message: dict[str, Any]) -> str:
    """把 Worker 上报的任务失败压成一行：原因、ffmpeg 退出码、stderr 最后三行。"""
    parts = [str(message.get("error") or "未说明原因").strip()]
    exit_code = message.get("exit_code")
    # Worker 的错误文案常常本身就是「ffmpeg 退出码：N」，别再重复一遍
    if isinstance(exit_code, int) and not isinstance(exit_code, bool) and "退出码" not in parts[0]:
        parts.append(f"ffmpeg 退出码 {exit_code}")
    stderr_tail = message.get("stderr_tail")
    if isinstance(stderr_tail, str) and stderr_tail.strip():
        lines = [line.strip() for line in stderr_tail.strip().splitlines() if line.strip()]
        parts.append(f"stderr 末尾：{' | '.join(lines[-3:])[-600:]}")
    return "；".join(parts)


@dataclass(frozen=True)
class WorkerCapabilities:
    """Worker 在 hello 中声明的能力快照。"""

    backends: tuple[str, ...] = ()
    encoders: tuple[str, ...] = ()
    ffmpeg_version: str | None = None
    platform: str | None = None
    max_jobs: int = 1
    #: v2 hello 申报的执行角色。带 preload 的 Worker 才会被推送预转码任务；
    #: 只报 realtime 的（如纯实时 CLI）保持原有只接播放任务的形态。
    roles: frozenset[str] = frozenset({"realtime"})
    #: 能回传的 HLS 分片类型。没声明的是旧版 Worker，只当它会 fMP4：它的上传
    #: 代理白名单只放行 ``.m4s``，TS 分片在它本机就被拒收，而 ffmpeg 不看上传
    #: 响应码、照样退出码 0 转完整部片——播放器等满 30 秒只拿到 404（issue #444）。
    segment_types: tuple[str, ...] = ("fmp4",)
    #: 能接收 ``job.playback``（观众播放位置）。旧版 Worker 不认识这条消息，每收到
    #: 一条就记一行「忽略未知控制消息」，3 秒一条会把它的日志刷满——所以只发给声明了的。
    playback_progress: bool = False
    #: 能读原盘：源是 NAS 下发的 ffconcat 清单（每段 m2ts 一个签名地址），靠 concat
    #: 的 ``option`` 指令给每段带上断线续读参数。旧版 Worker 的 ffmpeg 未必认这些，
    #: 原盘任务只派给声明了的。
    disc_sources: bool = False
    #: VideoToolbox 能硬解的片源编码（ffmpeg 编码名，Worker 用系统接口实测）。
    hw_decoders: tuple[str, ...] = _DEFAULT_HW_DECODERS
    #: Worker 的 ffmpeg 带的 Metal 滤镜（scale_vt、tonemap_videotoolbox……）。
    filters: tuple[str, ...] = ()
    #: 能边产出边送分片（docs/design/transcode-latency.md §5）：ffmpeg 输出分片化 MP4，
    #: Worker 按 4 秒栅格切段，每 0.5 秒一个片段分块回传。旧版 Worker 只认 HLS 产物名，
    #: 只派整段落盘的任务给它。
    progressive_segments: bool = False
    #: Worker 的 ffmpeg 认的取源选项（``ffmpeg_args.remote_read_options`` 会用到的那几个）
    read_options: tuple[str, ...] = ()
    server_config: bool = False
    hardware: dict[str, Any] = field(default_factory=dict)

    @property
    def video_caps(self) -> WorkerVideoCaps:
        """命令装配要的那部分（见 ``ffmpeg_args.WorkerVideoCaps``）。"""
        return WorkerVideoCaps(
            hw_decoders=frozenset(self.hw_decoders),
            filters=frozenset(self.filters),
            read_options=frozenset(self.read_options),
        )


@dataclass
class WorkerConnection:
    """一个在线 WebSocket Worker 及其正在执行的任务。"""

    worker_id: str
    websocket: WebSocket
    capabilities: WorkerCapabilities
    # Worker 实际连上来的根地址（scheme://host[:port][根路径]），由控制面
    # 握手时从请求本身推断，Worker 不需要上报、用户也不需要填。它天然是
    # 「这台 Worker 一定够得着」的地址——它刚刚就是从那儿连进来的。
    observed_base_url: str = ""
    worker_version: str | None = None
    arch: str | None = None
    #: 握手所用凭证对应的登录设备 id：在「设备」页注销这台转码器时据此当场断开
    #: 它的连接——只删凭证不断连接，被注销的转码器会一直干活到下次重连。
    login_device_id: int | None = None
    connected_at: float = field(default_factory=time.monotonic)
    last_seen: float = field(default_factory=time.monotonic)
    jobs: set[str] = field(default_factory=set)
    #: 已推送给这台 Worker、尚未终态的预转码任务（task_id → 推送时刻）。
    #: 预算记账的唯一事实来源——不记的话派单循环每轮读到的容量都是旧值，
    #: 会把整条队列一口气全推给同一台 Worker（推送后任务仍 CLAIMED，Worker
    #: 端信号量把它排成幻影线程，租约到期回收再派，churn 循环）。
    preload_jobs: dict[int, float] = field(default_factory=dict)
    draining: bool = False
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    max_jobs: int = 1
    load: dict[str, Any] = field(default_factory=dict)
    load_seen: float = 0.0
    # 每台最多 32 种任务规格，重连即丢弃；不把不同片源的 speed 混成硬件跑分。
    speeds: dict[tuple[Any, ...], float] = field(default_factory=dict)
    speed_samples: dict[tuple[Any, ...], int] = field(default_factory=dict)

    async def send(self, payload: dict[str, Any]) -> None:
        """串行发送控制消息，防止并发 stop/start 交叉写 WebSocket。"""
        async with self.send_lock:
            await self.websocket.send_json(payload)


class RemoteWorkerRegistry:
    """进程级 Worker 注册表与任务路由器。"""

    def __init__(self) -> None:
        # 能力探测通过 asyncio.to_thread() 执行，而 Worker 连接在事件循环线程
        # 中注册/注销；这里不能只依赖 asyncio.Lock，否则后台探测会并发遍历
        # 正在变化的 dict。锁只保护短暂的内存台账，不包住任何网络 await。
        self._lock = threading.RLock()
        self._workers: dict[str, WorkerConnection] = {}
        self._job_workers: dict[str, str] = {}
        self._job_attempts: dict[str, str] = {}
        #: 预转码在途任务反查表（task_id → worker_id）：complete/fail/取消时
        #: 按 task_id 释放记账，不必知道它当时在哪台 Worker 上
        self._preload_task_workers: dict[int, str] = {}
        self._job_events: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self._job_states: dict[str, dict[str, Any]] = {}
        self._job_profiles: dict[str, tuple[Any, ...]] = {}
        self._progress_samples: dict[str, tuple[float, int]] = {}
        self._job_speeds: dict[str, tuple[float, int, float]] = {}
        self._paused_jobs: set[str] = set()
        #: 可被播放挤掉的任务（离线下载的转码导出）：播放视角下它们占的名额算空闲，
        #: 真要派播放任务时由会话层先停掉它（session.py ``_evict_export_for_remote``）
        self._preemptible_jobs: set[str] = set()

    # -- Worker 生命周期 -------------------------------------------------

    async def register(
        self,
        websocket: WebSocket,
        hello: dict[str, Any],
        *,
        observed_base_url: str = "",
        login_device_id: int | None = None,
    ) -> WorkerConnection:
        """校验 hello 并登记 Worker；同 ID 的旧连接会被替换。"""
        raw_worker_id = hello.get("worker_id")
        if not isinstance(raw_worker_id, str):
            raise ValueError("worker_id 必须是字符串")
        worker_id = raw_worker_id.strip()
        if not _WORKER_ID.fullmatch(worker_id):
            raise ValueError("worker_id 为空或包含不支持的字符")
        capabilities = self._parse_capabilities(hello.get("capabilities"))
        connection = WorkerConnection(
            worker_id=worker_id,
            websocket=websocket,
            capabilities=capabilities,
            # v2：并发只在 Worker 本机配置（max_concurrency），服务端不再保存
            # 或下发任何上限；这里照单全收它申报的值
            max_jobs=capabilities.max_jobs,
            observed_base_url=observed_base_url,
            login_device_id=login_device_id,
            worker_version=str(hello.get("worker_version"))
            if hello.get("worker_version")
            else None,
            arch=str(hello["capabilities"].get("arch"))
            if isinstance(hello.get("capabilities"), dict)
            and hello["capabilities"].get("arch")
            else None,
            draining=bool(hello.get("draining", False)),
        )
        with self._lock:
            previous = self._workers.get(worker_id)
            if previous is not None:
                logger.warning("远程 Worker %s 重复连接，关闭旧连接", worker_id)
                self._mark_jobs_lost(previous, "远程 Worker 连接已替换")
            self._workers[worker_id] = connection
        if previous is not None:
            await self._close_quietly(previous.websocket, code=1012, reason="连接已替换")
        logger.info(
            "远程转码 Worker 已上线：%s（版本=%s 平台=%s ffmpeg=%s 并发=%d 后端=%s 分片=%s）",
            worker_id,
            connection.worker_version or "未知",
            capabilities.platform or "未知",
            capabilities.ffmpeg_version or "未知",
            connection.max_jobs,
            ",".join(capabilities.backends) or "无",
            ",".join(capabilities.segment_types),
        )
        if "nvenc" in capabilities.backends and "tonemap_cuda" in capabilities.filters:
            # NAS 本地探不到 NVENC（远程会话跑在这台 Worker 的 ffmpeg 上），原生
            # tone-map 只能信 Worker 申报：HDR 片的全 GPU 链（tonemap_cuda +
            # scale_cuda）靠这里点亮。不点亮的话远程会话一直走 Worker CPU 的
            # tonemapx 软链——实测吞吐只有 GPU 链的 1/4，GPU 大部分时间在等
            # CPU（利用率呈正弦波）。
            register_native_tonemap("nvenc", NVENC_TONEMAP)
        return connection

    async def unregister(self, connection: WorkerConnection) -> None:
        """断开 Worker，并让等待中的会话尽快看到失败。"""
        with self._lock:
            current = self._workers.get(connection.worker_id)
            if current is not connection:
                return
            self._workers.pop(connection.worker_id, None)
            lost_jobs = self._mark_jobs_lost(connection, "远程 Worker 已断开连接")
        # Worker 申报的原生 tone-map 注册一并撤销（hello 时登记的那份）；
        # 注册值已不是它写入的（本机探测的结论）就不动，避免盖掉别人。
        if registered_native_tonemap("nvenc") == NVENC_TONEMAP:
            register_native_tonemap("nvenc", None)
        # 带上被判失败的任务 ID：Worker 崩在半路时，用户先看到的是「播放失败」，
        # 而这一行是把那次失败和这台 Worker 的掉线对上号的唯一凭据。
        logger.warning(
            "远程转码 Worker 已离线：%s，%s",
            connection.worker_id,
            f"进行中的任务已判失败={','.join(lost_jobs)}" if lost_jobs else "无进行中的任务",
        )

    async def shutdown(self) -> None:
        """应用退出时关闭 Worker 连接；任务会先由会话管理器停止。"""
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
            self._job_workers.clear()
            self._job_attempts.clear()
            self._preload_task_workers.clear()
            self._job_events.clear()
            self._job_states.clear()
            self._job_profiles.clear()
            self._progress_samples.clear()
            self._job_speeds.clear()
            self._paused_jobs.clear()
        for connection in workers:
            await self._close_quietly(connection.websocket, code=1001, reason="服务端关闭")

    def connected_login_device_ids(self) -> set[int]:
        """此刻有活控制连接的登录设备 id（「设置 → 设备」据此把转码器标成「已连接」）。

        转码器只在握手时验一次凭证，之后整条连接靠心跳维持；只看凭证最近一次验签
        的时间，连着的转码器 5 分钟后就会被显示成离线。连没连着以这里为准。
        """
        with self._lock:
            return {
                c.login_device_id for c in self._workers.values() if c.login_device_id is not None
            }

    async def disconnect_device(self, login_device_id: int, reason: str) -> int:
        """断开用某台登录设备的凭证连上来的 Worker（注销转码器时调用），返回断开数。"""
        with self._lock:
            victims = [
                c for c in self._workers.values() if c.login_device_id == login_device_id
            ]
            for connection in victims:
                self._mark_jobs_lost(connection, reason)
                self._workers.pop(connection.worker_id, None)
        for connection in victims:
            logger.warning("远程转码 Worker %s 的凭证已被注销，断开连接", connection.worker_id)
            await self._close_quietly(connection.websocket, code=1008, reason=reason)
        return len(victims)

    async def disconnect_all(self, reason: str) -> None:
        """配置变更时断开所有 Worker，使旧令牌不再保持有效连接。"""
        with self._lock:
            workers = list(self._workers.values())
            for connection in workers:
                self._mark_jobs_lost(connection, reason)
            self._workers.clear()
            self._job_workers.clear()
            self._job_attempts.clear()
            self._preload_task_workers.clear()
        for connection in workers:
            await self._close_quietly(connection.websocket, code=1012, reason=reason)

    # -- 能力与状态 ------------------------------------------------------

    def has_capable_worker(
        self,
        backend: str = "videotoolbox",
        segment_type: str = "fmp4",
        *,
        disc: bool = False,
        strict: bool = False,
    ) -> bool:
        """同步查询是否有能执行指定后端、回传指定分片类型（原盘还要能读原盘）的空闲 Worker。

        默认是播放视角：离线导出占着的名额算空闲（播放来了它让路）。``strict`` 只认真正
        空着的名额——离线导出自己排队时用，不能把别的导出当成可挤掉的。"""
        if not remote_worker_enabled():
            return False
        return (
            self._select_worker(backend, segment_type, disc=disc, yield_preemptible=not strict)
            is not None
        )

    def workers_have_encoder(self, encoder: str, backend: str = "videotoolbox") -> bool:
        with self._lock:
            workers = [
                c
                for c in self._workers.values()
                if backend in c.capabilities.backends and self._is_fresh(c)
            ]
            return bool(workers) and all(encoder in c.capabilities.encoders for c in workers)

    def preemptible_job_for(
        self, backend: str = "videotoolbox", segment_type: str = "fmp4", *, disc: bool = False
    ) -> str | None:
        """播放要派任务、却只剩被离线导出占着的名额：返回该让路的导出任务号。

        有真正空着的名额就返回 None——不为了播放白白打断导出。"""
        if not remote_worker_enabled():
            return None
        with self._lock:
            if self._select_worker(backend, segment_type, disc=disc) is not None:
                return None
            connection = self._select_worker(
                backend, segment_type, disc=disc, yield_preemptible=True
            )
            if connection is None:
                return None
            return next(iter(sorted(connection.jobs & self._preemptible_jobs)), None)

    def has_busy_worker(self, backend: str = "videotoolbox") -> bool:
        """有能执行这个后端的 Worker 在线，只是任务名额都占满了（正在排空的不算）。

        决策层据此把「暂时没有空闲硬件」与「根本没有硬件」分开说：前者等一会儿、停掉别的转码
        就能放，后者要去配显卡——同一句「未检测到硬件」会把人支去查一台好好的转码器。
        """
        if not remote_worker_enabled():
            return False
        with self._lock:
            return any(
                backend in connection.capabilities.backends
                and "realtime" in connection.capabilities.roles
                # 离线导出占着的名额播放来了就让出来，不算「满」
                and len(connection.jobs - self._preemptible_jobs) >= connection.max_jobs
                and not connection.draining
                and self._is_fresh(connection)
                for connection in self._workers.values()
            )

    def worker_online(self, worker_id: str | None) -> bool:
        """会话等待分片时判断它所属的 Worker 是否仍在线。"""
        if not worker_id:
            return False
        with self._lock:
            connection = self._workers.get(worker_id)
            return connection is not None and self._is_fresh(connection)

    def video_caps(self, worker_id: str | None) -> WorkerVideoCaps | None:
        """某台在线 Worker 申报的视频能力（原盘清单按它给每段剪辑加取源选项）。"""
        with self._lock:
            connection = self._workers.get(worker_id) if worker_id else None
            return connection.capabilities.video_caps if connection is not None else None

    def snapshot(self) -> list[dict[str, Any]]:
        """返回不含令牌的状态，供管理员诊断页使用。"""
        with self._lock:
            return [
                {
                    "worker_id": connection.worker_id,
                    "worker_version": connection.worker_version,
                    "arch": connection.arch,
                    "device_id": connection.login_device_id,
                    "hardware": connection.capabilities.hardware,
                    "load": dict(connection.load) if self._load_fresh(connection) else None,
                    "server_config": connection.capabilities.server_config,
                    "platform": connection.capabilities.platform,
                    "ffmpeg_version": connection.capabilities.ffmpeg_version,
                    "backends": list(connection.capabilities.backends),
                    "max_jobs": connection.max_jobs,
                    "roles": sorted(connection.capabilities.roles),
                    "disc_sources": connection.capabilities.disc_sources,
                    "hw_decoders": list(connection.capabilities.hw_decoders),
                    "filters": list(connection.capabilities.filters),
                    "active_jobs": len(connection.jobs),
                    "preload_jobs": len(connection.preload_jobs),
                    "jobs": [
                        {
                            "job_id": job_id,
                            "type": self._job_states.get(job_id, {}).get("type"),
                            "out_time_ms": self._job_states.get(job_id, {}).get(
                                "out_time_ms"
                            ),
                            "speed": self._job_states.get(job_id, {}).get("speed"),
                            "phase": self._job_states.get(job_id, {}).get("phase"),
                        }
                        for job_id in sorted(connection.jobs)
                    ],
                    "draining": connection.draining,
                    "last_seen_seconds": max(0.0, time.monotonic() - connection.last_seen),
                    "online": self._is_fresh(connection),
                }
                for connection in self._workers.values()
            ]

    # -- 任务控制 --------------------------------------------------------

    def create_job_waiter(self, job_id: str) -> None:
        """创建一次性任务事件队列；重复调用会丢弃旧队列。"""
        with self._lock:
            self._job_events[job_id] = asyncio.Queue(maxsize=8)
            self._job_states[job_id] = {"type": "job.pending"}

    def remove_job_waiter(self, job_id: str) -> None:
        with self._lock:
            self._job_events.pop(job_id, None)

    def remove_job(self, job_id: str) -> None:
        """彻底移除已结束会话的状态，避免运行很久后字典持续增长。"""
        with self._lock:
            self._job_events.pop(job_id, None)
            self._job_states.pop(job_id, None)
        self._release_job(job_id)

    def job_state(self, job_id: str) -> dict[str, Any] | None:
        """取任务最近状态，供分片等待循环快速失败。"""
        with self._lock:
            return self._job_states.get(job_id)

    async def wait_job_event(
        self, job_id: str, *, timeout: float = JOB_ACCEPT_TIMEOUT_S
    ) -> dict[str, Any]:
        """等待 Worker 回应 accepted/failed。"""
        with self._lock:
            queue = self._job_events.get(job_id)
        if queue is None:
            raise RemoteWorkerUnavailable("远程任务等待器不存在")
        try:
            return await asyncio.wait_for(queue.get(), timeout=timeout)
        except TimeoutError as exc:
            raise RemoteWorkerUnavailable("远程 Worker 接单超时") from exc

    def reserve(
        self,
        job_id: str,
        *,
        backend: str,
        segment_type: str = "fmp4",
        attempt_id: str | None = None,
        disc: bool = False,
        plan: PlaybackPlan | None = None,
        source_id: int | None = None,
        learn_speed: bool = False,
        preemptible: bool = False,
    ) -> WorkerConnection:
        """选一个空闲 Worker 并占住槽位，返回它的连接。

        为什么先占位再下发、而不是一个 ``dispatch`` 打包做完：任务里的源地址
        和产物上传地址要用**这台** Worker 连上来的地址拼（``observed_base_url``），
        所以必须先确定是谁接单。占位和选人在同一把锁里完成，不会出现「按 A 的
        地址拼 URL，任务却发给了 B」。占位后如果拼装或发送失败，调用方必须调
        ``release_job``（``start_job`` 失败时会自己调）。

        ``segment_type`` 是任务要产出的分片类型：TS 任务只能落到声明了
        ``mpegts`` 的 Worker 上，多台新旧 Worker 同时在线时也不会派错。原盘任务
        （``disc``）同理只落到声明了 ``disc_sources`` 的 Worker 上。
        """
        with self._lock:
            if job_id in self._job_workers:
                raise RemoteWorkerUnavailable("远程任务已存在")
            connection = self._select_worker(
                backend, segment_type, disc=disc, plan=plan, source_id=source_id
            )
            # 预转码不占实时的位：播放到来时在跑的预转码由 Worker 自己让位（见
            # handle_message 的 preload.yielded），这里不再抢占
            if connection is None:
                if disc and self._select_worker(backend, segment_type) is not None:
                    raise RemoteWorkerUnavailable(
                        "在线的 Worker 版本过旧，不支持原盘，"
                        "请把 MovieClaw 转码器更新到与服务端相同的版本"
                    )
                if segment_type != "fmp4" and self._select_worker(backend) is not None:
                    raise RemoteWorkerUnavailable(
                        f"在线的 Worker 版本过旧，不支持 {segment_type} 分片，"
                        "请把 MovieClaw 转码器更新到与服务端相同的版本"
                    )
                raise RemoteWorkerUnavailable("没有在线且空闲的远程转码 Worker")
            self._job_workers[job_id] = connection.worker_id
            self._job_attempts[job_id] = (
                attempt_id if isinstance(attempt_id, str) and attempt_id else job_id
            )
            connection.jobs.add(job_id)
            if preemptible:
                self._preemptible_jobs.add(job_id)
            if plan is not None and source_id is not None and learn_speed:
                self._job_profiles[job_id] = self._profile(plan, source_id)
        return connection

    def release_job(self, job_id: str) -> None:
        """占位之后拼装失败时归还槽位。"""
        self._release_job(job_id)

    # -- 预转码推送派单（v2 统一调度）-------------------------------------

    def preload_capacity(self, connection: WorkerConnection) -> int:
        """这台 Worker 现在还能接几个预转码任务（播放优先）。

        有任何实时任务（播放转码、离线导出）在跑就是 0：预转码整体让位；空闲时整个并发
        都可以转预转码，即 max_concurrency 减去在途的预转码数。在途的推送记账在
        ``preload_jobs`` 里，直到终态（complete/fail/取消/回队）才释放，推送成功一个记一个，
        派单循环不会重复计数。
        """
        with self._lock:
            if connection.jobs:
                return 0
            return max(0, connection.max_jobs - len(connection.preload_jobs))

    def preload_workers(self) -> list[WorkerConnection]:
        """带 preload 角色、不在排空、心跳新鲜的在线连接（按余量排序）。"""
        with self._lock:
            candidates = [
                c
                for c in self._workers.values()
                if "preload" in c.capabilities.roles
                and not c.draining
                and self._is_fresh(c)
            ]
        candidates.sort(key=lambda c: (len(c.jobs) / max(1, c.max_jobs), -c.connected_at))
        return candidates

    def connection_for_device(self, login_device_id: int) -> WorkerConnection | None:
        """某登录设备此刻的活连接（REST claim 端点做角色/预算校验用）。"""
        with self._lock:
            return next(
                (
                    c
                    for c in self._workers.values()
                    if c.login_device_id == login_device_id and self._is_fresh(c)
                ),
                None,
            )

    def note_preload_pushed(self, connection: WorkerConnection, task_id: int) -> None:
        """任务成功推送给这台 Worker 后记账（直到终态才释放）。"""
        with self._lock:
            connection.preload_jobs[task_id] = time.monotonic()
            self._preload_task_workers[task_id] = connection.worker_id

    def release_preload_task(self, task_id: int, worker_id: str | None = None) -> None:
        """预转码任务到达终态（complete/fail/取消/回队/回收）后释放记账。

        ``worker_id`` 给出时只清该 Worker 的账——旧 Worker 对已重派任务的
        迟到上报不能把接手者的在途账释放掉（记账被覆盖后仅按 task_id pop
        会短暂超发）。不给 worker_id 按反查表清（服务端单方回队/取消的场景）。
        """
        with self._lock:
            owner = self._preload_task_workers.get(task_id)
            if owner is None:
                return
            if worker_id is not None and owner != worker_id:
                return
            self._preload_task_workers.pop(task_id, None)
            connection = self._workers.get(owner)
            if connection is not None:
                connection.preload_jobs.pop(task_id, None)

    async def send_preload_job(
        self, connection: WorkerConnection, payload: dict[str, Any]
    ) -> bool:
        """向一台 Worker 推送预转码 job.start；发送失败返回 False（调用方退回任务）。

        由正在事件循环里的调用方直接 await（不内嵌 create_task——抢占式
        claim 的原子性要求发送结果当场可知，异步丢弃会把失败留成无人认领
        的 CLAIMED 任务等 10 分钟租约）。发送成功即记账，预算立即扣减。
        """
        task_id = payload.get("task_id")
        try:
            await connection.send({"type": "job.start", "class": "preload", **payload})
        except Exception:  # noqa: BLE001
            logger.warning(
                "预转码任务下发失败：worker=%s task=%s",
                connection.worker_id,
                task_id,
            )
            return False
        if isinstance(task_id, int):
            self.note_preload_pushed(connection, task_id)
        return True

    async def send_preload_stop(self, device_id: int, task_id: int) -> None:
        """服务端取消预转码任务时，通知该设备上执行中的 Worker 停下。

        ``reason=cancel``：Worker 据此删掉本地半成品（几十 GB 的源文件）。播放让位不走这里——
        Worker 自己停掉分段并上报 ``preload.yielded``，断点与本地文件都保留。
        发送失败不打紧——租约到 10 分钟自然回收，断点台账保留。
        """
        with self._lock:
            connection = next(
                (
                    c
                    for c in self._workers.values()
                    if c.login_device_id == device_id and self._is_fresh(c)
                ),
                None,
            )
        if connection is None:
            return
        try:
            await connection.send(
                {"type": "job.stop", "class": "preload", "task_id": task_id, "reason": "cancel"}
            )
        except Exception:  # noqa: BLE001
            logger.info(
                "预转码取消停止消息发送失败（租约会兜底）：device=%s task=%s",
                device_id,
                task_id,
            )

    async def start_job(
        self,
        connection: WorkerConnection,
        job_id: str,
        payload: dict[str, Any],
    ) -> str:
        """向已占位的 Worker 下发任务；发送失败自动归还槽位。"""
        try:
            await connection.send({"type": "job.start", "job_id": job_id, **payload})
        except Exception as exc:  # noqa: BLE001
            self._release_job(job_id)
            logger.warning("下发远程任务失败：worker=%s job=%s", connection.worker_id, job_id)
            raise RemoteWorkerUnavailable("无法向远程 Worker 下发任务") from exc
        return connection.worker_id

    async def cancel(self, job_id: str, *, force: bool = False) -> None:
        """通知 Worker 停止任务；消息发送失败时仍释放本地占用。

        seek 重启时旧分片已经没有交付价值，``force`` 让新版 Worker 直接杀掉
        ffmpeg，避免它和新轮次短暂并行读源、写上传；不传时保持普通停止的优雅
        退出。字段是可选的，旧版 Worker 会按原有优雅停止处理。
        """
        with self._lock:
            worker_id = self._job_workers.get(job_id)
            connection = self._workers.get(worker_id) if worker_id else None
        if connection is not None:
            try:
                message = {"type": "job.stop", "job_id": job_id}
                if force:
                    message["force"] = True
                await connection.send(message)
            except Exception:  # noqa: BLE001
                logger.info("远程 Worker 停止消息发送失败，按断线处理：job=%s", job_id)
        self._release_job(job_id)

    async def pause(self, job_id: str) -> bool:
        """暂停远程 ffmpeg，但保留任务映射，供磁盘低水位保护使用。"""
        sent = await self._send_job_control(job_id, "job.pause")
        if sent:
            with self._lock:
                self._paused_jobs.add(job_id)
                self._progress_samples.pop(job_id, None)
                self._job_speeds.pop(job_id, None)
        return sent

    async def resume(self, job_id: str) -> bool:
        """恢复被磁盘低水位暂停的远程 ffmpeg。"""
        sent = await self._send_job_control(job_id, "job.resume")
        if sent:
            with self._lock:
                self._paused_jobs.discard(job_id)
                self._progress_samples.pop(job_id, None)
                self._job_speeds.pop(job_id, None)
        return sent

    async def _send_job_control(self, job_id: str, message_type: str) -> bool:
        """向当前任务所属 Worker 发送控制消息，不释放任务槽位。"""
        with self._lock:
            worker_id = self._job_workers.get(job_id)
            connection = self._workers.get(worker_id) if worker_id else None
            if connection is None or not self._is_fresh(connection):
                return False
        try:
            await connection.send({"type": message_type, "job_id": job_id})
        except Exception:  # noqa: BLE001
            logger.info(
                "远程 Worker 控制消息发送失败：type=%s job=%s",
                message_type,
                job_id,
            )
            return False
        return True

    async def report_playback(self, job_id: str, playback: dict[str, Any]) -> None:
        """把观众的播放位置推给任务所在的 Worker（它的面板显示「看到 25:10 / 1:52:10」）。

        纯展示信息：没声明 ``playback_progress`` 的 Worker 不发，发送失败也不当回事——
        断线自有心跳与任务状态去判定。
        """
        with self._lock:
            worker_id = self._job_workers.get(job_id)
            connection = self._workers.get(worker_id) if worker_id else None
        if connection is None or not connection.capabilities.playback_progress:
            return
        try:
            await connection.send({"type": "job.playback", "job_id": job_id, **playback})
        except Exception:  # noqa: BLE001
            logger.debug("播放位置推送失败（仅影响 Worker 面板显示）：job=%s", job_id)

    def publish_job_event(self, job_id: str, message: dict[str, Any]) -> None:
        """把 Worker 的状态消息交给正在启动/重启的会话。"""
        with self._lock:
            self._job_states[job_id] = message
            queue = self._job_events.get(job_id)
            # 进度是诊断状态，不应占用启动阶段的 accepted/failed 等待队列；
            # 否则首个 progress 恰好先到时，会被会话误判为接单失败。
            if queue is None or message.get("type") == "job.progress":
                return
            if queue.full():
                # 队列只用于 accepted/failed 等少量控制消息；丢掉最旧事件避免
                # 一个异常 Worker 堵住整个事件循环。
                queue.get_nowait()
            queue.put_nowait(message)

    async def handle_message(
        self, connection: WorkerConnection, message: dict[str, Any]
    ) -> dict[str, Any] | None:
        """处理 Worker 上行消息。未知消息只记日志，不中断连接。

        返回值只对 ``job.artifact_failed``（Worker 放弃了某个产物的上传）与
        ``job.timeline``（Worker 这一轮的起播分段）有意义：校验过归属与轮次后原样
        交回调用方，由它转给会话层——注册表不认识会话。其余消息一律返回 None。"""
        with self._lock:
            connection.last_seen = time.monotonic()
        message_type = str(message.get("type", ""))
        job_id = str(message.get("job_id", ""))
        if message_type == "worker.heartbeat":
            self._update_load(connection, message.get("load"))
            await connection.send({"type": "worker.heartbeat.ack"})
            return None
        if message_type == "worker.draining":
            with self._lock:
                connection.draining = True
            logger.info("远程转码 Worker 进入排空状态：%s", connection.worker_id)
            return None
        if message_type == "worker.ready":
            with self._lock:
                connection.draining = False
            logger.info("远程转码 Worker 恢复接单：%s", connection.worker_id)
            return None
        if message_type == "worker.goodbye":
            logger.info("远程转码 Worker 主动断开：%s", connection.worker_id)
            return None
        if message_type == "job.failed" and str(message.get("class") or "") == "preload":
            # 预转码任务的 WS 拒单（未开 preload 角色被误派 / 本机并发已满）。
            # 任务立即回队重派，不等 10 分钟租约；归属校验防止别的 Worker
            # 伪造拒收把别人的任务踢回队列
            task_id = message.get("task_id")
            logger.warning(
                "预转码任务被 Worker 拒收：worker=%s task=%s：%s",
                connection.worker_id,
                task_id,
                message.get("error"),
            )
            if isinstance(task_id, int):
                # 记账释放放在回队协程里（归属校验通过之后）：伪造的拒单
                # 不能提前释放他机的在途账
                asyncio.create_task(
                    self._requeue_preload(
                        connection, task_id, reason="Worker 拒收（角色未开或并发已满）"
                    )
                )
            return None
        if message_type == "preload.yielded":
            # Worker 为播放让位（实时任务进来）停掉了预转码：记账释放、任务当场回队，
            # 不挂到 10 分钟租约到期。归属校验在协程里做（同拒收）
            task_id = message.get("task_id")
            logger.info(
                "预转码让位：worker=%s task=%s（播放优先，断点保留）", connection.worker_id, task_id
            )
            if isinstance(task_id, int):
                asyncio.create_task(self._requeue_yielded_preload(connection, task_id))
            return None
        if (
            message_type
            in {
                "job.accepted",
                "job.progress",
                "job.failed",
                "job.finished",
                "job.artifact_failed",
                "job.timeline",
            }
            and job_id
        ):
            # 任务状态只能由实际被选中的 Worker 上报；共享 Worker 令牌下，
            # 未做这层校验的另一台 Worker 可以伪造别人的完成/失败消息。
            with self._lock:
                is_owner = self._job_workers.get(job_id) == connection.worker_id
                expected_attempt = self._job_attempts.get(job_id, job_id)
                message_attempt = message.get("attempt_id", expected_attempt)
                is_current_attempt = message_attempt == expected_attempt
            if not is_owner or not is_current_attempt:
                return None
            if message_type == "job.progress":
                self._observe_speed(connection, job_id, message)
            if message_type in {"job.artifact_failed", "job.timeline"}:
                # 不是任务状态迁移，不能写进状态表（会盖掉 accepted/progress）；
                # 交回调用方转给会话层（补片记账 / 并进会话时间线）
                return message
            if message_type == "job.failed":
                # Worker 报来的失败此前只进会话诊断，网页播放器之外（Infuse、
                # VidHub）谁也看不到；这一行把原因、退出码、stderr 末尾留在 NAS 日志
                logger.warning(
                    "远程转码任务失败：worker=%s job=%s：%s",
                    connection.worker_id,
                    job_id,
                    describe_job_failure(message),
                )
            elif message_type == "job.finished":
                logger.info("远程转码任务已转完：worker=%s job=%s", connection.worker_id, job_id)
            self.publish_job_event(job_id, message)
            if message_type in {"job.failed", "job.finished"}:
                self._release_job(job_id)
            return None
        logger.debug(
            "忽略远程 Worker 未知消息：worker=%s type=%s",
            connection.worker_id,
            message_type,
        )
        return None

    # -- 内部 ------------------------------------------------------------

    async def _requeue_preload(
        self, connection: WorkerConnection, task_id: int, *, reason: str
    ) -> bool:
        """预转码任务当场回队（归属校验后）：在途记账释放、断点台账保留。返回是否回队。

        校验在释放记账之前：伪造的回队消息不能释放他机的在途账。失败等租约兜底。
        """
        try:
            from movieclaw_api.services.preload.dispatcher import return_task
            from movieclaw_db.engine import get_database
            from movieclaw_db.models import PreloadTask

            async with get_database().session() as session:
                task = await session.get(PreloadTask, task_id)
                if task is not None and task.worker_device_id == connection.login_device_id:
                    self.release_preload_task(task_id, connection.worker_id)
                    await return_task(session, task_id, reason=reason)
                    return True
        except Exception:  # noqa: BLE001
            logger.warning("预转码任务回队失败（租约兜底）：task=%s", task_id, exc_info=True)
        return False

    async def _requeue_yielded_preload(self, connection: WorkerConnection, task_id: int) -> None:
        """播放让位的预转码当场回队，并立刻派一轮。

        让位的 Worker 此刻余量为 0（有实时任务在跑），派单会跳过它，任务由别的 Worker 接手；
        本机播放结束后（``_release_job``）再派回来，从断点续转。
        """
        if await self._requeue_preload(connection, task_id, reason="播放让位"):
            from movieclaw_api.services.preload.dispatcher import trigger_dispatch

            trigger_dispatch()

    def _select_worker(
        self,
        backend: str,
        segment_type: str = "fmp4",
        *,
        disc: bool = False,
        plan: PlaybackPlan | None = None,
        source_id: int | None = None,
        yield_preemptible: bool = False,
    ) -> WorkerConnection | None:
        """``yield_preemptible``：把离线导出占着的名额也算空闲（播放视角）。"""
        with self._lock:
            preemptible = self._preemptible_jobs if yield_preemptible else set()
            candidates = [
                connection
                for connection in self._workers.values()
                if backend in connection.capabilities.backends
                and segment_type in connection.capabilities.segment_types
                # 实时任务只派给申报了 realtime 角色的 Worker：--no-realtime
                # 的机器仍会申报 nvenc 后端（预转码用），但没有资格接播放任务
                and "realtime" in connection.capabilities.roles
                and (not disc or connection.capabilities.disc_sources)
                and len(connection.jobs - preemptible) < connection.max_jobs
                and not connection.draining
                and self._is_fresh(connection)
            ]
            if not candidates:
                return None
            profile = (
                self._profile(plan, source_id)
                if plan is not None and source_id is not None
                else None
            )
            # 仅所有候选都有同规格实测时才比较速度，避免把未知当成最慢、永远不给它接单。
            compare_speed = profile is not None and all(
                c.speed_samples.get(profile, 0) >= 3 for c in candidates
            )

            def rank(item: WorkerConnection) -> tuple:
                load = item.load if self._load_fresh(item) else {}
                now = time.monotonic()
                # CPU 看不到媒体引擎/网络的瓶颈：已有任务接近实时速率时保留供片余量。
                slow = any(
                    samples >= 3 and now - seen <= 5 and speed < 1.25
                    for job in item.jobs
                    for speed, samples, seen in [self._job_speeds.get(job, (0, 0, 0))]
                )
                pressure = max(
                    load.get("memory_pressure", 0),
                    max(0, load.get("thermal_state", 0) - 1),
                    int(load.get("cpu", 0) >= 0.9),
                    int(slow),
                )
                return (
                    pressure,
                    len(item.jobs) / item.max_jobs,
                    -item.speeds[profile] if compare_speed else 0,
                    int(load.get("cpu", 0.5) * 10),
                    item.connected_at,
                )

            return min(candidates, key=rank)

    @staticmethod
    def _profile(plan: PlaybackPlan, source_id: int) -> tuple[Any, ...]:
        v = plan.video
        return (
            source_id,
            v.source_codec,
            v.source_bit_depth,
            v.height,
            v.tone_map,
            v.burn_subtitle,
            v.source_color,
            v.codec,
            v.action,
            v.bitrate_cap_bps,
            v.dolby_vision,
            plan.container,
            plan.audio,
        )

    @staticmethod
    def _load_fresh(connection: WorkerConnection) -> bool:
        return bool(connection.load) and time.monotonic() - connection.load_seen <= LOAD_TIMEOUT_S

    def _update_load(self, connection: WorkerConnection, raw: Any) -> None:
        if not isinstance(raw, dict):
            return
        cpu = raw.get("cpu")
        memory = raw.get("memory_pressure")
        thermal = raw.get("thermal_state")
        if (
            not isinstance(cpu, (int, float))
            or isinstance(cpu, bool)
            or not math.isfinite(cpu)
            or not 0 <= cpu <= 1
            or type(memory) is not int
            or memory not in (0, 1, 2)
            or type(thermal) is not int
            or thermal not in (0, 1, 2, 3)
        ):
            return
        with self._lock:
            previous = connection.load.get("cpu") if self._load_fresh(connection) else None
            connection.load = {
                "cpu": cpu if previous is None else previous * 0.5 + cpu * 0.5,
                "memory_pressure": memory,
                "thermal_state": thermal,
            }
            used = raw.get("memory_used_bytes")
            if type(used) is int and used >= 0:
                connection.load["memory_used_bytes"] = used
            connection.load_seen = time.monotonic()

    def _observe_speed(self, connection: WorkerConnection, job_id: str, message: dict) -> None:
        with self._lock:
            profile = self._job_profiles.get(job_id)
            output = message.get("out_time_ms")
            if job_id in self._paused_jobs or type(output) is not int:
                self._progress_samples.pop(job_id, None)
                return
            now = time.monotonic()
            previous = self._progress_samples.get(job_id)
            self._progress_samples[job_id] = (now, output)
            if previous is None:
                return
            elapsed = now - previous[0]
            # 消息过久没到可能是主动限速或网络停顿，不用于给设备性能排序。
            if not 0.5 <= elapsed <= 3 or output <= previous[1]:
                self._job_speeds.pop(job_id, None)
                return
            speed = (output - previous[1]) / 1000 / elapsed
            old_speed, samples, _ = self._job_speeds.get(job_id, (speed, 0, now))
            self._job_speeds[job_id] = (old_speed * 0.5 + speed * 0.5, samples + 1, now)
            if profile is None or len(connection.jobs) != 1:
                return
            if profile not in connection.speeds and len(connection.speeds) >= 32:
                oldest = next(iter(connection.speeds))
                connection.speeds.pop(oldest)
                connection.speed_samples.pop(oldest, None)
            old = connection.speeds.get(profile, speed)
            connection.speeds[profile] = old * 0.5 + speed * 0.5
            connection.speed_samples[profile] = connection.speed_samples.get(profile, 0) + 1

    def _release_job(self, job_id: str) -> None:
        idle_preload_worker = False
        with self._lock:
            worker_id = self._job_workers.pop(job_id, None)
            self._job_attempts.pop(job_id, None)
            self._job_profiles.pop(job_id, None)
            self._progress_samples.pop(job_id, None)
            self._job_speeds.pop(job_id, None)
            self._paused_jobs.discard(job_id)
            self._preemptible_jobs.discard(job_id)
            if worker_id:
                connection = self._workers.get(worker_id)
                if connection is not None:
                    connection.jobs.discard(job_id)
                    idle_preload_worker = (
                        not connection.jobs and "preload" in connection.capabilities.roles
                    )
        if idle_preload_worker:
            # 播放结束、这台 Worker 余量回来了：立刻派一轮（让位过的预转码从断点续转）。
            # 延迟导入：dispatcher 依赖本模块，反向导入会循环
            from movieclaw_api.services.preload.dispatcher import trigger_dispatch

            trigger_dispatch()

    def _mark_jobs_lost(self, connection: WorkerConnection, error: str) -> list[str]:
        """把某条连接上的任务统一标为失败，并释放 Worker 槽位。"""
        with self._lock:
            lost_jobs = [
                job_id
                for job_id, worker_id in self._job_workers.items()
                if worker_id == connection.worker_id
            ]
            for job_id in lost_jobs:
                self._job_workers.pop(job_id, None)
                self._job_attempts.pop(job_id, None)
                connection.jobs.discard(job_id)
                self._job_profiles.pop(job_id, None)
                self._progress_samples.pop(job_id, None)
                self._job_speeds.pop(job_id, None)
                self._paused_jobs.discard(job_id)
                self.publish_job_event(
                    job_id,
                    {"type": "job.failed", "job_id": job_id, "error": error},
                )
        return lost_jobs

    @staticmethod
    def _is_fresh(connection: WorkerConnection) -> bool:
        return time.monotonic() - connection.last_seen <= WORKER_IDLE_TIMEOUT_S

    @staticmethod
    def _parse_capabilities(raw: Any) -> WorkerCapabilities:
        raw = raw if isinstance(raw, dict) else {}
        backends = tuple(
            item
            for item in raw.get("backends", [])
            if isinstance(item, str) and item in _SUPPORTED_BACKENDS
        )
        encoders = tuple(
            item for item in raw.get("encoders", []) if isinstance(item, str)
        )
        if "videotoolbox" in backends and "h264_videotoolbox" not in encoders:
            backends = tuple(item for item in backends if item != "videotoolbox")
        if "nvenc" in backends and "h264_nvenc" not in encoders:
            backends = tuple(item for item in backends if item != "nvenc")
        try:
            max_jobs = max(1, min(8, int(raw.get("max_jobs", 1))))
        except (TypeError, ValueError):
            max_jobs = 1
        # v2：hello 直接报 roles；没报的按旧语义只有 realtime
        raw_roles = raw.get("roles")
        roles = frozenset(
            item
            for item in (raw_roles if isinstance(raw_roles, list) else [])
            if isinstance(item, str) and item in {"realtime", "preload"}
        ) or frozenset({"realtime"})
        segment_types = raw.get("segment_types")
        segment_types = tuple(
            item
            for item in (segment_types if isinstance(segment_types, list) else [])
            if isinstance(item, str) and item in _SUPPORTED_SEGMENT_TYPES
        )
        def names(key: str) -> tuple[str, ...] | None:
            value = raw.get(key)
            if not isinstance(value, list):
                return None
            return tuple(item for item in value if isinstance(item, str) and item)

        return WorkerCapabilities(
            backends=backends,
            encoders=encoders,
            ffmpeg_version=str(raw.get("ffmpeg_version"))
            if raw.get("ffmpeg_version")
            else None,
            platform=str(raw.get("platform")) if raw.get("platform") else None,
            max_jobs=max_jobs,
            roles=roles,
            playback_progress=raw.get("playback_progress") is True,
            # 没声明（旧版 Worker）按只会 fMP4 处理，见字段注释
            segment_types=segment_types or ("fmp4",),
            disc_sources=raw.get("disc_sources") is True,
            hw_decoders=names("hw_decoders") or _DEFAULT_HW_DECODERS,
            filters=names("filters") or (),
            progressive_segments=raw.get("progressive_segments") is True,
            read_options=names("read_options") or (),
            server_config=raw.get("server_config") is True,
            hardware={
                k: v
                for k, v in (raw.get("hardware") or {}).items()
                if (k == "chip" and isinstance(v, str) and len(v) <= 128)
                or (k in {"cpu_cores", "memory_bytes"} and type(v) is int and v > 0)
            }
            if isinstance(raw.get("hardware"), dict)
            else {},
        )

    @staticmethod
    async def _close_quietly(websocket: WebSocket, *, code: int, reason: str) -> None:
        with contextlib.suppress(Exception):
            await websocket.close(code=code, reason=reason)


_registry = RemoteWorkerRegistry()


def get_remote_worker_registry() -> RemoteWorkerRegistry:
    """取进程级远程 Worker 注册表。"""
    return _registry


def reset_remote_worker_registry() -> None:
    """测试用：清空同步状态；调用方负责先关闭实际 WebSocket。"""
    with _registry._lock:
        _registry._workers.clear()
        _registry._job_workers.clear()
        _registry._job_attempts.clear()
        _registry._job_events.clear()
        _registry._job_states.clear()
        _registry._job_profiles.clear()
        _registry._progress_samples.clear()
        _registry._job_speeds.clear()
        _registry._paused_jobs.clear()
        _registry._preload_task_workers.clear()


def remote_worker_enabled() -> bool:
    """开关打开、且（若填了）覆盖地址合法时，远程硬件才对决策层可见。

    地址本身不是前置条件：默认取自 Worker 连上来的地址（observed_base_url）。
    """
    config = effective_remote_transcode_config()
    return config.enabled and not remote_transcode_issues(config)


def effective_remote_transcode_config() -> RemoteTranscodeRuntimeConfig:
    """供同步调用方读取网页配置的运行时快照。"""
    return _effective_remote_transcode_config()


def remote_worker_available(
    backend: str = "videotoolbox",
    segment_type: str = "fmp4",
    *,
    disc: bool = False,
    strict: bool = False,
) -> bool:
    """供播放决策/执行层同步查询远程硬件是否在线且有空闲槽位（``disc``：还要能读原盘）。

    离线导出占着的槽位对播放算空闲（播放来了它让路）；``strict`` 只认真正空着的。"""
    return _registry.has_capable_worker(backend, segment_type, disc=disc, strict=strict)


def remote_workers_have_encoder(encoder: str, backend: str = "videotoolbox") -> bool:
    """在线、能执行这个后端的 Worker 都申报了这个编码器（任务派给谁由调度定，得人人都有）。"""
    return _registry.workers_have_encoder(encoder, backend)


def remote_worker_busy(backend: str = "videotoolbox") -> bool:
    """远程硬件在线但名额全满（见 ``RemoteWorkerRegistry.has_busy_worker``）。"""
    return _registry.has_busy_worker(backend)
