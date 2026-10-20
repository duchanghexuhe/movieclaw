"""预转码任务的调度器（Worker 统一调度 v2 的服务端半边，§7）。

队列、原子抢占、断点台账、租约回收全部沿用 ``service.py`` 的数据库状态机；这里负责**选执行者并把
任务交给它**：

- **远程 Worker**：在线且有余量的 Worker 按预算接单，队首单它领不走（HEVC 没有硬件编码、或已有分段
  归别人）就换下一台；
- **NAS 本地**（§7）：正经的执行者，不是兜底。跑与 Worker 同一套分段协议（视频/音频按
  ``SEGMENT_SECONDS`` 分段，分段直接写进任务目录，推进断点台账，中断后续转），命令走 NAS 的硬件链
  （QSV/VAAPI，无硬件时 libx264）。调度规则：

  * 有能转这一单的 Worker 在线时，NAS 不接新单，让 GPU 按顺序清队，NAS 留给家里播放；
  * 没有可用 Worker（离线、能力不符、排空）时，NAS 接单；
  * NAS 已在跑的任务跑完为止，不中途交给 Worker（不同编码器的分段不能混拼）；
  * **播放优先**：本机开始任何播放转码会话，本地预转码立刻停下（杀掉 ffmpeg、保留断点、任务回队）；
    播放全部结束后再续派。看门狗每 2 秒看一次播放状态，这是“立刻”的实现方式。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.services.playback.remote_worker import (
    WorkerConnection,
    get_remote_worker_registry,
)
from movieclaw_api.services.preload import service as preload_service
from movieclaw_api.services.preload.ffmpeg_preload import (
    LOCAL_HW_BACKENDS,
    LocalHw,
    TrackPlan,
    segment_argv,
)
from movieclaw_api.services.preload.service import ARTIFACT_UPLOAD_NAME
from movieclaw_api.services.preload.signing import issue_preload_grant
from movieclaw_db.engine import get_database
from movieclaw_db.models import LibraryFile, PreloadTask, PreloadTaskStatus

logger = logging.getLogger("movieclaw_api.preload.dispatcher")

#: 与 claim 长轮询时代同一个有效期（签的是整个任务周期）
GRANT_TTL_S = 24 * 3600

#: NAS 本地同时最多跑几个预转码（硬件链一个就够吃满，小时级后台活）
_LOCAL_MAX_CONCURRENCY = 1

#: 看门狗的巡检周期：播放开始到预转码停下的上限延迟
_WATCH_INTERVAL = 2.0

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
    """调度排队任务：远程 Worker 优先，NAS 本地按规则接单。

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
    # 远程 Worker 先吃（每轮实时记账：占用是动态的），队列空或全员满员为止
    async with get_database().session() as session:
        while True:
            if await _dispatch_one_remote(session) is None:
                break
            dispatched += 1
    # NAS 本地：有硬件走硬件链，没有硬件时 H.264 用 libx264 软编（慢，但 NAS 本地是基石——
    # 不是每家都有 Worker，Worker 也不总在线；HEVC 仍要硬件）。播放在跑时容量为 0；能转哪一类
    # 单由「有没有能转它的 Worker 在线」决定
    _ensure_watchdog()  # 播放让位后要续派，看门狗负责这一步
    if _local_capacity() > 0:
        local = await _local_hardware()
        software_caps = None if local is not None else await asyncio.to_thread(_software_caps)
        workers = get_remote_worker_registry().preload_workers()
        allow_h264 = not workers
        allow_hevc = local is not None and local.hevc_encoder is not None and not any(
            preload_service.caps_accept_hevc(_preload_caps(w)) for w in workers
        )
        while _local_capacity() > 0:
            async with get_database().session() as session:
                claimed = await preload_service.claim_local_task(
                    session, allow_h264=allow_h264, allow_hevc=allow_hevc
                )
                if claimed is None:
                    break
                task, file = claimed
                plan = await preload_service.plan_for(session, file)
                command = preload_service.build_task_command(
                    task, file, worker_caps=software_caps, local=local, plan=plan
                )
                if command["unusable_reason"]:
                    # 本机装不出正确命令（如 HDR 没有任何 tone-map）：回队等远程，循环必须就此停，
                    # 否则队首就是它，会原样再抢一次
                    await return_task(session, task.id, reason="本地不可执行，转远程")
                    break
                _run_local(task, file, command)
                dispatched += 1
    if dispatched:
        logger.info("预转码调度：%d 个任务已分发（远程/本地）", dispatched)


