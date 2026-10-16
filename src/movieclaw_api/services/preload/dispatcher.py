"""预转码任务的调度器（Worker 统一调度 v2 的服务端半边）。

队列、原子抢占、断点台账、租约回收全部沿用 ``service.py`` 的数据库状态
机；这里负责**选执行者并把任务交给它**：

- **本地优先**（Q3 自动调度）：NAS 有可用硬件且播放不吃紧时，任务由本机
  后台执行器跑掉——产物直接落盘（不经 Worker 的下载/上传回路，快得多）；
- **远程兜底**：本地没硬件/避让中/HDR 源装不出正确命令时，按 v2 推送
  派单交给带 preload 角色的 Worker（数据面照旧走 REST）。

不与播放抢资源的口径（占用制避让）：本机在跑的**播放**转码会话数 ≥1 时
本地预转码不再起新任务（已起的继续跑完——省流档本来就是要占机器的活，
播放会话有它的低水位保护管着）。Worker 侧同理（T2 预算 = 并发−1）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.services.playback.remote_worker import (
    WorkerConnection,
    get_remote_worker_registry,
)
from movieclaw_api.services.preload import service as preload_service
from movieclaw_api.services.preload.service import ARTIFACT_UPLOAD_NAME
from movieclaw_api.services.preload.signing import issue_preload_grant
from movieclaw_db.engine import get_database
from movieclaw_db.models import PreloadTask, PreloadTaskStatus

logger = logging.getLogger("movieclaw_api.preload.dispatcher")

#: 与 claim 长轮询时代同一个有效期（签的是整个任务周期）
GRANT_TTL_S = 24 * 3600

#: 本地预转码同时最多跑几个（软编链按核数折算，与 limits.auto_transcode_concurrency
#: 软件档同一口径）。预转码是小时级后台活，不跟播放抢即时性。
_LOCAL_MAX_CONCURRENCY = 1

#: 本地执行连续失败的熔断线：attempts 在每次抢占时 +1，坏源/坏参数的任务
#: 回队重转会无限烧算力（推送失败回队不受此限——那是环境问题不是任务问题）
_LOCAL_FAIL_MAX_ATTEMPTS = 3

_dispatch_lock: asyncio.Lock | None = None
_dispatch_lock_loop: asyncio.AbstractEventLoop | None = None
_dispatch_rerun = False


def _get_dispatch_lock() -> asyncio.Lock:
    """派发单飞锁（按事件循环惰性创建；测试的多个循环各自一把）。

    两个并发触发点（下单 / 租约回收）同时派发时，会各自读到旧的容量快照
    然后都去推送——Worker 端信号量把超发任务排成幻影线程，租约到期回收
    再派，churn 循环。同一循环内串行化即可根除。
    """
    global _dispatch_lock, _dispatch_lock_loop
    loop = asyncio.get_running_loop()
    if _dispatch_lock is None or _dispatch_lock_loop is not loop:
        _dispatch_lock = asyncio.Lock()
        _dispatch_lock_loop = loop
    return _dispatch_lock


async def dispatch_queued_tasks() -> None:
    """调度排队任务：本地优先，远程 Worker 兜底。

    单飞：一轮进行中时再触发只记「要重跑」标记，本轮结束后自动补一轮——
    队列不会因为并发触发而漏派，也不会两个循环同时读容量互相超发。
    """
    global _dispatch_rerun
    lock = _get_dispatch_lock()
    if lock.locked():
        _dispatch_rerun = True
        return
    async with lock:
        while True:
            _dispatch_rerun = False
            await _dispatch_round()
            if not _dispatch_rerun:
                return


async def _dispatch_round() -> None:
    """一轮派发（调用方保证持有派发锁）。"""
    dispatched = 0
    # 本地能吃几个就先吃几个（每轮重读容量：占用是动态的）
    while _local_capacity() > 0:
        async with get_database().session() as session:
            claimed = await _claim_for(session, device_id=None)
            if claimed is None:
                break
            task, file = claimed
            command = preload_service.build_task_command(task, file, worker_caps=None)
            if command["unusable_reason"] or command["video_copy"]:
                # 本地装不出正确命令（HDR 无 tonemapx）或直拷形态：回队交给
                # 远程 Worker（jellyfin-ffmpeg 有 tonemapx；直拷的远程路径
                # 已有）。**必须跳出本地循环**——队列头就是这个任务，留在
                # 循环里会原样再抢一次
                await return_task(session, task.id, reason="本地不可执行，转远程")
                break
            _run_local(task, file, command)
            dispatched += 1
    # 远程派单：余量按 Worker 记账，队列空或全员满员为止
    async with get_database().session() as session:
        while True:
            if await _dispatch_one_remote(session) is None:
                break
            dispatched += 1
    if dispatched:
        logger.info("预转码调度：%d 个任务已分发（本地/远程）", dispatched)


async def _dispatch_one_remote(session: AsyncSession) -> int | None:
    """挑一台有余量的 Worker 派一个任务；没得派返回 None。"""
    registry = get_remote_worker_registry()
    workers = registry.preload_workers()
    if not workers:
        return None
    for worker in workers:
        if registry.preload_capacity(worker) <= 0:
            continue
        claimed = await _claim_for(session, device_id=worker.login_device_id)
        if claimed is None:
            return None  # 队列空了
        task, file = claimed
        # 硬编能力从 hello 申报取（与实时同源），软编回落；凭据绑定认领者，
        # 任务被回收重派后旧 Worker 的长凭据写不进产物
        payload = await _payload_for(
            session, task, file, _preload_caps(worker), device_id=worker.login_device_id
        )
        ok = await registry.send_preload_job(worker, payload)
        if ok:
            return 1
        await return_task(session, task.id, reason="推送失败")
        # 这台连接可疑，本轮不再派给它
    return None


def _preload_caps(worker: WorkerConnection) -> dict:
    """从 hello 申报的能力拼 build_task_command 要的 caps dict（NVENC 链判定用）。

    与 REST claim 时代 Worker 上报的 ``{encoders, filters, hw_decoders}`` 同构；
    只报 nvenc 编码器时 encoders 取 nvenc 两个（probe_caps 的原始集合）。
    """
    caps = worker.capabilities
    encoders: list[str] = []
    if "nvenc" in caps.backends:
        encoders = [e for e in caps.encoders if e.endswith("_nvenc")]
    return {
        "encoders": encoders,
        "filters": list(caps.filters),
        "hw_decoders": list(caps.hw_decoders),
    }


async def _claim_for(session: AsyncSession, *, device_id: int | None):
    """抢占下一个排队任务（沿用 claim_next_task 的原子抢占）。

    ``device_id=None`` 表示本地执行器抢占（worker_device_id 写 NULL）。
    """
    return await preload_service.claim_next_task(session, device_id=device_id)


async def _payload_for(
    session: AsyncSession, task, file, worker_caps: dict | None, *, device_id: int | None
) -> dict:
    """组装与 claim 长轮询完全同构的 job.start 载荷（Worker 端只认这一种）。"""
    grant = await issue_preload_grant(
        task_id=task.id, file_id=file.id, ttl_seconds=GRANT_TTL_S, device_id=device_id
    )
    command = preload_service.build_task_command(
        task, file, worker_caps=worker_caps
    )
    command["source_url"] = (
        f"api/v1/preload-worker/preload-source/{task.id}?token={grant}"
    )
    command["artifact_url"] = (
        f"api/v1/preload-worker/tasks/{task.id}"
        f"/artifacts/{ARTIFACT_UPLOAD_NAME}?token={grant}"
    )
    command["task_id"] = task.id
    command["display_name"] = Path(file.file_path).name
    command["resume_parts"] = task.resume_parts
    command["part_bytes"] = task.part_bytes
    return command


async def return_task(session: AsyncSession, task_id: int, *, reason: str) -> None:
    """当场退回队列（断点台账保留，下一次触发点重派）。"""
    task = await session.get(PreloadTask, task_id)
    if task is not None and task.status in (
        PreloadTaskStatus.CLAIMED,
        PreloadTaskStatus.RUNNING,
    ):
        task.status = PreloadTaskStatus.QUEUED
        task.worker_device_id = None
        task.claimed_at = None
        task.lease_expires_at = None
        await session.commit()
        logger.warning("预转码任务已退回队列：task=%s（%s）", task_id, reason)


async def return_task_or_fail(session: AsyncSession, task_id: int, *, reason: str) -> None:
    """本地执行失败回队；同任务反复失败（毒源/坏参数）直接置失败。

    回队重转对「播放抢占 / 服务重启」这类环境中断是正确语义，但对坏源是
    无限重转——每次触发点都会把整部电影重新烧一遍。attempts 在每次抢占时
    +1，连续超过熔断线就置 FAILED 交给用户处置（可重试）。
    """
    task = await session.get(PreloadTask, task_id)
    if task is None:
        return
    if task.status not in (PreloadTaskStatus.CLAIMED, PreloadTaskStatus.RUNNING):
        return
    if task.attempts >= _LOCAL_FAIL_MAX_ATTEMPTS:
        await preload_service.fail_task(
            session, task, f"连续失败 {task.attempts} 次，已熔断：{reason}"
        )
    else:
        await return_task(session, task_id, reason=reason)


def trigger_dispatch() -> None:
    """从同步上下文安全地触发一次派单（无事件循环时丢弃，等下一个触发点）。"""
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(dispatch_queued_tasks())


# ---------------------------------------------------------------------------
# 本地执行器：NAS 后台跑软编预转码（本地优先路径）
# ---------------------------------------------------------------------------

#: 进程内本地任务记账（task_id → 协程句柄）。服务重启后这里的内存丢了
#: 也无碍：租约 10 分钟到期回队，断点台账保留。
_LOCAL_RUNNING: dict[int, asyncio.Task] = {}


def _local_capacity() -> int:
    """本地还能起几个预转码任务。

    两道闸：
    - **硬件可用**：纯软编在 NAS 上是幻灯片（省流档 1080p 也要小时级），
      只有硬件（核显）能跑出可用的速度。硬解+软编混合链没有探测依据，
      不赌；
    - **播放避让**：有任何一个播放转码会话在跑就不再起新任务——播放是
      即时性活，预转码是后台活，让路的永远是后者。
    """
    from movieclaw_api.services.playback.hwprobe import available_local_backends
    from movieclaw_api.services.playback.session import get_session_manager

    if not available_local_backends():
        return 0
    if any(
        s.tier is not None and s.tier >= 3 for s in get_session_manager().active()
    ):
        return 0
    return max(0, _LOCAL_MAX_CONCURRENCY - len(_LOCAL_RUNNING))


def _run_local(task, file, command: dict) -> None:
    """把任务交给后台协程执行（调用方已完成抢占）。"""
    runner = asyncio.create_task(_execute_local_task(task.id, file, command["argv"]))
    _LOCAL_RUNNING[task.id] = runner
    runner.add_done_callback(lambda _t, tid=task.id: _LOCAL_RUNNING.pop(tid, None))
    logger.info("预转码任务由本地执行：task=%s（软编链）", task.id)


def cancel_local_task(task_id: int) -> bool:
    """取消本地执行器在跑的任务（服务层 cancel_task 的本地臂）。"""
    runner = _LOCAL_RUNNING.get(task_id)
    if runner is None or runner.done():
        return False
    runner.cancel()
    logger.info("本地预转码任务已请求取消：task=%s", task_id)
    return True


async def _execute_local_task(
    task_id: int, file, argv: list[str]
) -> None:
    """跑一条本地预转码命令并登记产物（与 Worker 的 REST 数据面等价）。

    与 Worker 路径的差异：源直接读 NAS 盘上的原文件（argv 里的 source.mkv
    换成真实路径），产物先写协议名（artifact.mkv），完成后改名为动态片名
    （与 REST complete 的 concat 目标同名），再调 complete 的服务层校验登记。
    失败/异常时任务回队（断点保留）——本地路径随时可能因播放抢占而中断，
    回队让下一个触发点重派。
    """
    from movieclaw_api.services.media_probe import probe_media

    directory = preload_service.artifact_dir_for(task_id)
    directory.mkdir(parents=True, exist_ok=True)
    # argv 尾部是 [-f, matroska, <output>]；输入的 source.mkv 是 Worker
    # 落盘约定名，本地直接换成真实源路径
    local_argv = list(argv)
    input_index = local_argv.index("-i")
    local_argv[input_index + 1] = file.file_path
    tmp_path = directory / ARTIFACT_UPLOAD_NAME
    local_argv[-1] = str(tmp_path)
    final_path = None
    process = None
    try:
        # 续约线程：任务跑几小时，租约 10 分钟一续
        stop_renew = asyncio.Event()
        renewer = asyncio.create_task(_renew_local_lease(task_id, stop_renew))
        try:
            process = await asyncio.create_subprocess_exec(
                "ffmpeg", *local_argv,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await process.communicate()
        finally:
            stop_renew.set()
            renewer.cancel()
        if process.returncode != 0:
            tail = (stderr or b"")[-600:].decode(errors="replace")
            logger.warning(
                "本地预转码失败：task=%s 退出码 %s：%s", task_id, process.returncode, tail
            )
            async with get_database().session() as session:
                await return_task_or_fail(
                    session, task_id, reason=f"ffmpeg 退出码 {process.returncode}"
                )
            return
        spec = probe_media(str(tmp_path))
        async with get_database().session() as session:
            from movieclaw_db.models import PreloadTask

            task = await session.get(PreloadTask, task_id)
            if task is None:
                return
            # 与 REST complete 同一收尾：产物定名为动态片名后再校验登记
            #（complete_task 按 artifact_path_for 找文件——协议名不是它要的）
            final_path = await preload_service.artifact_path_for(session, task)
            if final_path != tmp_path:
                tmp_path.replace(final_path)
            else:
                final_path = None  # 源条目已失效：complete 会拦下，占位文件留给 finally 清
            derived = await preload_service.complete_task(
                session,
                task,
                actual_bytes=(final_path or tmp_path).stat().st_size,
                duration_seconds=spec.duration_seconds if spec else None,
                video_codec=spec.video_codec if spec else None,
                spec=spec,
            )
        final_path = None  # 已登记为版本（file_path 指向它），finally 不再清理
        logger.info("本地预转码完成：task=%s → file=%s", task_id, derived.id)
    except asyncio.CancelledError:
        # 用户取消：communicate() 被取消不会杀子进程——手动收掉，否则小时级
        # ffmpeg 成孤儿继续烧机，容量位还被 done_callback 提前还给派单器
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        raise
    except Exception:  # noqa: BLE001
        logger.exception("本地预转码异常：task=%s", task_id)
        async with get_database().session() as session:
            await return_task_or_fail(session, task_id, reason="本地执行异常")
    finally:
        # 失败/异常时协议名与改名后的半成品都留在目录里会污染下一轮
        #（断点续转按段文件算），清掉；成功时 final_path 已置 None
        with contextlib.suppress(OSError):
            if final_path is not None and final_path.exists():
                final_path.unlink()
        with contextlib.suppress(OSError):
            if tmp_path.exists():
                tmp_path.unlink()


async def _renew_local_lease(task_id: int, stop: asyncio.Event) -> None:
    """本地执行期间持续续租（与 Worker 的 progress 上报同一节奏）。"""
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=60)
        if stop.is_set():
            return
        async with get_database().session() as session:
            await preload_service.renew_lease(session, task_id)
            await session.commit()
