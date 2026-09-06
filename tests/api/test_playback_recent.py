"""媒体库首页最近观看：成员隔离、剧集聚合与可见库过滤。"""

from __future__ import annotations

from datetime import datetime

import pytest_asyncio

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.playback_recent import recent_watch_items
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    FileSource,
    FileState,
    LibraryFile,
    MediaEpisode,
    MediaItem,
    MediaMetadata,
    PlaybackState,
)
from movieclaw_db.repositories.library_repo import LibraryRepository
from movieclaw_playback.state import get_states


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'recent.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


def _file(
    library_id: int,
    item_id: int,
    season: int,
    episode: int,
    *,
    duration_seconds: int = 2400,
    missing: bool = False,
    added_at: datetime = datetime(2026, 8, 1),
    variant: str = "",
) -> LibraryFile:
    return LibraryFile(
        library_id=library_id,
        media_item_id=item_id,
        season_number=season,
        episode_number=episode,
        file_path=f"/{library_id}/{item_id}/S{season:02d}E{episode:02d}{variant}.mkv",
        size_bytes=1,
        source=FileSource.SCANNED,
        duration_seconds=duration_seconds,
        missing_since=datetime(2026, 8, 1) if missing else None,
        state=FileState.MISSING if missing else FileState.IN_PLACE,
        created_at=added_at,
        updated_at=added_at,
    )


