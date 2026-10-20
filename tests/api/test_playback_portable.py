"""便携版的播放端到端（docs/design/preload-transcode.md）：外网默认放便携版，字幕与片头引用源文件。

便携版是源文件的派生行（``derivation`` 非空、``derives_from_file_id`` 指向源）：它没有自己的外挂字幕
与片头标记，播放时借用源文件的（引用而不复制，源文件后补的也能用）。手选原片（file_id）则一切照原片。
"""

from __future__ import annotations

from functools import partial
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.auth import reset_auth_state
from movieclaw_api.settings.store import reset_setting_store
from movieclaw_db.crypto import reset_secret_box
from movieclaw_db.engine import get_database
from movieclaw_db.models import (
    FileSource,
    FileState,
    LibraryFile,
    MediaItem,
    MediaSegmentState,
)
from movieclaw_db.repositories.library_repo import LibraryRepository

_PB = "/api/v1/playback"
_ADMIN = {"username": "admin", "password": "Sup3rSecret!"}
#: 全解码客户端：决策直接给档 0（原文件直出，不起 ffmpeg），字幕清单照常给
_APP = {"universal": True}
_EXTERNAL_HOST = "8.8.8.8"

_SOURCE_NAME = "Show S01E01.mkv"
_PORTABLE_NAME = "Show S01E01 - 省流 720p.mkv"
_CHS = "Show S01E01.chs.srt"
_CHS_TEXT = "1\n00:00:01,000 --> 00:00:02,000\n便携版也要有这条字幕\n"
_ENG = "Show S01E01.eng.srt"
_ENG_TEXT = "1\n00:00:01,000 --> 00:00:02,000\nAdded to the source later\n"
_AUDIO = [{"codec": "aac", "channels": 2, "language": "chi", "default": True}]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'portable.db'}")
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
    """把会话判成外网：客户端地址取公网段（真实链路里是反代转发来的 X-Forwarded-For）。"""
    monkeypatch.setattr(
        "movieclaw_api.api.routes.playback.client_address", lambda request: _EXTERNAL_HOST
    )


async def _seed(tmp_path: Path) -> dict:
    """一部剧一集：源文件（2160p，挂中文外挂字幕，同目录）+ 同一集的便携版（720p，另一目录）。"""
    root = tmp_path / "tv"
    portable_dir = tmp_path / "portable"
    (root / "Season 01").mkdir(parents=True, exist_ok=True)
    portable_dir.mkdir(parents=True, exist_ok=True)
    source_path = root / "Season 01" / _SOURCE_NAME
    source_path.write_bytes(b"FAKE-SOURCE" * 64)
    (root / "Season 01" / _CHS).write_text(_CHS_TEXT, encoding="utf-8")
    portable_path = portable_dir / _PORTABLE_NAME
    portable_path.write_bytes(b"FAKE-PORTABLE" * 64)

    async with get_database().session() as session:
        library = await LibraryRepository(session).create(
            name="剧集", kind="tv", root_paths=[str(root)]
        )
        item = MediaItem(kind="tv", tmdb_id=77, title="示例剧", original_title="Show")
        session.add(item)
        await session.flush()
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
            external_subtitles=[_entry(_CHS, "chi")],
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
            "source_dir": root / "Season 01",
            "source_size": source.size_bytes,
        }


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


