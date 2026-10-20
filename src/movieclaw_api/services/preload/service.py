"""预转码服务层（docs/design/preload-transcode.md）。

职责：下单（体积预估 + 配额预检）、任务队列状态机、Worker 领取与租约、
产物校验登记、取消/重试、冷数据清理、派生物失效（源删除/洗版联动）。

任务表（``PreloadTask``）自管状态机而不用 services/jobs.py 的进程内
dispatcher：预转码任务由**外部 Worker** 执行（在线时领、离线无碍），没有
进程内 handler 可跑；Job 模型的 lease/heartbeat 语义对不上"一台 Windows
机器半夜领走任务转 2 小时"的形态。排队去重、台账、失败重试的**思想**与
jobs.py 一致，实现是独立的小状态机。

产物（``LibraryFile`` 派生行）落在 data/preload/<task_id>/，文件名带片名与
码率（``artifact_file_name``，例「坠落2：死点 (2026) - 省流 1080p HEVC
1.5Mbps.mkv」）：登记后 file_path 指向它，播放/详情页把它当普通版本对待
（探测、字幕轨、播放决策全部复用既有链路）；派生身份靠
derives_from_file_id / derivation。
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.exceptions import (
    AppException,
    BadRequestException,
    NotFoundException,
)
from movieclaw_api.services.media_probe import PROBE_SCHEMA_VERSION, MediaSpec
from movieclaw_api.services.preload.ffmpeg_preload import (
    FORMAT_DEFAULT,
    FORMAT_HEVC,
    PRELOAD_PART_BYTES,
    PRESET_DEFAULT,
    LocalHw,
    TrackPlan,
    build_preload_command,
    effective_preset,
    estimate_size_bytes,
    final_mux_argv,
    plan_tracks,
    preset_available,
    segment_total,
)
from movieclaw_api.settings import PreloadSetting
from movieclaw_api.settings.store import get_setting_store
from movieclaw_db.engine import get_database
from movieclaw_db.models import (
    FileState,
    LibraryFile,
    MediaItem,
    PreloadTask,
    PreloadTaskStatus,
    utcnow,
)

logger = logging.getLogger("movieclaw_api.preload")

#: 领取租约：Worker 领走任务后必须按时上报进度续约；到期未续自动回队。
LEASE_SECONDS = 10 * 60

#: 分段上传 URL 里的产物名：纯协议标识（Worker 把分段 PUT 到
#: /artifacts/<名>，complete 时才由服务端拼装成最终名）。固定名避开中文
#: 文件名的 URL 编码问题；产物最终名见 artifact_file_name
ARTIFACT_UPLOAD_NAME = "artifact.mkv"

#: 转码分段时长（§7 断点续转）：每段独立 ffmpeg 进程转（-ss/-t 切），完成时
#: 服务端 concat demuxer 拼接。被打断时只重转未完成的段（上传台账 resume_parts
#: 记录已收到的连续段数，Worker 重领时跳过）
SEGMENT_SECONDS = 600

#: 产物登记时的来源快照（library_file.origin 的展示文案口径）
_PRELOAD_ORIGIN = {"kind": "preload", "label": "预转码版本", "detail": None}


def _preset_of(task: PreloadTask) -> dict:
    """任务的档位定义（历史行的格式/档位异常时回退默认档的 H.264 形态）。"""
    return effective_preset(task.preset_id, task.video_format)


class PreloadQuotaExceeded(AppException):
    """配额预检未过（§6 三件套之一：防呆兜底）。"""

    def __init__(self, needed: int, free: int) -> None:
        super().__init__(
            status_code=409,
            code="PRELOAD_QUOTA_EXCEEDED",
            message=(
                f"预转码配额不足：本单预估 {needed / 1024**3:.1f} GB，"
                f"剩余 {max(free, 0) / 1024**3:.1f} GB。请先清理冷数据或调大配额"
            ),
        )


# ---------------------------------------------------------------------------
# 配额
# ---------------------------------------------------------------------------


async def preload_usage_bytes(session: AsyncSession) -> int:
    """当前预转码产物总占用（派生行的体积和）。"""
    row = (
        await session.execute(
            select(func.coalesce(func.sum(LibraryFile.size_bytes), 0)).where(
                LibraryFile.derivation.is_not(None),  # type: ignore[union-attr]
            )
        )
    ).scalar_one()
    return int(row)


#: 在途任务的状态：还没登记成产物，但预估体积已经要占配额
_INFLIGHT_STATUSES = (
    PreloadTaskStatus.QUEUED,
    PreloadTaskStatus.CLAIMED,
    PreloadTaskStatus.RUNNING,
)


async def preload_inflight_bytes(session: AsyncSession) -> int:
    """在途任务（排队/领取/执行中）的预估体积之和。"""
    row = (
        await session.execute(
            select(func.coalesce(func.sum(PreloadTask.estimated_bytes), 0)).where(
                PreloadTask.status.in_(_INFLIGHT_STATUSES)
            )
        )
    ).scalar_one()
    return int(row)


async def check_quota(session: AsyncSession, *, needed_bytes: int) -> None:
    """配额预检（§6）：现有产物 + 在途任务预估 + 本单预估 ≤ 配额，否则拒绝入队。

    在途任务必须计入：否则整季一批下单在产物登记前全都能过预检，合起来照样超配额。
    """
    setting = await get_setting_store().get(PreloadSetting)
    used = await preload_usage_bytes(session) + await preload_inflight_bytes(session)
    if used + needed_bytes > setting.quota_bytes:
        raise PreloadQuotaExceeded(needed_bytes, setting.quota_bytes - used)


def preload_root() -> Path:
    """产物根目录（与 storage registry 的 key=preload 同一 resolve）。"""
    from movieclaw_api.core.config import get_settings

    path = Path(get_settings().preload_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def artifact_dir_for(task_id: int) -> Path:
    """一个任务一个子目录：临时分片、最终产物、失败清理都以它为单位。"""
    return preload_root() / str(task_id)


_WIN_ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|]')

#: 产物文件名的编码展示写法（video_codec 值 → 人话）
_CODEC_LABELS = {"h264": "H.264", "hevc": "HEVC"}


def sanitize_filename(text: str) -> str:
    """Windows 文件名净化：非法字符折成空格，压缩空白并去首尾的点/空格。"""
    cleaned = re.sub(r"\s+", " ", _WIN_ILLEGAL_CHARS.sub(" ", text)).strip(" .")
    return cleaned or "未命名"


def _bitrate_label(spec: str) -> str:
    """ffmpeg 码率字面量 → 展示写法（"1.5M" → "1.5Mbps"）。"""
    text = spec.strip().lower()
    if text.endswith("m"):
        return f"{text[:-1]}Mbps"
    if text.endswith("k"):
        return f"{text[:-1]}Kbps"
    return f"{text}bps"


def preset_title(preset: dict) -> str:
    """档位标题（下单弹窗胶囊的悬停提示）：省流（HEVC · 1080p · 1.5Mbps）。

    规格随格式（用户只认「省流/高清」两个字），标题把实际规格亮出来。
    """
    codec = _CODEC_LABELS.get(preset.get("video_codec", "h264"), "H.264")
    return (
        f"{preset.get('short', '预转码')}（{codec} · {preset['height']}p"
        f" · {_bitrate_label(preset['maxrate'])}）"
    )


def artifact_file_name(
    *,
    media_title: str,
    year: int | None,
    kind: str | None,
    season_number: int | None,
    episode_number: int | None,
    preset: dict,
) -> str:
    """产物文件名：片名(+年份/剧集号) - 档位 分辨率 编码 码率.mkv。

    版本列表与下载直接显示这个文件名，所以要自带片名与码率，而不是一个
    通用占位（例：「坠落2：死点 (2026) - 省流 1080p HEVC 1.5Mbps.mkv」）。
    """
    title = sanitize_filename(media_title)[:80]
    if year:
        title = f"{title} ({year})"
    if kind == "tv" and season_number and episode_number:
        title = f"{title} S{season_number:02d}E{episode_number:02d}"
    codec = _CODEC_LABELS.get(preset.get("video_codec", "h264"), "H.264")
    spec = (
        f"{preset.get('short', '预转码')} {preset['height']}p"
        f" {codec} {_bitrate_label(preset['maxrate'])}"
    )
    return f"{title} - {spec}.mkv"


async def artifact_path_for(session: AsyncSession, task: PreloadTask) -> Path:
    """产物最终落盘路径（complete 时按源条目的片名定名）。

    片名拿不到（源行/条目已删）退回协议名——这条路径随后就会被时长/源存在
    性校验拦下，不影响作废流程。
    """
    directory = artifact_dir_for(task.id or 0)
    source = await session.get(LibraryFile, task.target_file_id)
    item = (
        await session.get(MediaItem, source.media_item_id)
        if source is not None and source.media_item_id
        else None
    )
    if source is None or item is None:
        return directory / ARTIFACT_UPLOAD_NAME
    return directory / artifact_file_name(
        media_title=item.title,
        year=item.year,
        kind=item.kind,
        season_number=source.season_number,
        episode_number=source.episode_number,
        preset=_preset_of(task),
    )


# ---------------------------------------------------------------------------
# 下单与查询
# ---------------------------------------------------------------------------


def _source_spec(file: LibraryFile) -> dict:
    """台账行 → ffmpeg 参数构造要的规格 dict。"""
    return {
        "resolution": file.resolution,
        "video_codec": file.video_codec,
        "hdr": file.hdr,
        "dv_profile": file.dv_profile,
        "color_space": file.color_space,
        "bit_depth": file.bit_depth,
        "duration_seconds": file.duration_seconds,
        "audio_streams": file.audio_streams or [],
        "subtitle_streams": file.subtitle_streams or [],
        "chapters": file.chapters or [],
    }


async def plan_for(session: AsyncSession, file: LibraryFile) -> TrackPlan:
    """这一单保留哪些轨（§5）：默认轨策略取库语言与原始语言，与播放端同一份上下文。"""
    from movieclaw_api.services.playback.track_context import track_context

    return plan_tracks(file, await track_context(session, file))


async def get_target_file(session: AsyncSession, file_id: int) -> LibraryFile:
    file = await session.get(LibraryFile, file_id)
    if file is None or file.state == FileState.TRASHED:
        raise NotFoundException("源文件不存在或已进回收站")
    return file


async def create_preload_task(
    session: AsyncSession,
    *,
    file_id: int,
    preset_id: str = PRESET_DEFAULT,
    video_format: str = FORMAT_DEFAULT,
) -> PreloadTask:
    """下单：源文件 → 预转码任务（§3 入口；预估 + 配额预检后排队）。

    - 格式在**下单时快照**到任务行：之后改全局设置不影响在途任务，
      重试/断点续转也始终按下单时的格式装配命令；
    - 同源同档已有活跃任务时幂等返回现有任务（详情页按钮防连点）；
    - 直拷判定命中（源已 ≤ 档位高度且是 H.264/SDR）拒绝——没有要转的东西；
    - 原盘目录、已有派生产物再派生同样拒绝。
    """
    if not preset_available(preset_id, video_format):
        raise BadRequestException(f"未知档位：{preset_id}")
    file = await get_target_file(session, file_id)
    if file.is_disc():
        raise BadRequestException("原盘目录暂不支持预转码")
    if file.derivation is not None:
        raise BadRequestException("预转码产物不能再作为预转码的源")

    existing = (
        await session.execute(
            select(PreloadTask).where(
                PreloadTask.target_file_id == file_id,
                PreloadTask.preset_id == preset_id,
                PreloadTask.status.in_(
                    [
                        PreloadTaskStatus.QUEUED,
                        PreloadTaskStatus.CLAIMED,
                        PreloadTaskStatus.RUNNING,
                    ]
                ),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    preset = effective_preset(preset_id, video_format)
    plan = await plan_for(session, file)
    command = build_preload_command(
        source_path=file.file_path,
        output_path="preview.mkv",
        preset=preset,
        source=_source_spec(file),
        tracks=plan,
    )
    if command.video_copy:
        # 直拷形态：源已经就是档位（≤档位高度的 H.264 SDR），转出来是同一个
        # 文件——明确拒绝而不是白耗一次上传下载
        raise BadRequestException("这个版本已经是便携档（≤档位高度的 H.264 SDR），无需转码")

    estimated = estimate_size_bytes(
        duration_seconds=file.duration_seconds,
        preset=preset,
        audio_tracks=len(plan.audio),
    )
    await check_quota(session, needed_bytes=estimated)

    task = PreloadTask(
        target_file_id=file_id,
        preset_id=preset_id,
        video_format=video_format,
        status=PreloadTaskStatus.QUEUED,
        estimated_bytes=estimated,
        part_bytes=PRELOAD_PART_BYTES,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    logger.info(
        "预转码下单：file=%s → task=%s（预估 %.1f GB）",
        file_id, task.id, estimated / 1024**3,
    )
    pruned = await prune_finished_tasks(session)
    if pruned:
        logger.info("预转码台账清理：删除 %d 条过期终态任务", pruned)
    return task


#: 终态任务台账只保留最近 N 条：任务行只在下单时增长，下单时顺手清理即可
#: 封顶。列表接口展示最近 100 条，N 留一倍余量——被清掉的旧任务不会出现在
#: 用户眼前的列表里。
FINISHED_TASK_KEEP = 200


async def prune_finished_tasks(session: AsyncSession) -> int:
    """终态任务只保留最近 ``FINISHED_TASK_KEEP`` 条，返回删除的行数。

    只清终态（completed/failed/cancelled/invalidated）——活跃任务（排队/
    领取/执行中）不在清理范围；进行中的实时进度与断点续转台账不受影响。
    删任务行不动产物（``result_file_id`` 是任务指向产物的单向引用）。
    """
    finished = [
        PreloadTaskStatus.COMPLETED,
        PreloadTaskStatus.FAILED,
        PreloadTaskStatus.CANCELLED,
        PreloadTaskStatus.INVALIDATED,
    ]
    stale = (
        (
            await session.execute(
                select(PreloadTask.id)
                .where(PreloadTask.status.in_(finished))
                .order_by(PreloadTask.created_at.desc())
                .offset(FINISHED_TASK_KEEP)
            )
        )
        .scalars()
        .all()
    )
    if not stale:
        return 0
    await session.execute(delete(PreloadTask).where(PreloadTask.id.in_(stale)))
    await session.commit()
    return len(stale)


async def cancel_task(session: AsyncSession, task_id: int) -> PreloadTask:
    """取消任务（§3：可取消）。已领取的任务照样取消；半成品目录一并清理。"""
    task = await session.get(PreloadTask, task_id)
    if task is None:
        raise NotFoundException("任务不存在")
    if task.status in (PreloadTaskStatus.COMPLETED, PreloadTaskStatus.CANCELLED):
        raise BadRequestException(f"任务已结束（{task.status}），不能取消")
    worker_device_id = task.worker_device_id
    task.status = PreloadTaskStatus.CANCELLED
    task.lease_expires_at = None
    task.updated_at = utcnow()
    await session.commit()
    # 半成品目录在提交后清理（失败只记日志，取消本身已成立）
    shutil.rmtree(artifact_dir_for(task_id), ignore_errors=True)
    logger.info("预转码任务已取消：task=%s", task_id)
    if worker_device_id is None:
        # 本地执行器（worker_device_id 为 NULL）在跑的任务：取消它的后台协程；
        # 排队中的任务（也是 NULL）没有协程，cancel_local_task 无操作
        from movieclaw_api.services.preload.dispatcher import cancel_local_task

        cancel_local_task(task_id)
        return task
    from movieclaw_api.services.playback.remote_worker import get_remote_worker_registry

    registry = get_remote_worker_registry()
    # 推送记账随终态释放（Worker 被叫停后的各路径都不上报 fail，不释放会
    # 把这台 Worker 的预转码预算永久压到 0）
    registry.release_preload_task(task_id)
    # 通知执行中的 Worker 停下来（杀掉在跑的分段 ffmpeg）；它稍后的
    # fail/complete 上报会被终态校验挡住，不产生任何状态变化
    await registry.send_preload_stop(worker_device_id, task_id)
    return task


async def retry_task(session: AsyncSession, task_id: int) -> PreloadTask:
    """失败任务重新排队（§3：可重试）。断点台账保留，续转不丢。"""
    task = await session.get(PreloadTask, task_id)
    if task is None:
        raise NotFoundException("任务不存在")
    if task.status != PreloadTaskStatus.FAILED:
        raise BadRequestException("只有失败的任务可以重试")
    task.status = PreloadTaskStatus.QUEUED
    task.error = None
    release_owner(task)
    task.claimed_at = None
    task.lease_expires_at = None
    # 熔断计数清零：attempts 是「连续失败」语义，手动重试意味着新一轮
    task.attempts = 0
    task.updated_at = utcnow()
    await session.commit()
    await session.refresh(task)
    return task


#: 产物被删/被冷数据清理时，挂着它的已完成任务转「已失效」的原因（可重新转码）
PRODUCT_REMOVED_REASON = "预转码版本已删除，可重新转码"
PRODUCT_STALE_REASON = "预转码版本已被冷数据清理，可重新转码"


async def _invalidate_tasks_of(session: AsyncSession, product_file_id: int, reason: str) -> None:
    """产物行即将删除：挂着它的已完成任务转「已失效」。

    必须在删行之前调用——result_file_id 是 ON DELETE SET NULL，删完就找不回这张任务，
    它会一直显示「转码完成」，而产物已经不在了。
    """
    tasks = (
        await session.execute(
            select(PreloadTask).where(
                PreloadTask.result_file_id == product_file_id,
                PreloadTask.status == PreloadTaskStatus.COMPLETED,
            )
        )
    ).scalars().all()
    for task in tasks:
        task.status = PreloadTaskStatus.INVALIDATED
        task.error = reason
        task.updated_at = utcnow()


async def delete_derived_file(session: AsyncSession, file_id: int) -> str:
    """删除一个预转码产物（版本列表的删除按钮 / 单文件删除的派生分支）：删文件 + 删行 + 删目录。

    返回被删的产物路径（调用方回显给用户）。
    """
    file = await session.get(LibraryFile, file_id)
    if file is None or file.derivation is None:
        raise NotFoundException("预转码产物不存在")
    path = Path(file.file_path)
    if path.exists():
        try:
            path.unlink()
        except OSError:
            logger.warning("删除预转码产物失败（保留台账行）：%s", path, exc_info=True)
            raise BadRequestException("产物文件删除失败，请检查磁盘") from None
    await _invalidate_tasks_of(session, file_id, PRODUCT_REMOVED_REASON)
    await session.delete(file)
    await session.commit()
    # 任务目录里只剩产物：一并清掉
    shutil.rmtree(path.parent, ignore_errors=True)
    logger.info("预转码产物已删除：file=%s", file_id)
    return str(path)


async def list_tasks(
    session: AsyncSession,
    *,
    statuses: list[PreloadTaskStatus] | None = None,
    limit: int = 100,
) -> list[PreloadTask]:
    stmt = select(PreloadTask).order_by(PreloadTask.created_at.desc()).limit(limit)
    if statuses:
        stmt = stmt.where(PreloadTask.status.in_(statuses))
    return list((await session.execute(stmt)).scalars())


# ---------------------------------------------------------------------------
# Worker 领取 / 进度 / 产物登记
# ---------------------------------------------------------------------------


async def reclaim_expired_leases(session: AsyncSession) -> int:
    """租约到期未续的任务回队（断点台账保留）。由定时任务与领取路径顺带调用。"""
    now = utcnow()
    rows = list(
        (
            await session.execute(
                select(PreloadTask).where(
                    PreloadTask.status.in_(
                        [PreloadTaskStatus.CLAIMED, PreloadTaskStatus.RUNNING]
                    ),
                    PreloadTask.lease_expires_at.is_not(None),  # type: ignore[union-attr]
                    PreloadTask.lease_expires_at < now,  # type: ignore[operator]
                )
            )
        ).scalars()
    )
    for task in rows:
        task.status = PreloadTaskStatus.QUEUED
        task.lease_expires_at = None
        task.claimed_at = None
        release_owner(task)
        task.progress = None  # 上一次执行的进度不属于回队后的执行
        task.updated_at = now
        logger.info("预转码任务租约到期回队：task=%s", task.id)
    if rows:
        await session.commit()
        # 回队 = 推送记账终态：卡死 Worker 的在途账立即归还，否则重派会让
        # 预算越扣越负（重派到已删源的任务直接 FAILED，永远等不到上报来释放）
        from movieclaw_api.services.playback.remote_worker import get_remote_worker_registry

        registry = get_remote_worker_registry()
        for task in rows:
            registry.release_preload_task(task.id)
        # 回队的任务可能立刻有 Worker 能接：触发一次推送派单（在事件循环里
        # 才触发，定时任务/测试的同步上下文由下一个触发点兜底）
        try:
            from movieclaw_api.services.preload.dispatcher import trigger_dispatch

            trigger_dispatch()
        except Exception:  # noqa: BLE001 - 派单失败不影响回收本身
            logger.warning("租约回队后的派单触发失败", exc_info=True)
    return len(rows)


async def claim_next_task(
    session: AsyncSession,
    *,
    device_id: int | None = None,
    max_parts_bytes: int | None = None,
    accept_hevc: bool = True,
) -> tuple[PreloadTask, LibraryFile] | None:
    """领取下一个排队任务（§7：在线时领、离线无碍）。

    ``accept_hevc=False``：执行者没有 hevc_nvenc 时跳过 HEVC 单（HEVC 只有 NVENC 一条链，
    领了也装不出命令）。是跳过而不是占住队首——H.264 单照常派。

    ``device_id=None`` 是本地执行器抢占（worker_device_id 写 NULL——
    它不走 Worker 的 REST 数据面，台账里 NULL 表达「不归任何一台 Worker」；
    服务重启后由租约回收兜底）。原子性：UPDATE ... WHERE status=queued
    抢占式改状态，两台 Worker 并发领取不会拿到同一个任务（SQLite 写锁
    串行化保证）。
    """
    await reclaim_expired_leases(session)
    # 已有分段的任务只回到产出它的执行者（见 release_owner）：别人接手会把两种编码器的分段拼在一起
    query = select(PreloadTask).where(
        PreloadTask.status == PreloadTaskStatus.QUEUED,
        or_(
            PreloadTask.resume_parts == 0,  # type: ignore[operator]
            PreloadTask.worker_device_id == device_id,  # type: ignore[arg-type]
        ),
    )
    if not accept_hevc:
        query = query.where(_not_hevc())
    row = (
        await session.execute(query.order_by(PreloadTask.created_at.asc()).limit(1))
    ).scalar_one_or_none()
    if row is None:
        return None
    return await _claim_row(session, row, device_id=device_id)


async def claim_local_task(
    session: AsyncSession, *, allow_h264: bool, allow_hevc: bool
) -> tuple[PreloadTask, LibraryFile] | None:
    """NAS 本机领单（§7）：队列里本机能转、且该本机领的第一单。

    - 已有分段、归本机的任务（被播放让位后回队的）：本机必须接着转，不管有没有 Worker 在线；
    - 从未出过段的新单：只在**没有能转它的 Worker 在线**时才接——有 Worker 就让 GPU 清队，
      NAS 留给家里播放。``allow_h264`` / ``allow_hevc`` 由调用方按「有没有能转它的 Worker」给出；
    - HEVC 单还要求本机真跑过 HEVC 硬件编码（``allow_hevc``）。
    """
    await reclaim_expired_leases(session)
    pinned = and_(PreloadTask.resume_parts > 0, PreloadTask.worker_device_id.is_(None))  # type: ignore[union-attr]
    usable = [pinned] if allow_hevc else [and_(pinned, _not_hevc())]
    if allow_h264:
        usable.append(and_(PreloadTask.resume_parts == 0, _not_hevc()))  # type: ignore[operator]
    if allow_hevc:
        usable.append(and_(PreloadTask.resume_parts == 0, _is_hevc()))  # type: ignore[operator]
    row = (
        await session.execute(
            select(PreloadTask)
            .where(PreloadTask.status == PreloadTaskStatus.QUEUED, or_(*usable))
            .order_by(PreloadTask.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return await _claim_row(session, row, device_id=None)


def _is_hevc():  # type: ignore[no-untyped-def]
    return PreloadTask.video_format == FORMAT_HEVC  # type: ignore[union-attr]


def _not_hevc():  # type: ignore[no-untyped-def]
    return or_(
        PreloadTask.video_format.is_(None),  # type: ignore[union-attr]
        PreloadTask.video_format != FORMAT_HEVC,  # type: ignore[operator]
    )


def release_owner(task: PreloadTask) -> None:
    """回到队列时释放领取者。已有分段的任务保留执行者：续转只能回到同一种执行者（NAS 本地 =
    无设备，Worker = 其设备），不同编码器产出的分段不能混拼。从未出过段的任务随便谁接。"""
    if task.resume_parts == 0:
        task.worker_device_id = None


async def _claim_row(
    session: AsyncSession, row: PreloadTask, *, device_id: int | None
) -> tuple[PreloadTask, LibraryFile] | None:
    """抢占一行（原子：UPDATE ... WHERE status=queued）并装入源文件；源已失效则直接失败。"""
    now = utcnow()
    changed = (
        await session.execute(
            update(PreloadTask)
            .where(PreloadTask.id == row.id, PreloadTask.status == PreloadTaskStatus.QUEUED)
            .values(
                status=PreloadTaskStatus.CLAIMED,
                worker_device_id=device_id,
                claimed_at=now,
                lease_expires_at=now + timedelta(seconds=LEASE_SECONDS),
                attempts=PreloadTask.attempts + 1,
                progress=None,  # 旧执行的最后进度不属于新执行
            )
        )
    ).rowcount
    if not changed:
        return None
    await session.commit()
    task = await session.get(PreloadTask, row.id)
    file = await session.get(LibraryFile, task.target_file_id)
    if file is None or file.state != FileState.IN_PLACE:
        # 源已失效：任务直接失败（不回队——回队也会再撞上同一个源）
        task.status = PreloadTaskStatus.FAILED
        task.error = "源文件已删除或失效"
        await session.commit()
        return None
    return task, file


def build_task_command(
    task: PreloadTask,
    file: LibraryFile,
    *,
    worker_caps: dict | None = None,
    local: LocalHw | None = None,
    plan: TrackPlan | None = None,
) -> dict[str, Any]:
    """领取响应里的命令载荷：argv + 元数据（执行者原样执行 argv）。

    ``worker_caps`` 是 Worker 在 claim 请求里上报的本机能力
    （``{encoders, filters, hw_decoders}``）：
    有 NVENC + ``scale_cuda`` 就下发硬编链，没有落回 libx264 软编。``local`` 是 NAS 本机的
    QSV/VAAPI 能力（见 ``LocalHw``），二者互斥。``plan`` 是这一单保留的轨（:func:`plan_for`）。
    """
    hw_backend = "nvenc" if local is None and _caps_have_nvenc(worker_caps) else None
    command = build_preload_command(
        source_path="source.mkv",
        output_path=ARTIFACT_UPLOAD_NAME,
        preset=_preset_of(task),
        source=_source_spec(file),
        tracks=plan,
        hw_backend=hw_backend,
        worker_caps=worker_caps,
        local=local,
    )
    seconds = 0 if command.video_copy else SEGMENT_SECONDS
    return {
        "argv": command.argv,
        "preset_id": task.preset_id,
        "tone_map": command.tone_map,
        "video_copy": command.video_copy,
        "hw_backend": command.hw_backend,
        # 非空 = 这条命令装不出来（HDR 缺 tonemap、HEVC 缺硬件编码器），领取方据此拒做并说明
        "unusable_reason": command.unusable_reason,
        "skipped_subtitle_indexes": command.skipped_subtitle_indexes,
        "resume_parts": task.resume_parts,
        "part_bytes": task.part_bytes,
        # 断点续转（§7）：视频与音频按固定时长分段（字幕不分段，拼接时内封）。执行者对第 i 段在
        # -i 前插 "-ss i*SEGMENT_SECONDS"、在输入后插 "-t"（末段转到 EOF）；服务端按 resume_parts
        # 跳过已收段，complete 时拼接。video_copy（直拷）不分段
        "segment_seconds": seconds,
        "segment_total": segment_total(file.duration_seconds, seconds),
        "duration_seconds": file.duration_seconds,
        "source_size_bytes": file.size_bytes,
    }


def _caps_have_nvenc(caps: dict | None) -> bool:
    """NVENC 全 GPU 链的最低门槛：任一 NVENC 编码器 + scale_cuda（缺一 4K 缩放就断）。
    档位要的是哪个编码器，由 build_preload_command 逐档判定。"""
    if not caps:
        return False
    encoders = {str(e).lower() for e in caps.get("encoders") or []}
    filters = {str(f).lower() for f in caps.get("filters") or []}
    return bool({"h264_nvenc", "hevc_nvenc"} & encoders) and "scale_cuda" in filters


def caps_accept_hevc(caps: dict | None) -> bool:
    """执行者能不能吃 HEVC 单：只有 NVENC 一条链（hevc_nvenc + scale_cuda，与
    build_preload_command 的 use_nvenc 同口径）。"""
    if not caps:
        return False
    encoders = {str(e).lower() for e in caps.get("encoders") or []}
    filters = {str(f).lower() for f in caps.get("filters") or []}
    return "hevc_nvenc" in encoders and "scale_cuda" in filters


async def renew_lease(session: AsyncSession, task_id: int) -> None:
    """进度上报顺带续约。"""
    await session.execute(
        update(PreloadTask)
        .where(PreloadTask.id == task_id)
        .values(lease_expires_at=utcnow() + timedelta(seconds=LEASE_SECONDS))
    )


async def record_part_count(session: AsyncSession, task_id: int, *, part: int) -> int:
    """推进断点台账到 ``part+1``（段号合法性已由路由校验）。"""
    task = await session.get(PreloadTask, task_id)
    if task is None:
        raise NotFoundException("任务不存在")
    task.resume_parts = part + 1
    await renew_lease(session, task_id)
    await session.commit()
    return task.resume_parts


async def void_artifact(session: AsyncSession, task: PreloadTask) -> None:
    """整件作废（§7 扩展点 2：校验不过不做部分采纳）：分段、拼接中间物、已拼的产物文件
    一并删掉，断点台账清零——重领从头转。

    只清台账不删段是死路：重领时 Worker 会跳过全部已记录的段直接 complete，
    永远卡在「没有收到任何产物分段」。
    """
    directory = artifact_dir_for(task.id or 0)
    for part_path in directory.glob("seg*.mkv"):
        part_path.unlink(missing_ok=True)
    (directory / "concat.txt").unlink(missing_ok=True)
    (await artifact_path_for(session, task)).unlink(missing_ok=True)
    task.resume_parts = 0
    task.updated_at = utcnow()
    await session.commit()


async def finalize_artifact(
    session: AsyncSession,
    task: PreloadTask,
    *,
    probe: Callable[[str], MediaSpec | None],
) -> LibraryFile:
    """拼接分段 + 字幕内封 + 探测 + 登记为版本（REST 完成上报与 NAS 本地执行共用，§7）。

    拼接时字幕与章节从源文件一次拷入（:func:`final_mux_argv`）。任何一步校验失败都整件作废
    （:func:`void_artifact`，断点归零，重转从头来）；成功后分段即删。
    """
    directory = artifact_dir_for(task.id or 0)
    final_path = await artifact_path_for(session, task)
    if final_path.exists():
        final_path.unlink()  # 重跑的兜底
    parts = sorted(directory.glob("seg*.mkv"))
    try:
        if not parts:
            raise BadRequestException("没有收到任何产物分段")
        source = await session.get(LibraryFile, task.target_file_id)
        if source is None or not source.file_path:
            raise BadRequestException("源文件已不存在，产物作废")
        plan = await plan_for(session, source)
        listing = directory / "concat.txt"
        listing.write_text(
            "".join(f"file '{part.resolve().as_posix()}'\n" for part in parts), encoding="utf-8"
        )
        argv = final_mux_argv(
            concat_list=str(listing),
            source_path=source.file_path,
            output=str(final_path),
            subtitle_indexes=plan.subtitles,
        )
        try:
            # 拼接是 IO 密集的大活：放线程池，事件循环（WS 心跳、播放会话）照常跑
            muxed = await asyncio.to_thread(
                subprocess.run, argv, capture_output=True, text=True, timeout=3600
            )
        finally:
            listing.unlink(missing_ok=True)
        if muxed.returncode != 0:
            logger.warning("预转码拼接/字幕内封失败：task=%s %s", task.id, muxed.stderr[-500:])
            raise BadRequestException("产物分段拼接失败，整件作废")
        size = final_path.stat().st_size
        if size <= 0:
            raise BadRequestException("产物为空，整件作废")
        spec = await asyncio.to_thread(probe, str(final_path))
        derived = await complete_task(
            session,
            task,
            actual_bytes=size,
            duration_seconds=spec.duration_seconds if spec else None,
            video_codec=spec.video_codec if spec else None,
            spec=spec,
        )
    except BadRequestException:
        await void_artifact(session, task)
        raise
    for part in parts:
        part.unlink(missing_ok=True)
    return derived


async def complete_task(
    session: AsyncSession,
    task: PreloadTask,
    *,
    actual_bytes: int,
    duration_seconds: int | None,
    video_codec: str | None,
    spec: MediaSpec | None = None,
    container: str = "mkv",
) -> LibraryFile:
    """产物校验 + 登记为版本（§7 扩展点 2：整件采纳，不做部分采纳）。

    校验：文件在盘、大小与最后分段对齐、时长与源相近（±5%，浮点探针精度）。
    ``spec`` 是调用方 ffprobe 产物的结果（REST/本地两条完成路径都已探测），
    登记行的音轨/字幕轨从这里来——不填的话决策引擎按「无音轨」装配转码
    计划、字幕轨列表为空，播放就是没声音没字幕（NAS 实测惊天魔盗团产物
    即此坑）；没传时退回旧行为（流字段留空，由扫描补探兜底）。
    登记成 ``LibraryFile`` 派生行后，播放决策/字幕轨/详情页零改动地认它。
    """
    if task.status == PreloadTaskStatus.COMPLETED:
        file = await session.get(LibraryFile, task.result_file_id)
        if file is not None:
            return file
        raise BadRequestException("任务已完成但产物缺失，请删除后重转")
    if task.status not in (PreloadTaskStatus.CLAIMED, PreloadTaskStatus.RUNNING):
        # 取消/失效后迟到的完成上报（本地协程被取消的瞬间、REST 侧终态闸
        # 之后的极窄窗口）不能把任务翻回已完成
        raise BadRequestException(f"任务已不在执行态（{task.status}），完成上报被拒")
    directory = artifact_dir_for(task.id or 0)
    path = await artifact_path_for(session, task)
    if not path.is_file():
        raise BadRequestException("产物文件不存在，无法登记")
    size = path.stat().st_size
    size_drift = abs(size - actual_bytes)
    if actual_bytes and size_drift > 1024 and size_drift > actual_bytes * 0.01:
        raise BadRequestException("产物大小与上报不一致，可能上传不完整")
    if duration_seconds is None:
        raise BadRequestException("产物探测失败（无时长），整件作废")
    source = await session.get(LibraryFile, task.target_file_id)
    source_duration = source.duration_seconds if source else None
    if source_duration and source_duration > 0:
        drift = abs(duration_seconds - source_duration) / source_duration
        if drift > 0.05:
            raise BadRequestException(
                f"产物时长与源偏差 {drift:.0%}（>5%），整件作废"
            )
    if not command_acceptable(task, video_codec):
        raise BadRequestException(
            f"产物视频编码不是档位要求的 {_preset_of(task).get('video_codec', 'h264').upper()}"
        )

    # 产物没有音轨（源有音轨）= 转码链丢了音频，整件作废而不是登记出
    # 一部无声片
    if spec is not None and not spec.audio_streams and (source.audio_streams or []):
        raise BadRequestException("产物没有音轨（源有音轨），整件作废")

    # 登记为派生行：规格用产物自己的探测结果，来源快照标派生身份
    target = await session.get(LibraryFile, task.target_file_id)
    if target is None:
        raise BadRequestException("源文件已不存在，产物作废")
    # 重转场景：同任务目录下可能还挂着上一轮的派生行（file_path 唯一约束会炸登记）。
    # 产物可再生的语义下新产物直接顶掉旧版本行；按任务目录圈定范围——产物名是动态的，
    # 旧命名（portable.mkv）的历史行也在内。须校验全部通过后才动：校验失败是整件作废，
    # 不能先删了旧行、文件又回不来
    stale_rows = list(
        (
            await session.execute(
                select(LibraryFile).where(
                    LibraryFile.derives_from_file_id == task.target_file_id
                )
            )
        ).scalars()
    )
    for stale in stale_rows:
        if Path(stale.file_path).parent != directory:
            continue
        old_path = Path(stale.file_path)
        if old_path != path and old_path.is_file():
            old_path.unlink(missing_ok=True)  # 改名后的旧文件；同名的是本轮新鲜产物
        await session.delete(stale)
        await session.flush()
        logger.info("重转顶掉旧产物行：file=%s（task=%s）", stale.id, task.id)
    preset = _preset_of(task)
    derived = LibraryFile(
        library_id=target.library_id,
        media_item_id=target.media_item_id,
        season_number=target.season_number,
        episode_number=target.episode_number,
        file_path=str(path),
        size_bytes=size,
        container=container,
        resolution=spec.resolution if spec else "1080p",
        video_codec=(
            spec.video_codec if spec else (video_codec or preset.get("video_codec", "h264"))
        ),
        hdr=spec.hdr if spec else None,
        bit_depth=spec.bit_depth if spec else None,
        duration_seconds=duration_seconds,
        bit_rate=spec.bit_rate if spec else None,
        frame_rate=spec.frame_rate if spec else None,
        color_space=spec.color_space if spec else None,
        probe_version=PROBE_SCHEMA_VERSION if spec else None,
        audio_streams=list(spec.audio_streams) if spec else None,
        subtitle_streams=list(spec.subtitle_streams) if spec else None,
        chapters=list(spec.chapters) if spec else None,
        source="imported",
        origin={
            **_PRELOAD_ORIGIN,
            "detail": f"预转码 · {preset.get('short', task.preset_id)} · 任务 #{task.id}",
        },
        derives_from_file_id=task.target_file_id,
        derivation={
            "kind": "preload",
            "preset_id": task.preset_id,
            "task_id": task.id,
            "generated_at": utcnow().isoformat(),
        },
    )
    session.add(derived)
    await session.flush()
    task.status = PreloadTaskStatus.COMPLETED
    task.result_file_id = derived.id
    task.actual_bytes = size
    task.completed_at = utcnow()
    task.updated_at = utcnow()
    await session.commit()
    await session.refresh(derived)
    logger.info(
        "预转码产物已登记：task=%s → file=%s（%.1f GB，预估 %.1f GB）",
        task.id, derived.id, size / 1024**3, task.estimated_bytes / 1024**3,
    )
    return derived


def command_acceptable(task: PreloadTask, video_codec: str | None) -> bool:
    """产物编码符合档位（高清档= h264；直拷只在源是 H.264 时成立，所以两种
    档位的产物编码都由档位定义说了算）。"""
    expected = _preset_of(task).get("video_codec", "h264")
    return (video_codec or "").lower() == expected


async def fail_task(session: AsyncSession, task: PreloadTask, error: str) -> PreloadTask:
    """Worker 失败上报：记因、断点保留（重试时续传）、释放租约。

    状态闸：只有执行态（CLAIMED/RUNNING）能落失败。取消/回队后迟到的
    上报不能把任务翻成 FAILED——用户会看到「已取消」变「失败」的假象，
    还能对一个不存在的执行再点重试。
    """
    if task.status not in (PreloadTaskStatus.CLAIMED, PreloadTaskStatus.RUNNING):
        logger.info(
            "预转码任务失败上报被忽略（任务已 %s）：task=%s", task.status, task.id
        )
        return task
    task.status = PreloadTaskStatus.FAILED
    task.error = error[:2000]
    task.lease_expires_at = None
    task.updated_at = utcnow()
    await session.commit()
    await session.refresh(task)
    logger.warning("预转码任务失败：task=%s：%s", task.id, error)
    return task


# ---------------------------------------------------------------------------
# 冷数据清理（§6 三件套之二/三）
# ---------------------------------------------------------------------------


async def stale_preload_files(
    session: AsyncSession, *, days: int, library_ids: set[int] | None = None
) -> list[tuple[LibraryFile, int]]:
    """超期未播的预转码产物 [(行, 距上次播放天数)]，按最久未播排序。

    「最近一次播放」取 playback_log 里该派生行的最后一次 start（以
    media_item + 季集定位同一播放单元）；从未播过按产物生成时间算。
    ``library_ids`` 圈定范围（成员可见性）；None=全部库（管理员/统计）。
    """
    horizon = utcnow() - timedelta(days=days)
    stmt = select(LibraryFile).where(LibraryFile.derivation.is_not(None))  # type: ignore[union-attr]
    if library_ids is not None:
        stmt = stmt.where(LibraryFile.library_id.in_(library_ids))  # type: ignore[union-attr]
    rows = list((await session.execute(stmt)).scalars())
    from movieclaw_db.models import PlaybackLog

    # 最近一次播放一把查齐：按播放单元聚合，不逐行查（产物多时是 N+1）
    last_played: dict[tuple[Any, Any, Any], Any] = {}
    item_ids = {row.media_item_id for row in rows if row.media_item_id}
    if item_ids:
        grouped = await session.execute(
            select(
                PlaybackLog.media_item_id,
                PlaybackLog.season_number,
                PlaybackLog.episode_number,
                func.max(PlaybackLog.started_at),
            )
            .where(PlaybackLog.media_item_id.in_(item_ids))  # type: ignore[arg-type]
            .group_by(
                PlaybackLog.media_item_id,
                PlaybackLog.season_number,
                PlaybackLog.episode_number,
            )
        )
        last_played = {
            (int(media_item_id), season, episode): last
            for media_item_id, season, episode, last in grouped.all()
        }
    out: list[tuple[LibraryFile, int]] = []
    for row in rows:
        last = last_played.get(
            (row.media_item_id, row.season_number, row.episode_number)
        )
        anchor = last or row.created_at
        if anchor is None or anchor < horizon:
            days_since = (
                (utcnow() - anchor).days if anchor else 9999
            )
            out.append((row, days_since))
    out.sort(key=lambda pair: -pair[1])
    return out


async def cleanup_stale(
    session: AsyncSession,
    *,
    days: int,
    dry_run: bool = False,
    library_ids: set[int] | None = None,
) -> dict[str, Any]:
    """「清理冷数据」：预览（dry_run）或执行删除，返回将删列表与可释放总量。

    ``library_ids`` 圈定删除范围（成员可见性）——破坏性动作必须在查询处
    收口，路由层事后过滤返回值等于没防。
    """
    stale = await stale_preload_files(session, days=days, library_ids=library_ids)
    item_ids = {row.media_item_id for row, _ in stale if row.media_item_id}
    titles: dict[int, str] = {}
    if item_ids:
        titles = {
            item.id: item.title
            for item in (
                await session.execute(select(MediaItem).where(MediaItem.id.in_(item_ids)))
            ).scalars()
        }
    # 预览要让人认得出删的是哪一集：片名 + 季集号（电影为 0/0）+ 体积 + 未播天数
    preview = [
        {
            "file_id": row.id,
            "media_item_id": row.media_item_id,
            "title": titles.get(row.media_item_id),
            "season_number": row.season_number,
            "episode_number": row.episode_number,
            "size_bytes": row.size_bytes,
            "days_since_play": days_since,
            # 成员可见性过滤用（管理员不筛）
            "library_id": row.library_id,
        }
        for row, days_since in stale
    ]
    total = sum(row.size_bytes or 0 for row, _ in stale)
    if dry_run:
        return {"count": len(stale), "total_bytes": total, "files": preview}
    setting = await get_setting_store().get(PreloadSetting)
    freed = 0
    for row, _days in stale:
        path = Path(row.file_path)
        try:
            if path.is_file():
                path.unlink()
        except OSError:
            logger.warning("冷数据清理删文件失败（跳过）：%s", path, exc_info=True)
            continue
        freed += row.size_bytes or 0
        shutil.rmtree(path.parent, ignore_errors=True)
        await _invalidate_tasks_of(session, row.id, PRODUCT_STALE_REASON)
        await session.delete(row)
    await session.commit()
    logger.info(
        "预转码冷数据清理：删除 %d 个产物，释放 %.1f GB",
        len(stale), freed / 1024**3,
    )
    return {
        "count": len(stale),
        "total_bytes": total,
        "freed_bytes": freed,
        "files": preview,
        "stale_days": setting.stale_days,
    }


# ---------------------------------------------------------------------------
# 统计（设置「转码」页与缓存管理区块：共 X GB / 共 Y 部 / 最老的 Z 天前）
# ---------------------------------------------------------------------------


async def overview(
    session: AsyncSession, *, library_ids: set[int] | None = None
) -> dict[str, Any]:
    """预转码产物的总量统计。``oldest_days`` 以最近一次播放计（从未播按生成时间）。

    ``library_ids`` 圈定统计范围（成员可见性）；None=全部库（管理员）。
    """
    stmt = (
        select(func.count())
        .select_from(LibraryFile)
        .where(LibraryFile.derivation.is_not(None))  # type: ignore[union-attr]
    )
    if library_ids is not None:
        stmt = stmt.where(LibraryFile.library_id.in_(library_ids))  # type: ignore[union-attr]
    count = (await session.execute(stmt)).scalar_one()
    if library_ids is None:
        usage = await preload_usage_bytes(session)
    else:
        usage = int(
            (
                await session.execute(
                    select(func.coalesce(func.sum(LibraryFile.size_bytes), 0)).where(
                        LibraryFile.derivation.is_not(None),  # type: ignore[union-attr]
                        LibraryFile.library_id.in_(library_ids),  # type: ignore[union-attr]
                    )
                )
            ).scalar_one()
        )
    setting = await get_setting_store().get(PreloadSetting)
    stale = await stale_preload_files(session, days=1, library_ids=library_ids)
    oldest_days = max((days for _, days in stale), default=0)
    return {
        "count": int(count),
        "total_bytes": usage,
        "quota_bytes": setting.quota_bytes,
        "oldest_days": oldest_days,
        "stale_days": setting.stale_days,
        "auto_cleanup": setting.auto_cleanup,
    }


# ---------------------------------------------------------------------------
# 派生物失效（§6：源删除 / 洗版换源联动）
# ---------------------------------------------------------------------------


async def invalidate_for_source(session: AsyncSession, source_file_id: int) -> int:
    """源文件失效时清理它的预转码产物：删产物文件 + 删派生行 + 删任务目录。

    直接删不进回收站（§6：产物可再生，源都没了派生物没有存在意义）。
    返回清理的产物数。产物行 ondelete=CASCADE 会在源行删除时兜底删行，
    这里先把物理文件处理掉。
    """
    rows = list(
        (
            await session.execute(
                select(LibraryFile).where(
                    LibraryFile.derives_from_file_id == source_file_id  # type: ignore[arg-type]
                )
            )
        ).scalars()
    )
    # 关联任务标失效：产物没了但任务历史还在，详情页据此显示
    # 「已因换源失效，可重新转码」而不是笼统地回到"没下过单"（§6）
    tasks = list(
        (
            await session.execute(
                select(PreloadTask).where(
                    PreloadTask.target_file_id == source_file_id,  # type: ignore[arg-type]
                    PreloadTask.status == PreloadTaskStatus.COMPLETED,
                )
            )
        ).scalars()
    )
    for task in tasks:
        task.status = PreloadTaskStatus.INVALIDATED
        task.error = "源文件已被回收或换源，产物已自动清理；可重新下单转码"
    count = 0
    for row in rows:
        path = Path(row.file_path)
        if path.is_file():
            try:
                path.unlink()
            except OSError:
                logger.warning("预转码产物清理删文件失败：%s", path, exc_info=True)
        shutil.rmtree(path.parent, ignore_errors=True)
        await session.delete(row)
        count += 1
    if count:
        # 产物行先落删：调用方随后删源行时，库里的 CASCADE 会和产物的 DELETE 在同一次
        # flush 里打架，ORM 按行数核对就报 "expected to delete 2 row(s); 1 were matched"
        await session.flush()
        logger.info("源文件 #%s 失效，已清理 %d 个预转码产物", source_file_id, count)
    return count


async def invalidate_orphans(session: AsyncSession) -> int:
    """定时兜底：源行已消失（CASCADE 已删派生行）但目录残留的物理文件清理。"""
    root = preload_root()
    if not root.is_dir():
        return 0
    from movieclaw_db.models import PreloadTask as _PT

    live_task_ids = set(
        (
            await session.execute(select(_PT.id))
        ).scalars()
    )
    # 已登记产物行所在的目录（产物名动态，按目录归属判定而不是文件名）
    registered_dirs = {
        Path(row.file_path).parent
        for row in (
            await session.execute(
                select(LibraryFile).where(LibraryFile.derivation.is_not(None))  # type: ignore[union-attr]
            )
        ).scalars()
    }
    removed = 0
    for entry in root.iterdir():
        if not entry.name.isdigit() or int(entry.name) in live_task_ids:
            continue
        # 目录里还有已登记产物行的不清（产物行还在库上）
        if entry in registered_dirs:
            continue
        shutil.rmtree(entry, ignore_errors=True)
        removed += 1
    return removed


def register_task_safe():
    """把维护任务注册进调度器；注册失败（测试环境无调度器）不炸导入。"""
    def decorator(fn):
        try:
            from movieclaw_db.models.scheduled_task import TriggerType
            from movieclaw_scheduler.registry import register_task

            return register_task(
                "preload_maintenance",
                title="预转码维护",
                trigger_type=TriggerType.INTERVAL,
                interval_seconds=3600,
                description="回收超时租约、清理孤儿目录与超期产物（自动清理开启时）",
            )(fn)
        except Exception:  # pragma: no cover - 测试环境
            return fn
    return decorator


@register_task_safe()
async def preload_maintenance() -> None:
    """定时任务：租约回收 + 孤儿目录清理 + 自动冷数据清理（开启时）。"""
    db = get_database()
    async with db.session() as session:
        await reclaim_expired_leases(session)
        await invalidate_orphans(session)
        setting = await get_setting_store().get(PreloadSetting)
        if setting.auto_cleanup:
            result = await cleanup_stale(session, days=setting.stale_days)
            if result["count"]:
                freed = result.get("freed_bytes", 0)
                logger.info(
                    "预转码自动清理：%d 部共 %.1f GB",
                    result["count"], freed / 1024**3,
                )
                # 活动流记一笔（§6：自动化不该是盲盒）；publish 不抛错，
                # 事件丢了清理本身不受影响
                from movieclaw_api import events
                from movieclaw_sdk.events.library import PRELOAD_CLEANED

                await events.publish(
                    session,
                    PRELOAD_CLEANED,
                    lambda: {
                        "removed": result["count"],
                        "freed_bytes": freed,
                        "stale_days": setting.stale_days,
                    },
                )
                await session.commit()


__all__ = [
    "ARTIFACT_UPLOAD_NAME",
    "LEASE_SECONDS",
    "PreloadQuotaExceeded",
    "artifact_dir_for",
    "artifact_file_name",
    "artifact_path_for",
    "build_task_command",
    "cancel_task",
    "caps_accept_hevc",
    "check_quota",
    "claim_local_task",
    "claim_next_task",
    "cleanup_stale",
    "complete_task",
    "create_preload_task",
    "delete_derived_file",
    "fail_task",
    "finalize_artifact",
    "invalidate_for_source",
    "list_tasks",
    "plan_for",
    "preload_inflight_bytes",
    "preload_usage_bytes",
    "reclaim_expired_leases",
    "record_part_count",
    "renew_lease",
    "retry_task",
    "void_artifact",
]