async def test_recent_watch_is_member_scoped_and_keeps_latest_episode(db) -> None:
    """同一剧只出一张卡并定位最新一集；他人状态、不可见库和缺失文件均不混入。"""
    async with db.session() as session:
        repo = LibraryRepository(session)
        tv_library = await repo.create(name="剧集库", kind="tv", root_paths=["/tv"])
        movie_library = await repo.create(name="电影库", kind="movie", root_paths=["/movies"])
        private_library = await repo.create(name="私藏库", kind="movie", root_paths=["/private"])

        show = MediaItem(
            kind="tv",
            tmdb_id=101,
            title="示例剧",
            original_title="Example Show",
            poster_path="/show.jpg",
        )
        movie = MediaItem(
            kind="movie",
            tmdb_id=102,
            title="示例电影",
            original_title="Example Movie",
            backdrop_path="/movie-backdrop.jpg",
        )
        private_movie = MediaItem(
            kind="movie", tmdb_id=103, title="私藏电影", original_title="Private"
        )
        missing_movie = MediaItem(
            kind="movie", tmdb_id=104, title="已丢失电影", original_title="Missing"
        )
        session.add_all([show, movie, private_movie, missing_movie])
        await session.flush()
        assert tv_library.id and movie_library.id and private_library.id
        assert show.id and movie.id and private_movie.id and missing_movie.id

        session.add_all(
            [
                _file(tv_library.id, show.id, 1, 1),
                _file(tv_library.id, show.id, 1, 2),
                # S01E03 尚未观看；同一集的多个版本只能算一集。
                _file(
                    tv_library.id,
                    show.id,
                    1,
                    3,
                    added_at=datetime(2026, 8, 15, 21, 0),
                ),
                _file(
                    tv_library.id,
                    show.id,
                    1,
                    3,
                    added_at=datetime(2026, 8, 15, 22, 0),
                    variant="-2160p",
                ),
                # 文件已经全部缺失的集不计入未看提醒。
                _file(
                    tv_library.id,
                    show.id,
                    1,
                    4,
                    missing=True,
                    added_at=datetime(2026, 8, 15, 23, 0),
                ),
                _file(movie_library.id, movie.id, 0, 0, duration_seconds=7200),
                _file(private_library.id, private_movie.id, 0, 0),
                _file(movie_library.id, missing_movie.id, 0, 0, missing=True),
                MediaEpisode(
                    media_item_id=show.id,
                    season_number=1,
                    episode_number=2,
                    name="第二集",
                    runtime_minutes=42,
                    still_path="/episode-2.jpg",
                ),
                # 本地封面 16:9：最近观看卡缺横向剧照时靠 poster_aspect 决定铺满还是模糊铺底
                MediaMetadata(
                    media_item_id=movie.id,
                    runtime_minutes=130,
                    poster_width=1280,
                    poster_height=720,
                ),
                # 当前成员连看两集：首页只保留更新的 S01E02。
                PlaybackState(
                    member_id=7,
                    media_item_id=show.id,
                    season_number=1,
                    episode_number=1,
                    position_ms=600_000,
                    play_count=1,
                    last_played_at=datetime(2026, 8, 14, 20, 0),
                ),
                PlaybackState(
                    member_id=7,
                    media_item_id=show.id,
                    season_number=1,
                    episode_number=2,
                    position_ms=1_200_000,
                    play_count=1,
                    last_played_at=datetime(2026, 8, 15, 20, 0),
                ),
                PlaybackState(
                    member_id=7,
                    media_item_id=movie.id,
                    season_number=0,
                    episode_number=0,
                    played=True,
                    play_count=1,
                    last_played_at=datetime(2026, 8, 15, 19, 0),
                ),
                PlaybackState(
                    member_id=7,
                    media_item_id=private_movie.id,
                    season_number=0,
                    episode_number=0,
                    play_count=1,
                    last_played_at=datetime(2026, 8, 15, 21, 0),
                ),
                PlaybackState(
                    member_id=7,
                    media_item_id=missing_movie.id,
                    season_number=0,
                    episode_number=0,
                    play_count=1,
                    last_played_at=datetime(2026, 8, 15, 22, 0),
                ),
                # 另一成员即使更新，也不能影响成员 7 的排序和进度。
                PlaybackState(
                    member_id=8,
                    media_item_id=movie.id,
                    season_number=0,
                    episode_number=0,
                    position_ms=3_600_000,
                    play_count=3,
                    last_played_at=datetime(2026, 8, 15, 23, 0),
                ),
            ]
        )
        await session.commit()

        rows = await recent_watch_items(
            session,
            member_id=7,
            visible_library_ids={tv_library.id, movie_library.id},
            limit=20,
        )

        assert [row.media_item_id for row in rows] == [show.id, movie.id]
        episode = rows[0]
        assert (episode.season_number, episode.episode_number) == (1, 2)
        assert episode.episode_title == "第二集"
        assert episode.duration_ms == 2_400_000
        assert episode.progress_percent == 50
        assert episode.unwatched_ahead_count == 1
        assert episode.poster_url == "https://image.tmdb.org/t/p/w500/show.jpg"
        assert episode.poster_aspect == 0.6667  # TMDB 海报无尺寸记录，按 2:3 惯例
        assert episode.episode_still_url == ("https://image.tmdb.org/t/p/w500/episode-2.jpg")

        completed = rows[1]
        assert completed.played is True
        assert completed.duration_ms == 7_200_000
        assert completed.progress_percent is None
        assert completed.unwatched_ahead_count == 0
        assert completed.backdrop_url == ("https://image.tmdb.org/t/p/w780/movie-backdrop.jpg")
        assert completed.poster_aspect == 1.7778
        assert completed.episode_still_url is None

        # 一旦开始播放 S01E03，锚点推进且该集算已看，未看提醒随之清零。
        session.add(
            PlaybackState(
                member_id=7,
                media_item_id=show.id,
                season_number=1,
                episode_number=3,
                position_ms=60_000,
                play_count=1,
                last_played_at=datetime(2026, 8, 16, 0, 0),
            )
        )
        await session.commit()
        refreshed = await recent_watch_items(
            session,
            member_id=7,
            visible_library_ids={tv_library.id, movie_library.id},
            limit=20,
        )
        assert (refreshed[0].season_number, refreshed[0].episode_number) == (1, 3)
        assert refreshed[0].unwatched_ahead_count == 0


