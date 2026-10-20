"""便携版（预转码派生行）的条目详情页：默认字幕与外挂列表借用源文件，与起播同一口径。

便携版没有自己的外挂字幕（台账为空），起播时借用源文件的（见 ``metadata_owner``）。详情页的文件区
要与之对齐：外挂列表列出源文件的外挂字幕，「默认」标记给的是起播真会放的那条。
"""

from __future__ import annotations

from functools import partial
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.auth import reset_auth_state
from movieclaw_api.services.playback.track_defaults import file_track_defaults
from movieclaw_api.settings.store import reset_setting_store
from movieclaw_db.crypto import reset_secret_box
from movieclaw_db.engine import get_database
from movieclaw_db.models import (
    FileSource,
    FileState,
    LibraryFile,
    MediaItem,
    MediaMetadata,
    PlaybackState,
    utcnow,
)
from movieclaw_db.repositories.library_repo import LibraryRepository
from movieclaw_playback.track_policy import TrackContext

_PB = "/api/v1/playback"
_ADMIN = {"username": "admin", "password": "Sup3rSecret!"}
#: 全解码客户端：决策直接给档 0（原文件直出，不起 ffmpeg），字幕清单照常给
_APP = {"universal": True}
_EXTERNAL_HOST = "8.8.8.8"

_SOURCE_NAME = "Show S01E01.mkv"
_PORTABLE_NAME = "Show S01E01 - 省流 720p.mkv"
_CHS = "Show S01E01.chs.srt"
_ENG = "Show S01E01.eng.srt"
#: 原声是英文，媒体库语言（默认 zh-CN）不同：默认字幕开库语言的那条外挂字幕
_AUDIO = [{"codec": "aac", "channels": 2, "language": "eng", "default": True}]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'portable-detail.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("TMDB_API_KEY", "test-key-not-used")
    get_settings.cache_clear()
    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    monkeypatch.setattr("movieclaw_api.api.routes.playback.available_backends", lambda: ())
    monkeypatch.setattr(
        "movieclaw_api.services.playback.plan.probe_keyframe_interval", lambda path, duration: 4.0
    )

    from movieclaw_api.app import create_app

    with TestClient(create_app()) as c:
        c.post("/api/v1/auth/bootstrap", json=_ADMIN)
        yield c

    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    get_settings.cache_clear()


def _external(monkeypatch) -> None:
    """把会话判成外网：便携版只在外网参与择优（真实链路里是反代转发来的 X-Forwarded-For）。"""
    monkeypatch.setattr(
        "movieclaw_api.api.routes.playback.client_address", lambda request: _EXTERNAL_HOST
    )


async def _seed(tmp_path: Path) -> dict:
    """源文件是英文原声、同目录挂两条外挂字幕；同集的便携版在另一目录，没有字幕。"""
    root = tmp_path / "tv"
    portable_dir = tmp_path / "portable"
    (root / "Season 01").mkdir(parents=True, exist_ok=True)
    portable_dir.mkdir(parents=True, exist_ok=True)
    source_path = root / "Season 01" / _SOURCE_NAME
    source_path.write_bytes(b"FAKE-SOURCE" * 64)
    cue = "1\n00:00:01,000 --> 00:00:02,000\n{}\n"
    (root / "Season 01" / _CHS).write_text(cue.format("简体字幕"), encoding="utf-8")
    (root / "Season 01" / _ENG).write_text(cue.format("English"), encoding="utf-8")
    portable_path = portable_dir / _PORTABLE_NAME
    portable_path.write_bytes(b"FAKE-PORTABLE" * 64)

    async with get_database().session() as session:
        library = await LibraryRepository(session).create(
            name="剧集", kind="tv", root_paths=[str(root)]
        )
        item = MediaItem(kind="tv", tmdb_id=77, title="示例剧", original_title="Show")
        session.add(item)
        await session.flush()
        # 有刮削时间的档案：详情页走库内档案，不触发 TMDB 兜底（测试里没有网络）
        session.add(
            MediaMetadata(media_item_id=item.id, original_language="en", scraped_at=utcnow())
        )
        source = LibraryFile(
            library_id=library.id,
            media_item_id=item.id,
            season_number=1,
            episode_number=1,
            file_path=str(source_path),
            size_bytes=source_path.stat().st_size,
            source=FileSource.SCANNED,
            state=FileState.IN_PLACE,
            container="mkv",
            video_codec="hevc",
            resolution="2160p",
            duration_seconds=2700,
            audio_streams=_AUDIO,
            subtitle_streams=[],
            external_subtitles=[_entry(_CHS, "chi"), _entry(_ENG, "eng")],
        )
        session.add(source)
        await session.commit()
        portable = LibraryFile(
            library_id=library.id,
            media_item_id=item.id,
            season_number=1,
            episode_number=1,
            file_path=str(portable_path),
            size_bytes=portable_path.stat().st_size,
            source=FileSource.IMPORTED,
            state=FileState.IN_PLACE,
            container="mkv",
            video_codec="h264",
            resolution="720p",
            duration_seconds=2700,
            audio_streams=_AUDIO,
            subtitle_streams=[],
            derives_from_file_id=source.id,
            derivation={"kind": "preload", "preset_id": "portable_720p", "task_id": 1},
        )
        session.add(portable)
        await session.commit()
        return {
            "library": library.id,
            "item": item.id,
            "source": source.id,
            "portable": portable.id,
        }


async def _set_state(file_id: int, state: FileState) -> None:
    async with get_database().session() as session:
        row = await session.get(LibraryFile, file_id)
        assert row is not None
        row.state = state
        await session.commit()


