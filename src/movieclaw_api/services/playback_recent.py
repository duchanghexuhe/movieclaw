"""媒体库首页“最近观看”的业务查询。

Jellyfin 只负责把播放事实写进 ``playback_state``；首页直接读取领域表，
不反向调用 Jellyfin 协议接口。查询同时收紧到当前账号可见、文件仍在位的
媒体库，避免权限变更或文件丢失后继续泄露历史条目。
"""

from __future__ import annotations

from sqlalchemy import and_, distinct, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlmodel import select

from movieclaw_api.core.config import get_settings
from movieclaw_api.schemas.playback import RecentWatchItemView
from movieclaw_api.services.library.thumbs import primary_aspect
from movieclaw_api.services.media_scrape import asset_version
from movieclaw_db.models import (
    FileState,
    Library,
    LibraryFile,
    MediaEpisode,
    MediaItem,
    MediaMetadata,
    PlaybackState,
)
from movieclaw_media.models import MediaKind


def _runtime_ms(
    file_duration_seconds: int | None,
    episode_runtime_minutes: int | None,
    item_runtime_minutes: int | None,
) -> int | None:
    """时长优先取真实文件，其次分集档案，最后退回条目常规片长。"""
    if file_duration_seconds and file_duration_seconds > 0:
        return file_duration_seconds * 1000
    runtime_minutes = episode_runtime_minutes or item_runtime_minutes
    return runtime_minutes * 60_000 if runtime_minutes and runtime_minutes > 0 else None


def _progress_percent(position_ms: int, duration_ms: int | None) -> int | None:
    """生成卡片进度；保留 1%~99%，完成态由 ``played`` 单独表达。"""
    if position_ms <= 0 or not duration_ms:
        return None
    return max(1, min(99, round(position_ms * 100 / duration_ms)))