async def test_unwatched_ahead_ignores_watched_episodes_and_refreshed_added_time(db) -> None:
    """未看提醒只数“锚点之后 + 从未看过”的在位集：洗版刷新入库时间不误报，全看完归零。"""
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=["/tv"]
        )
        show = MediaItem(kind="tv", tmdb_id=301, title="完结剧", original_title="Finished")
        session.add(show)
        await session.flush()
        assert library.id and show.id

        # 全部分集的入库时间都晚于播放时间：模拟洗版把旧版本台账行删掉后
        # 重新落行（旧口径会把整季都误报成“新入库”）。
        rewritten = datetime(2026, 8, 20, 3, 0)
        session.add_all(
            [
                _file(library.id, show.id, 1, episode, added_at=rewritten)
                for episode in (1, 2, 3, 4, 5)
            ]
        )
        session.add_all(
            [
                # 锚点是 S01E02：E01 更早看过，E04 是跳着看过的更后面的集。
                PlaybackState(
                    member_id=5,
                    media_item_id=show.id,
                    season_number=1,
                    episode_number=1,
                    played=True,
                    play_count=1,
                    last_played_at=datetime(2026, 8, 10),
                ),
                PlaybackState(
                    member_id=5,
                    media_item_id=show.id,
                    season_number=1,
                    episode_number=2,
                    played=True,
                    play_count=1,
                    last_played_at=datetime(2026, 8, 12),
                ),
                PlaybackState(
                    member_id=5,
                    media_item_id=show.id,
                    season_number=1,
                    episode_number=4,
                    played=True,
                    play_count=1,
                    last_played_at=datetime(2026, 8, 11),
                ),
                # 只收藏、从未播放的集仍然算“未看”。
                PlaybackState(
                    member_id=5,
                    media_item_id=show.id,
                    season_number=1,
                    episode_number=5,
                    is_favorite=True,
                ),
                # 别人看过不影响本成员的未看数。
                PlaybackState(
                    member_id=6,
                    media_item_id=show.id,
                    season_number=1,
                    episode_number=3,
                    played=True,
                    play_count=1,
                    last_played_at=datetime(2026, 8, 13),
                ),
            ]
        )
        await session.commit()

        rows = await recent_watch_items(session, member_id=5, visible_library_ids=None, limit=20)
        assert (rows[0].season_number, rows[0].episode_number) == (1, 2)
        # 锚点之后只剩 E03、E05 没看；E01/E02 在锚点之前，E04 已经看过。
        assert rows[0].unwatched_ahead_count == 2

        # 把剩下两集也看完：整部剧看完后角标彻底消失，不再被入库时间惊动。
        # E03 建新行，E05 沿用那条收藏行标记已看（唯一键上每人每单元只有一行）。
        session.add(
            PlaybackState(
                member_id=5,
                media_item_id=show.id,
                season_number=1,
                episode_number=3,
                played=True,
                play_count=1,
                last_played_at=datetime(2026, 8, 11, 12),
            )
        )
        favorite = (await get_states(session, [show.id], member_id=5))[(show.id, 1, 5)]
        favorite.played = True
        favorite.play_count = 1
        favorite.last_played_at = datetime(2026, 8, 11, 13)
        await session.commit()

        finished = await recent_watch_items(
            session, member_id=5, visible_library_ids=None, limit=20
        )
        assert (finished[0].season_number, finished[0].episode_number) == (1, 2)
        assert finished[0].unwatched_ahead_count == 0


async def test_recent_watch_honors_limit_and_empty_visibility(db) -> None:
    """limit 在作品聚合后生效；空白名单不读取任何播放记录。"""
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=["/movies"]
        )
        first = MediaItem(kind="movie", tmdb_id=201, title="新", original_title="New")
        second = MediaItem(kind="movie", tmdb_id=202, title="旧", original_title="Old")
        session.add_all([first, second])
        await session.flush()
        assert library.id and first.id and second.id
        session.add_all(
            [
                _file(library.id, first.id, 0, 0),
                _file(library.id, second.id, 0, 0),
                PlaybackState(
                    member_id=0,
                    media_item_id=first.id,
                    play_count=1,
                    last_played_at=datetime(2026, 8, 15, 12, 0),
                ),
                PlaybackState(
                    member_id=0,
                    media_item_id=second.id,
                    play_count=1,
                    last_played_at=datetime(2026, 8, 14, 12, 0),
                ),
            ]
        )
        await session.commit()

        limited = await recent_watch_items(session, member_id=0, visible_library_ids=None, limit=1)
        assert [row.media_item_id for row in limited] == [first.id]
        assert (
            await recent_watch_items(session, member_id=0, visible_library_ids=set(), limit=20)
            == []
        )
