"""订阅上限：一季上千集时，只订阅前 N 集（docs/design/subscription-episode-cap.md）。

TMDB 里一季可以有上千集（名侦探柯南 1186 集、国产漫剧动辄 1000+）。整季展开成
上千个工单，既占磁盘与入库后处理，也未必看得完。上限是用户确认的「订阅到第 N 集」，
**期望集合 E 定义的一部分**：E 里这一季超过上限的集（含追新与占位集）都不在其中，
所以

- 被截断的季不会判「已收齐」（见 ``recompute_subscription_status``），订阅留在
  订阅中，等用户决定要不要继续；
- 「继续订阅」就是把上限改大一次，``SubscriptionService.update`` 的 diff 负责补工单；
  清除上限（「全部」）就回到今天的整季行为。

与 ``episode_floor`` 方向相反：下限纠正「这季其实有几集」（事实），上限是「我先订阅
到第几集」（节奏）。只存确实截断了本季的上限——不低于本季总集数的值视为不设。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.services.subscription.episode_floor import (
    FLOOR_MAX,
    floors_of,
    tracks_season,
)
from movieclaw_db.models import MediaEpisode, Subscription

# 单季上限的取值范围同集数下限：防手滑，长寿剧一千多集也够用
CAP_MAX = FLOOR_MAX


def _positive_int(value: object) -> int | None:
    try:
        number = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


@dataclass(frozen=True)
class EpisodeWindow:
    """被订阅上限截断的一季：订阅到第 ``cap`` 集，本季共 ``total`` 集。"""

    season_number: int
    cap: int
    total: int


def caps_of(subscription: Subscription) -> dict[int, int]:
    """订阅的集数上限 {季号: 集号}（JSON 键是字符串，这里统一转回整数）。"""
    caps: dict[int, int] = {}
    for season, cap in (subscription.episode_caps or {}).items():
        season_number, number = _positive_int(season), _positive_int(cap)
        if season_number is not None and number is not None:
            caps[season_number] = number
    return caps


def known_max_of(episodes: list[MediaEpisode]) -> dict[int, int]:
    """每季已录的最大集号 {季号: 集号}。"""
    known_max: dict[int, int] = {}
    for episode in episodes:
        known_max[episode.season_number] = max(
            known_max.get(episode.season_number, 0), episode.episode_number
        )
    return known_max


def windows_of(subscription: Subscription, known_max: dict[int, int]) -> list[EpisodeWindow]:
    """订阅范围内被上限截断的季。

    总集数取 TMDB 已录的最大集号与集数下限中大的那个（下限撑起的占位集也算本季的集）。
    上限不低于总集数的季没有被截断，不在其中。
    """
    if not subscription.episode_caps:
        return []
    floors = floors_of(subscription)
    windows: list[EpisodeWindow] = []
    for season_number, cap in sorted(caps_of(subscription).items()):
        if not tracks_season(subscription, season_number):
            continue
        total = max(known_max.get(season_number, 0), floors.get(season_number, 0))
        if cap < total:
            windows.append(EpisodeWindow(season_number, cap, total))
    return windows


async def season_episode_max_many(
    session: AsyncSession, media_item_ids: list[int]
) -> dict[int, dict[int, int]]:
    """批量取每个条目、每季的最大集号 {条目: {季号: 集号}}——订阅列表一次分组查完。"""
    if not media_item_ids:
        return {}
    rows = await session.execute(
        select(
            MediaEpisode.media_item_id,
            MediaEpisode.season_number,
            func.max(MediaEpisode.episode_number),
        )
        .where(MediaEpisode.media_item_id.in_(media_item_ids))  # type: ignore[attr-defined]
        .group_by(MediaEpisode.media_item_id, MediaEpisode.season_number)
    )
    result: dict[int, dict[int, int]] = {}
    for item_id, season, number in rows.all():
        if number:
            result.setdefault(item_id, {})[season] = number
    return result