def session_for(client: TestClient, ids: dict, **extra) -> dict:
    body = {
        "media_item_id": ids["item"],
        "season_number": 1,
        "episode_number": 1,
        "capability": _APP,
        **extra,
    }
    resp = client.post(f"{_PB}/sessions", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def subtitle_body(client: TestClient, url: str) -> str:
    resp = client.get(url)
    assert resp.status_code == 200, resp.text
    return resp.text


def track_refs(data: dict) -> list[str]:
    return [s["track_ref"] for s in data["decision"]["subtitles"]]


async def _add_source_subtitle(source_id: int, source_dir: Path) -> None:
    (source_dir / _ENG).write_text(_ENG_TEXT, encoding="utf-8")
    async with get_database().session() as session:
        row = await session.get(LibraryFile, source_id)
        assert row is not None
        row.external_subtitles = [*(row.external_subtitles or []), _entry(_ENG, "eng")]
        await session.commit()


async def _set_source_state(source_id: int, state: FileState) -> None:
    async with get_database().session() as session:
        row = await session.get(LibraryFile, source_id)
        assert row is not None
        row.state = state
        await session.commit()


async def _seed_source_segments(source_id: int, size_bytes: int) -> None:
    from movieclaw_api.services.library.skip_segments import ALGO_VERSION
    from movieclaw_db.models import utcnow

    async with get_database().session() as session:
        session.add(
            MediaSegmentState(
                library_file_id=source_id,
                fingerprint_status="ok",
                source_size=size_bytes,
                algo_version=ALGO_VERSION,
                analyzed_at=utcnow(),
                segments=[{"type": "intro", "start_ms": 0, "end_ms": 60_000, "to_end": False}],
            )
        )
        await session.commit()


def test_external_session_plays_portable_with_source_subtitles(client, tmp_path, monkeypatch):
    ids = call(client, _seed, tmp_path)
    _external(monkeypatch)

    data = session_for(client, ids)

    decision = data["decision"]
    assert decision["file_id"] == ids["portable"]
    assert decision["portable_source_file_id"] == ids["source"]
    assert decision["portable_resolution"] == "720p"
    # 字幕是源文件的外挂字幕，按引用下发（同一个 external 引用，由源文件所在目录解析）
    assert track_refs(data) == [f"external:{_CHS}"]
    assert "便携版也要有这条字幕" in subtitle_body(client, data["subtitle_urls"][0])


def test_subtitle_added_to_source_later_is_used_by_the_portable_copy(client, tmp_path, monkeypatch):
    ids = call(client, _seed, tmp_path)
    _external(monkeypatch)
    assert track_refs(session_for(client, ids)) == [f"external:{_CHS}"]

    call(client, _add_source_subtitle, ids["source"], ids["source_dir"])

    data = session_for(client, ids)
    assert data["decision"]["file_id"] == ids["portable"]
    assert track_refs(data) == [f"external:{_CHS}", f"external:{_ENG}"]
    eng_url = data["subtitle_urls"][track_refs(data).index(f"external:{_ENG}")]
    assert "Added to the source later" in subtitle_body(client, eng_url)


def test_portable_borrows_no_subtitles_while_its_source_is_not_in_place(
    client, tmp_path, monkeypatch
):
    """源文件失联（或进了回收站）：便携版不借它的外挂字幕，与详情页同一口径，只剩便携版自己的。"""
    ids = call(client, _seed, tmp_path)
    _external(monkeypatch)
    call(client, _set_source_state, ids["source"], FileState.MISSING)

    data = session_for(client, ids)

    assert data["decision"]["file_id"] == ids["portable"]
    assert track_refs(data) == []


def test_portable_skip_intro_comes_from_the_source_segments(client, tmp_path, monkeypatch):
    ids = call(client, _seed, tmp_path)
    _external(monkeypatch)
    call(client, _seed_source_segments, ids["source"], ids["source_size"])

    data = session_for(client, ids)

    assert data["decision"]["file_id"] == ids["portable"]
    assert [(s["type"], s["start_ms"], s["end_ms"]) for s in data["segments"]] == [
        ("intro", 0, 60_000)
    ]


def test_lan_session_plays_the_original_with_its_own_subtitles(client, tmp_path):
    """内网：便携版不参与择优，原片直出（原画），字幕照旧。"""
    ids = call(client, _seed, tmp_path)

    data = session_for(client, ids)

    assert data["decision"]["file_id"] == ids["source"]
    assert data["decision"]["portable_source_file_id"] is None
    assert track_refs(data) == [f"external:{_CHS}"]


def test_switching_to_the_original_by_file_id_plays_it_on_the_external_session(
    client, tmp_path, monkeypatch
):
    """「切到原画」带源文件 id 重开：只判原片，便携版不参与，字幕仍是源文件自己的。"""
    ids = call(client, _seed, tmp_path)
    _external(monkeypatch)

    data = session_for(client, ids, file_id=ids["source"])

    assert data["decision"]["file_id"] == ids["source"]
    assert data["decision"]["portable_source_file_id"] is None
    assert track_refs(data) == [f"external:{_CHS}"]


def test_subtitle_endpoint_serves_the_portable_copy_from_the_source_directory(
    client, tmp_path, monkeypatch
):
    """取字幕的端点按归属行解析：字幕 URL 挂的是便携版的 file_id，内容却只能来自源文件所在目录
    （便携版目录里没有这份 sidecar）。"""
    ids = call(client, _seed, tmp_path)
    _external(monkeypatch)
    data = session_for(client, ids)
    url = data["subtitle_urls"][0]
    assert f"/files/{ids['portable']}/subtitles" in url
    assert not (tmp_path / "portable" / _CHS).exists()

    assert "便携版也要有这条字幕" in subtitle_body(client, url)


def test_inherited_subtitle_finds_the_portable_copys_source_external_track():
    """同剧上一集的外挂字幕换到本集便携版：外挂候选看归属行（源文件），不看便携版自己的台账。"""
    from movieclaw_api.services.playback.track_memory import translate_subtitle
    from movieclaw_playback.track_policy import NO_CONTEXT, TrackContext

    prev = LibraryFile(
        file_path="/tv/S01E01.mkv",
        external_subtitles=[_entry("S01E01.chs.srt", "chi"), _entry("S01E01.eng.srt", "eng")],
    )
    episode2_source = LibraryFile(
        file_path="/tv/S01E02.mkv",
        external_subtitles=[_entry("S01E02.chs.srt", "chi"), _entry("S01E02.eng.srt", "eng")],
    )
    episode2_portable = LibraryFile(
        file_path="/portable/S01E02 - 720p.mkv", external_subtitles=None
    )

    owned = translate_subtitle(
        "external:S01E01.eng.srt",
        prev,
        episode2_portable,
        target_context=TrackContext(metadata_owner=episode2_source),
    )
    unowned = translate_subtitle("external:S01E01.eng.srt", prev, episode2_portable)

    assert owned == "external:S01E02.eng.srt"
    assert unowned is None
    assert NO_CONTEXT.metadata_owner is None
