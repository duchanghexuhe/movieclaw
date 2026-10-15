"""预转码任务的 WS 推送派单器（Worker 统一调度 v2 的服务端半边）。

队列、原子抢占、断点台账、租约回收全部沿用 ``service.py`` 的数据库状态
机；这里只做一件事：**在合适的时机把排队的任务推给合适的 Worker**。
预转码出队从 REST 长轮询改为 WS 推送后，下单到开转的延迟不再取决于
Worker 的轮询间隔；数据面（分段上传 / 进度续约 / complete / fail）照旧
走 REST——预转码是小时级任务，WS 掉线重连不该打断它。

并发预算（T2 拍板）：``max_concurrency − 1``，realtime 永远优先——并发=1
的机器不接预转码；实时任务把余量吃掉后不再派新单，在跑的预转码由
``job.stop`` 抢占兜底（任务回队、断点保留）。
"""

from __future__ import annotations

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
from movieclaw_db.models import PreloadTaskStatus

logger = logging.getLogger("movieclaw_api.preload.dispatcher")

#: 与 claim 长轮询时代同一个有效期（签的是整个任务周期）
GRANT_TTL_S = 24 * 3600


async def dispatch_queued_tasks() -> None:
    """把排队任务推给有余量的 Worker；没有 Worker/余量时静默返回。

    幂等：抢占成功但推送失败的行会当场回队，重试由下一个触发点（下单、
    任务结束、Worker 上线、租约回收）驱动。
    """
    registry = get_remote_worker_registry()
    workers = registry.preload_workers()
    if not workers:
        return
    # 余量按 Worker 记账：一台余量耗尽就换下一台，直到队列空或全员满员
    capacity = {c.worker_id: registry.preload_capacity(c) for c in workers}
    if not any(v > 0 for v in capacity.values()):
        return
    dispatched = 0
    async with get_database().session() as session:
        while any(v > 0 for v in capacity.values()):
            worker = next(c for c in workers if capacity[c.worker_id] > 0)
            claimed = await _claim_for(session, device_id=worker.login_device_id)
            if claimed is None:
                break  # 队列空了
            task, file = claimed
            # 硬编能力从 hello 申报取（与实时同源），软编回落
            payload = await _payload_for(session, task, file, _preload_caps(worker))
            ok = await registry.send_preload_job(worker, payload)
            capacity[worker.worker_id] -= 1
            if ok:
                dispatched += 1
            else:
                await _return_task(session, task.id, reason="推送失败")
                capacity[worker.worker_id] = 0  # 这台连接可疑，本轮不再派给它
    if dispatched:
        logger.info("预转码推送派单：%d 个任务已下发", dispatched)


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
    """抢占下一个排队任务（沿用 claim_next_task 的原子抢占）。"""
    return await preload_service.claim_next_task(
        session, device_id=device_id or 0
    )


async def _payload_for(
    session: AsyncSession, task, file, worker_caps: dict | None
) -> dict:
    """组装与 claim 长轮询完全同构的 job.start 载荷（Worker 端只认这一种）。"""
    grant = await issue_preload_grant(
        task_id=task.id, file_id=file.id, ttl_seconds=GRANT_TTL_S
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


async def _return_task(session: AsyncSession, task_id: int, *, reason: str) -> None:
    """推送失败当场退回队列（断点台账保留，下一次触发点重派）。"""
    from movieclaw_db.models import PreloadTask

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


async def preempt_for_realtime(device_id: int) -> None:
    """实时任务需要并发位时，抢占该设备最早的一个在跑预转码任务。

    预算公式保证正常情况下用不着走到这里（realtime 永远优先）；兜底场景是
    同机既有 preload 又有 realtime 角色时，实时占用与预转码占用错峰变化的
    边界。抢占 = 任务回队 + 给 Worker 发 job.stop（Worker 停掉 ffmpeg，
    断点台账在服务端，重派时自动续转）。
    """
    from sqlalchemy import select

    from movieclaw_db.models import PreloadTask

    registry = get_remote_worker_registry()
    async with get_database().session() as session:
        task = (
            await session.execute(
                select(PreloadTask)
                .where(
                    PreloadTask.worker_device_id == device_id,
                    PreloadTask.status.in_(
                        [PreloadTaskStatus.CLAIMED, PreloadTaskStatus.RUNNING]
                    ),
                )
                .order_by(PreloadTask.claimed_at.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if task is None:
            return
        task_id = task.id
        task.status = PreloadTaskStatus.QUEUED
        task.worker_device_id = None
        task.claimed_at = None
        task.lease_expires_at = None
        await session.commit()
        logger.info(
            "预转码任务被实时任务抢占回队：task=%s device=%s（断点保留）",
            task_id,
            device_id,
        )
    # job.stop 语义复用实时路径：消息带 class=preload，Worker 只停预转码任务
    await registry.send_preload_stop(device_id, task_id)


def trigger_dispatch() -> None:
    """从同步上下文安全地触发一次派单（无事件循环时丢弃，等下一个触发点）。"""
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(dispatch_queued_tasks())