def _entry(filename: str, language: str) -> dict:
    return {
        "filename": filename,
        "format": "srt",
        "language": language,
        "title": None,
        "default": False,
        "forced": False,
        "sdh": False,
        "size_bytes": 1,
        "file_mtime_ns": 1,
    }


def call(client: TestClient, fn, *args, **kwargs):
    return client.portal.call(partial(fn, *args, **kwargs))  # type: ignore[attr-defined]


def detail_files(client: TestClient, ids: dict) -> dict[int, dict]:
    resp = client.get(f"/api/v1/libraries/{ids['library']}/items/{ids['item']}")
    assert resp.status_code == 200, resp.text
    return {view["id"]: view for view in resp.json()["data"]["files"]}


def external_views(file: dict) -> list[dict]:
    return [stream for stream in file["subtitle_streams"] if stream["external"]]


def session_for(client: TestClient, ids: dict) -> dict:
    body = {
        "media_item_id": ids["item"],
        "season_number": 1,
        "episode_number": 1,
        "capability": _APP,
    }
    resp = client.post(f"{_PB}/sessions", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def playback_external_refs(data: dict) -> list[str]:
    return [
        sub["track_ref"]
        for sub in data["decision"]["subtitles"]
        if sub["track_ref"].startswith("external:")
    ]


def playback_default(data: dict) -> str | None:
    return next(
        (sub["track_ref"] for sub in data["decision"]["subtitles"] if sub["is_default"]), None
    )


def test_portable_detail_lists_the_source_subtitles_and_marks_what_playback_plays(
    client, tmp_path, monkeypatch
):
    ids = call(client, _seed, tmp_path)
    files = detail_files(client, ids)
    portable, source = files[ids["portable"]], files[ids["source"]]

    # 便携版没有自己的外挂字幕：列表与源文件自己的一样（语言、标题都按源文件的文件名解析）
    # 唯一的差别是 borrowed：借来的为真
    assert [{**s, "borrowed": False} for s in external_views(portable)] == external_views(source)
    assert [stream["file_name"] for stream in external_views(portable)] == [_CHS, _ENG]
    # 「默认」：原声英文、媒体库语言中文，开的是库语言的外挂字幕
    assert portable["playback_defaults"]["subtitle_track"] == f"external:{_CHS}"

    _external(monkeypatch)
    data = session_for(client, ids)

    assert data["decision"]["file_id"] == ids["portable"]
    assert sorted(playback_external_refs(data)) == [f"external:{_CHS}", f"external:{_ENG}"]
    assert playback_default(data) == portable["playback_defaults"]["subtitle_track"]


def test_borrowed_external_subtitles_are_flagged_and_the_source_keeps_its_own(client, tmp_path):
    ids = call(client, _seed, tmp_path)
    files = detail_files(client, ids)

    # 借来的外挂字幕归属源文件：标 borrowed，详情页据此不给删除入口（删除只认本行的文件名）
    assert [s["borrowed"] for s in external_views(files[ids["portable"]])] == [True, True]
    assert [s["borrowed"] for s in external_views(files[ids["source"]])] == [False, False]


def test_portable_detail_borrows_nothing_while_its_source_is_not_in_place(client, tmp_path):
    ids = call(client, _seed, tmp_path)
    call(client, _set_state, ids["source"], FileState.MISSING)

    portable = detail_files(client, ids)[ids["portable"]]

    assert external_views(portable) == []
    assert portable["playback_defaults"]["subtitle_track"] is None


def test_borrowed_subtitle_previews_from_the_source_directory(client, tmp_path):
    ids = call(client, _seed, tmp_path)

    resp = client.get(
        f"/api/v1/libraries/files/{ids['portable']}/subtitles/preview",
        params={"track": f"external:{_CHS}"},
    )

    assert resp.status_code == 200, resp.text
    assert any("简体字幕" in cue["text"] for cue in resp.json()["data"]["cues"])


def test_inherited_subtitle_on_a_portable_copy_reads_its_source_external_tracks():
    """同剧上一集换过的外挂字幕换到本集便携版：候选看本集源文件的外挂字幕，不看便携版自己的台账。"""
    prev = LibraryFile(
        id=1,
        media_item_id=9,
        season_number=1,
        episode_number=1,
        file_path="/tv/S01E01.mkv",
        audio_streams=_AUDIO,
        subtitle_streams=[],
        external_subtitles=[_entry("S01E01.chs.srt", "chi"), _entry("S01E01.eng.srt", "eng")],
    )
    source = LibraryFile(
        id=2,
        media_item_id=9,
        season_number=1,
        episode_number=2,
        file_path="/tv/S01E02.mkv",
        audio_streams=_AUDIO,
        subtitle_streams=[],
        external_subtitles=[_entry("S01E02.chs.srt", "chi"), _entry("S01E02.eng.srt", "eng")],
    )
    portable = LibraryFile(
        id=3,
        media_item_id=9,
        season_number=1,
        episode_number=2,
        file_path="/portable/S01E02 - 720p.mkv",
        audio_streams=_AUDIO,
        subtitle_streams=[],
        external_subtitles=None,
        derives_from_file_id=2,
    )
    states = {
        (9, 1, 1): PlaybackState(
            member_id=1,
            media_item_id=9,
            season_number=1,
            episode_number=1,
            subtitle_track="external:S01E01.eng.srt",
        )
    }

    defaults = file_track_defaults(
        portable,
        TrackContext.build("zh-CN", "en"),
        states,
        {(9, 1, 1): prev},
        owners={3: source},
    )

    assert defaults is not None
    assert defaults.subtitle_ref == "external:S01E02.eng.srt"
    assert defaults.subtitle.reason == "series"