async def _dispatch_one_remote(session: AsyncSession) -> int | None:
    """挑一台有余量、且领得走队首单的 Worker 派一个任务；没得派返回 None。

    按能力过滤（HEVC 单只派给有 hevc_nvenc 的 Worker）：这台没有可领的单就换下一台，
    不能就此停——否则一台 CPU Worker 会挡住后面的 NVENC Worker。
    """
    registry = get_remote_worker_registry()
    for worker in registry.preload_workers():
        if registry.preload_capacity(worker) <= 0:
            continue
        caps = _preload_caps(worker)
        claimed = await _claim_for(
            session,
            device_id=worker.login_device_id,
            accept_hevc=preload_service.caps_accept_hevc(caps),
        )
        if claimed is None:
            continue  # 这台没有可领的单（队列空，或剩下的只有它做不了的 HEVC / 别人的分段）
        task, file = claimed
        # 硬编能力从 hello 申报取（与实时同源），软编回落；凭据绑定认领者，
        # 任务被回收重派后旧 Worker 的长凭据写不进产物
        plan = await preload_service.plan_for(session, file)
        payload = await _payload_for(
            session, task, file, caps, device_id=worker.login_device_id, plan=plan
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


async def _claim_for(session: AsyncSession, *, device_id: int | None, accept_hevc: bool = True):
    """抢占下一个排队任务（沿用 claim_next_task 的原子抢占与分段归属规则）。"""
    return await preload_service.claim_next_task(
        session, device_id=device_id, accept_hevc=accept_hevc
    )


async def _payload_for(
    session: AsyncSession,
    task,
    file,
    worker_caps: dict | None,
    *,
    device_id: int | None,
    plan: TrackPlan,
) -> dict:
    """组装与 claim 长轮询完全同构的 job.start 载荷（Worker 端只认这一种）。"""
    grant = await issue_preload_grant(
        task_id=task.id, file_id=file.id, ttl_seconds=GRANT_TTL_S, device_id=device_id
    )
    command = preload_service.build_task_command(
        task, file, worker_caps=worker_caps, plan=plan
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


async def return_task(
    session: AsyncSession, task_id: int, *, reason: str, yielded: bool = False
) -> None:
    """当场退回队列（断点台账保留，下一次触发点重派）。

    ``yielded``：是播放让位而停（不是失败）——这次领取不计入熔断次数。已有分段的任务保留执行者。
    """
    task = await session.get(PreloadTask, task_id)
    if task is not None and task.status in (
        PreloadTaskStatus.CLAIMED,
        PreloadTaskStatus.RUNNING,
    ):
        task.status = PreloadTaskStatus.QUEUED
        task.claimed_at = None
        task.lease_expires_at = None
        task.progress = None
        preload_service.release_owner(task)
        if yielded:
            task.attempts = max(0, task.attempts - 1)
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
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(dispatch_queued_tasks())


# ---------------------------------------------------------------------------
# NAS 本地执行器（§7）
# ---------------------------------------------------------------------------

#: 进程内本地任务记账（task_id → 协程句柄）。服务重启后这里的内存丢了
#: 也无碍：租约到期回队，断点台账保留。
_LOCAL_RUNNING: dict[int, asyncio.Task] = {}
#: 正在被播放让位的任务（只发一次取消，避免清理途中被再次打断）
_PREEMPTING: set[int] = set()

def detect_local_hw() -> LocalHw | None:
    """NAS 本机的硬件链能力（QSV/VAAPI）。阻塞：调用方放线程池（见 :func:`_local_hardware`）。

    第一个探测通过的后端胜出（顺序同播放端）；原生 tone-map、HEVC 编码器、tonemapx 都在这里
    按自检结论给出，装配层只读它。没有硬件返回 None（本地只能软编 H.264）。
    """
    from movieclaw_api.services.playback.ffmpeg_args import (
        HW_BACKENDS,
        encoder_options,
        native_tonemap_filter,
    )
    from movieclaw_api.services.playback.hwprobe import (
        available_local_backends,
        local_filter_available,
        local_hevc_encoder,
    )

    names = [name for name in available_local_backends() if name in LOCAL_HW_BACKENDS]
    if not names:
        return None
    name = names[0]
    backend = HW_BACKENDS[name]
    return LocalHw(
        backend=name,
        hevc_encoder=local_hevc_encoder(name),
        native_tonemap=native_tonemap_filter(backend),
        has_tonemapx=local_filter_available("tonemapx"),
        encoder_opts=tuple(encoder_options(backend)),
    )


async def _local_hardware() -> LocalHw | None:
    """本机硬件能力。探测结果在 hwprobe 里已缓存，这里每轮重算（便宜）；阻塞调用放线程池。"""
    return await asyncio.to_thread(detect_local_hw)


def _playback_transcoding() -> bool:
    """本机是否有任何播放转码会话在跑（远程会话与离线导出不算：导出本身会让位给播放）。"""
    from movieclaw_api.services.playback.session import get_session_manager
    from movieclaw_playback.decide import PlaybackTier

    return any(
        session.tier >= PlaybackTier.AUDIO_TRANSCODE
        and not getattr(session, "remote", False)
        and getattr(session, "export_id", None) is None
        for session in get_session_manager().active()
    )


def _software_caps() -> dict | None:
    """没有硬件时 NAS 走软编链：本机 ffmpeg 有 tonemapx（镜像里的 jellyfin-ffmpeg）时 HDR 也能
    在 CPU 上做色调映射；没有就只转 SDR，HDR 单留给 Worker。阻塞：调用方放线程池。"""
    from movieclaw_api.services.playback.hwprobe import local_filter_available

    return {"filters": ["tonemapx"]} if local_filter_available("tonemapx") else None


def _local_capacity() -> int:
    """NAS 还能同时跑几个预转码。播放在跑时为 0（播放优先，NAS 不接新单）；没有硬件不再是 0——
    软编也接（见 :func:`_dispatch_round`）。"""
    if _playback_transcoding():
        return 0
    return max(0, _LOCAL_MAX_CONCURRENCY - len(_LOCAL_RUNNING))


def _run_local(task, file, command: dict) -> None:  # noqa: ARG001 - 与远程派单签名对齐
    """把任务交给后台协程执行（调用方已完成抢占）。"""
    task_id = task.id
    runner = asyncio.create_task(_execute_local_task(task_id, command))
    _ensure_watchdog()
    _LOCAL_RUNNING[task_id] = runner
    runner.add_done_callback(lambda _t, tid=task_id: _LOCAL_RUNNING.pop(tid, None))
    logger.info(
        "预转码任务由 NAS 本地执行：task=%s（%d 段）", task_id, command["segment_total"]
    )


def cancel_local_task(task_id: int) -> bool:
    """取消本地执行器在跑的任务（服务层 cancel_task 的本地臂）。"""
    runner = _LOCAL_RUNNING.get(task_id)
    if runner is None or runner.done():
        return False
    runner.cancel()
    logger.info("本地预转码任务已请求取消：task=%s", task_id)
    return True


class _LocalStopped(Exception):
    """播放开始：本段停下，任务回队（不是失败）。"""


class _LocalFailure(Exception):
    """本段 ffmpeg 失败。"""


async def _execute_local_task(task_id: int, command: dict) -> None:
    """NAS 本机分段转码（与 Worker 的数据面等价：分段进任务目录、断点台账推进、拼接登记）。

    每段一个 ffmpeg 进程，完成即落盘并推进 ``resume_parts``；中途被播放抢占（看门狗取消本协程，
    或段与段之间发现播放在跑）只杀掉当前进程、删掉半截段，任务回队，断点保留。
    """
    from movieclaw_api.services import media_probe

    directory = preload_service.artifact_dir_for(task_id)
    directory.mkdir(parents=True, exist_ok=True)
    holder: dict[str, Any] = {"process": None}
    stop_renew = asyncio.Event()
    renewer = asyncio.create_task(_renew_local_lease(task_id, stop_renew))
    segment_tmp: Path | None = None
    try:
        async with get_database().session() as session:
            task = await session.get(PreloadTask, task_id)
            file = await session.get(LibraryFile, task.target_file_id) if task else None
            if task is None or file is None:
                return
            source_path = file.file_path
            resume = task.resume_parts
        total = command["segment_total"]
        seconds = command["segment_seconds"]
        started_at = time.monotonic()
        for part in range(resume, total):
            if _playback_transcoding():
                raise _LocalStopped("播放开始，预转码让位")
            segment_tmp = directory / f"seg{part:05d}.part.mkv"
            argv = segment_argv(
                command["argv"],
                part=part,
                total=total,
                seconds=seconds,
                source=source_path,
                output=str(segment_tmp),
            )
            seg_started = time.monotonic()
            code, tail = await _run_ffmpeg(holder, argv)
            if code != 0:
                raise _LocalFailure(f"ffmpeg 退出码 {code}：{tail}")
            segment_tmp.replace(directory / f"seg{part:05d}.mkv")
            segment_tmp = None
            done = part + 1 - resume
            per_segment = (time.monotonic() - started_at) / max(done, 1)
            async with get_database().session() as session:
                await preload_service.record_part_count(session, task_id, part=part)
                await _set_local_progress(
                    session,
                    task_id,
                    {
                        "phase": "transcoding",
                        "segment_index": part + 1,
                        "segment_total": total,
                        "eta_seconds": int(per_segment * (total - part - 1)),
                        "segment_seconds_taken": round(time.monotonic() - seg_started, 1),
                    },
                )
        async with get_database().session() as session:
            task = await session.get(PreloadTask, task_id)
            derived = await preload_service.finalize_artifact(
                session, task, probe=media_probe.probe_media
            )
        logger.info("NAS 本地预转码完成：task=%s → file=%s", task_id, derived.id)
    except _LocalStopped as exc:
        await _yield_to_playback(task_id, str(exc))
    except asyncio.CancelledError:
        # 播放让位（看门狗取消）或服务关停：先收掉 ffmpeg，任务回队保留断点；用户取消时任务
        # 已是 CANCELLED，return_task 不动它
        _kill_process(holder)
        await _yield_to_playback(task_id, "本机预转码中断（播放或停机）")
        raise
    except _LocalFailure as exc:
        logger.warning("本地预转码失败：task=%s %s", task_id, exc)
        async with get_database().session() as session:
            await return_task_or_fail(session, task_id, reason=str(exc)[:500])
    except Exception:  # noqa: BLE001
        logger.exception("本地预转码异常：task=%s", task_id)
        async with get_database().session() as session:
            await return_task_or_fail(session, task_id, reason="本地执行异常")
    finally:
        stop_renew.set()
        renewer.cancel()
        _PREEMPTING.discard(task_id)
        with contextlib.suppress(OSError):
            if segment_tmp is not None and segment_tmp.exists():
                segment_tmp.unlink()


async def _run_ffmpeg(holder: dict[str, Any], argv: list[str]) -> tuple[int, str]:
    """跑一段 ffmpeg；进程挂在 holder 上，被取消时由调用方杀掉。"""
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", *argv,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    holder["process"] = process
    try:
        _, stderr = await process.communicate()
    finally:
        holder["process"] = None
    return process.returncode or 0, (stderr or b"")[-600:].decode(errors="replace")


def _kill_process(holder: dict[str, Any]) -> None:
    """取消时收掉 ffmpeg：``communicate()`` 被取消不会杀子进程，手动收，
    否则小时级 ffmpeg 成孤儿。"""
    process = holder.get("process")
    if process is not None and process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()


async def _yield_to_playback(task_id: int, reason: str) -> None:
    """播放让位：任务回队（断点保留，不计失败次数）。在被取消的协程里做 DB 操作是允许的。"""
    async with get_database().session() as session:
        await return_task(session, task_id, reason=reason, yielded=True)


async def _set_local_progress(session: AsyncSession, task_id: int, progress: dict) -> None:
    """本地执行的进度落台账（活动页据此显示第几段/剩余时间），并把执行中状态记为 RUNNING。"""
    task = await session.get(PreloadTask, task_id)
    if task is None:
        return
    if task.status == PreloadTaskStatus.CLAIMED:
        task.status = PreloadTaskStatus.RUNNING
    task.progress = progress
    await preload_service.renew_lease(session, task_id)
    await session.commit()


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


# ---------------------------------------------------------------------------
# 播放让位看门狗（§7）
# ---------------------------------------------------------------------------

_WATCHDOG: asyncio.Task | None = None


def _ensure_watchdog() -> None:
    """启动看门狗（幂等）。没有事件循环（同步上下文）时什么也不做，等下一个触发点。"""
    global _WATCHDOG
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    if _WATCHDOG is None or _WATCHDOG.done():
        _WATCHDOG = asyncio.create_task(_watchdog_loop())


def _preempt_local_runners() -> None:
    """播放在跑：让所有本地执行协程停下（each 取消一次，清理在协程内完成）。"""
    for task_id, runner in list(_LOCAL_RUNNING.items()):
        if runner.done() or task_id in _PREEMPTING:
            continue
        _PREEMPTING.add(task_id)
        runner.cancel()
        logger.info("播放开始：本地预转码让位，task=%s", task_id)


async def _has_open_tasks() -> bool:
    """还有没结束的任务（排队/领取/执行中）吗？看门狗据此决定要不要继续巡检。"""
    from sqlalchemy import func, select

    async with get_database().session() as session:
        count = (
            await session.execute(
                select(func.count()).select_from(PreloadTask).where(
                    PreloadTask.status.in_(
                        [
                            PreloadTaskStatus.QUEUED,
                            PreloadTaskStatus.CLAIMED,
                            PreloadTaskStatus.RUNNING,
                        ]
                    )
                )
            )
        ).scalar_one()
    return bool(count)


async def _watchdog_loop() -> None:
    """每 2 秒：播放在跑就让本地执行停下；播放停了就续派。没有开放任务时退出。"""
    while True:
        try:
            if _playback_transcoding():
                _preempt_local_runners()
            else:
                await dispatch_queued_tasks()
            if not _LOCAL_RUNNING and not await _has_open_tasks():
                return
        except Exception:  # noqa: BLE001 - 巡检失败不能让看门狗死掉
            logger.warning("预转码看门狗巡检失败", exc_info=True)
        await asyncio.sleep(_WATCH_INTERVAL)