async def recent_watch_items(
    session: AsyncSession,
    *,
    member_id: int,
    visible_library_ids: set[int] | None,
    limit: int,
) -> list[RecentWatchItemView]:
    """返回一个账号最近观看的作品，同一剧只保留最后活动的那一集。

    ``playback_state`` 不记录从哪个库/版本播放；同一作品跨库存在时按媒体库
    首页的展示顺序选择第一个可见库，保证卡片有稳定、可访问的详情落点。
    """
    if visible_library_ids == set():
        return []

    # 先在数据库内按作品选出最近状态，避免连看一整季时把同一部剧重复铺满。
    ranked_states = (
        select(
            PlaybackState.id.label("state_id"),
            func.row_number()
            .over(
                partition_by=PlaybackState.media_item_id,
                order_by=(PlaybackState.last_played_at.desc(), PlaybackState.id.desc()),
            )
            .label("item_rank"),
        )
        .where(
            PlaybackState.member_id == member_id,
            PlaybackState.last_played_at.is_not(None),  # type: ignore[union-attr]
            PlaybackState.season_number >= 0,
            PlaybackState.episode_number >= 0,
        )
        .subquery()
    )

    # 角标口径是“还有多少集可以接着看”，而不是“入库时间比我最近播放新”。
    # 纯时间口径有两处硬伤，都会给用户发无意义的提醒：
    #   1. 不看是否已经看过——整部剧看完之后，补齐旧季、重扫或任何一次入库
    #      都会重新催一遍，而用户根本没有可看的新内容；
    #   2. 入库时间本身不可靠——洗版成功会把旧版本的台账行物理删除
    #      （services/subscription/upgrade.py），该集的入库时间被刷新成洗版
    #      那一刻，早就看过的老集会被误报成“新入库”。
    # 因此改为统计：本库仍有在位文件、季集排在卡片锚点之后、且当前成员从未
    # 看过的分集。同一集的多个版本（1080p / 2160p）用 count(distinct 季:集)
    # 归一，只算一集——不先 GROUP BY 出一张全库派生表再筛，是因为那样
    # SQLite 每次求值都要为整张台账建临时索引；直接打
    # ix_library_file_browse_unit 只扫这一部作品的库存行，实测快一个数量级。
    # 两个别名都是必须的：外层已经 join 了 library_file 与 playback_state，
    # 不取别名 SQLAlchemy 会把子查询里的同名表与外层那两张自动关联成一张。
    unit_file = aliased(LibraryFile)
    watched_state = aliased(PlaybackState)
    # 判定“看过”只认播放事实：播放过（last_played_at 非空）或被显式标记已看。
    # 只收藏、只记忆过轨选择而从未播放的状态行不算看过。
    watched_unit = (
        select(watched_state.id)
        .where(
            watched_state.member_id == member_id,
            watched_state.media_item_id == unit_file.media_item_id,
            watched_state.season_number == unit_file.season_number,
            watched_state.episode_number == unit_file.episode_number,
            or_(
                watched_state.played.is_(True),  # type: ignore[union-attr]
                watched_state.last_played_at.is_not(None),  # type: ignore[union-attr]
            ),
        )
        .exists()
    )
    unwatched_ahead_count = (
        select(
            func.count(
                distinct(unit_file.season_number.op("||")(":").op("||")(unit_file.episode_number))
            )
        )
        .select_from(unit_file)
        .where(
            unit_file.library_id == Library.id,
            unit_file.media_item_id == PlaybackState.media_item_id,
            unit_file.state == FileState.IN_PLACE,  # 在位口径：缺失/待回收都不算可看
            # 季集按字典序严格大于卡片锚点：补齐的旧季、洗版的老集都排在锚点
            # 之前，不会被当成“可以接着看”的内容。
            or_(
                unit_file.season_number > PlaybackState.season_number,
                and_(
                    unit_file.season_number == PlaybackState.season_number,
                    unit_file.episode_number > PlaybackState.episode_number,
                ),
            ),
            ~watched_unit,
        )
        .correlate(Library, PlaybackState)
        .scalar_subquery()
        .label("unwatched_ahead_count")
    )

    statement = (
        select(
            PlaybackState,
            MediaItem,
            Library.id,
            MediaMetadata.poster_file,
            MediaMetadata.poster_width,
            MediaMetadata.poster_height,
            MediaMetadata.backdrop_file,
            MediaMetadata.runtime_minutes,
            MediaEpisode.name,
            MediaEpisode.runtime_minutes,
            MediaEpisode.still_file,
            MediaEpisode.still_path,
            func.max(LibraryFile.duration_seconds).label("file_duration_seconds"),
            unwatched_ahead_count,
        )
        .join(ranked_states, ranked_states.c.state_id == PlaybackState.id)
        .join(MediaItem, MediaItem.id == PlaybackState.media_item_id)
        .join(
            LibraryFile,
            and_(
                LibraryFile.media_item_id == PlaybackState.media_item_id,
                LibraryFile.season_number == PlaybackState.season_number,
                LibraryFile.episode_number == PlaybackState.episode_number,
                LibraryFile.in_place(),
            ),
        )
        .join(Library, Library.id == LibraryFile.library_id)
        .outerjoin(MediaMetadata, MediaMetadata.media_item_id == MediaItem.id)
        .outerjoin(
            MediaEpisode,
            and_(
                MediaEpisode.media_item_id == PlaybackState.media_item_id,
                MediaEpisode.season_number == PlaybackState.season_number,
                MediaEpisode.episode_number == PlaybackState.episode_number,
            ),
        )
        .where(ranked_states.c.item_rank == 1)
        .group_by(
            PlaybackState.id,
            MediaItem.id,
            Library.id,
            MediaMetadata.id,
            MediaEpisode.id,
        )
        .order_by(
            PlaybackState.last_played_at.desc(),
            MediaItem.id.asc(),
            Library.sort_order.asc(),
            Library.id.asc(),
        )
    )
    if visible_library_ids is not None:
        statement = statement.where(Library.id.in_(visible_library_ids))  # type: ignore[attr-defined]

    rows = (await session.execute(statement)).all()
    image_base = get_settings().tmdb_image_base_url.rstrip("/")
    result: list[RecentWatchItemView] = []
    seen_items: set[int] = set()
    for (
        state,
        item,
        library_id,
        poster_file,
        poster_width,
        poster_height,
        backdrop_file,
        item_runtime_minutes,
        episode_name,
        episode_runtime_minutes,
        episode_still_file,
        episode_still_path,
        file_duration_seconds,
        unwatched_ahead,
    ) in rows:
        if item.id is None or item.id in seen_items or state.last_played_at is None:
            continue
        seen_items.add(item.id)
        if poster_file:
            poster_url = f"/images/assets/{poster_file}?v={asset_version(poster_file)}"
        else:
            poster_url = f"{image_base}/w500{item.poster_path}" if item.poster_path else None
        if backdrop_file:
            backdrop_url = f"/images/assets/{backdrop_file}?v={asset_version(backdrop_file)}"
        else:
            backdrop_url = f"{image_base}/w780{item.backdrop_path}" if item.backdrop_path else None
        episode_still_url = None
        if item.kind == "tv":
            if episode_still_file:
                episode_still_url = (
                    f"/images/assets/{episode_still_file}?v={asset_version(episode_still_file)}"
                )
            elif episode_still_path:
                episode_still_url = f"{image_base}/w500{episode_still_path}"
        duration_ms = _runtime_ms(
            file_duration_seconds,
            episode_runtime_minutes if item.kind == "tv" else None,
            item_runtime_minutes,
        )
        result.append(
            RecentWatchItemView(
                media_item_id=item.id,
                library_id=library_id,
                kind=MediaKind(item.kind),
                title=item.title,
                year=item.year,
                poster_url=poster_url,
                poster_aspect=primary_aspect(item, poster_width, poster_height),
                backdrop_url=backdrop_url,
                episode_still_url=episode_still_url,
                season_number=state.season_number,
                episode_number=state.episode_number,
                episode_title=episode_name or None,
                unwatched_ahead_count=int(unwatched_ahead or 0) if item.kind == "tv" else 0,
                position_ms=state.position_ms,
                duration_ms=duration_ms,
                progress_percent=_progress_percent(state.position_ms, duration_ms),
                played=state.played,
                play_count=state.play_count,
                last_played_at=state.last_played_at,
            )
        )
        if len(result) >= limit:
            break
    return result
