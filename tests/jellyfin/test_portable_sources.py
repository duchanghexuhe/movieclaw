"""便携版的 Jellyfin 协议（docs/design/preload-transcode.md）。

外网会话的 PlaybackInfo 把便携版排在 MediaSources 首位（客户端默认就播它）；内网顺序不变。
便携版没有自己的外挂字幕，合成流编号与字幕投递照源文件的外挂字幕来（归属行，见 track_context）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from jellyfin.helpers import jf_login
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.models import FileSource, FileState, LibraryFile
from movieclaw_jellyfin.ids import item_guid, media_source_guid

SIDECAR_NAME = "Inception.2010.2160p.chs.srt"
SRT_TEXT = "1\n00:00:01,000 --> 00:00:02,500\n便携版的外挂字幕\n\n"
PORTABLE_NAME = "Inception.2010.720p - 省流.mkv"


@pytest.fixture
def portable_env(seeded: dict, media_root: Path) -> dict:
    """原片挂一条中文外挂字幕（同目录）；同一部片再有一份 720p 便携版（另一目录，派生行）。"""
    from movieclaw_api.core.config import get_settings as _settings

    movie_dir = media_root / "Inception (2010)"
    (movie_dir / SIDECAR_NAME).write_text(SRT_TEXT, encoding="utf-8")
    portable_dir = media_root / "portable"
    portable_dir.mkdir(exist_ok=True)
    portable_path = portable_dir / PORTABLE_NAME
    portable_path.write_bytes(b"FAKE-PORTABLE" * 64)

    async def _seed() -> dict:
        init_db(_settings().database_url, echo=False)
        async with get_database().session() as session:
            source = (
                await session.execute(
                    select(LibraryFile).where(
                        LibraryFile.file_path.like("%Inception.2010.2160p.mkv")
                    )
                )
            ).scalar_one()
            source.external_subtitles = [
                {
                    "filename": SIDECAR_NAME,
                    "format": "srt",
                    "language": "chi",
                    "title": None,
                    "default": True,
                    "forced": False,
                    "sdh": False,
                    "size_bytes": 1,
                    "file_mtime_ns": 1,
                }
            ]
            session.add(
                LibraryFile(
                    library_id=source.library_id,
                    media_item_id=source.media_item_id,
                    season_number=0,
                    episode_number=0,
                    file_path=str(portable_path),
                    size_bytes=portable_path.stat().st_size,
                    source=FileSource.IMPORTED,
                    state=FileState.IN_PLACE,
                    container="mkv",
                    video_codec="h264",
                    resolution="720p",
                    duration_seconds=148 * 60,
                    bit_rate=2_000_000,
                    audio_streams=source.audio_streams,
                    derives_from_file_id=source.id,
                    derivation={"kind": "preload", "preset_id": "portable_720p", "task_id": 1},
                )
            )
            await session.commit()
            portable = (
                await session.execute(
                    select(LibraryFile).where(LibraryFile.derives_from_file_id == source.id)
                )
            ).scalar_one()
            ids = {"source": source.id, "portable": portable.id}
        await dispose_db()
        return ids

    return {**asyncio.run(_seed()), "movie": seeded["movie"]}


@pytest.fixture
def pclient(portable_env: dict, client: TestClient) -> TestClient:
    """便携版在应用启动前播种完成（fixture 依赖顺序保证）。"""
    return client


def _playback_info(client: TestClient, token: str, guid: str) -> dict:
    resp = client.post(f"/Items/{guid}/PlaybackInfo", headers={"X-Emby-Token": token}, json={})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _external(monkeypatch) -> None:
    monkeypatch.setattr(
        "movieclaw_jellyfin.routes.playback.network_side.request_side", lambda request, **_kwargs: "outside"
    )


def test_lan_playback_info_keeps_the_original_first(
    pclient: TestClient, portable_env: dict
) -> None:
    """内网：便携版不改变顺序（协议层只在外网把它排首位）。"""
    token = jf_login(pclient)
    body = _playback_info(pclient, token, item_guid(portable_env["movie"]))

    ids = [s["Id"] for s in body["MediaSources"]]
    assert ids.index(media_source_guid(portable_env["source"])) < ids.index(
        media_source_guid(portable_env["portable"])
    )
    assert not any(s["Name"].startswith("便携版") for s in body["MediaSources"][:1])


def test_external_playback_info_puts_the_portable_copy_first(
    pclient: TestClient, portable_env: dict, monkeypatch
) -> None:
    _external(monkeypatch)
    token = jf_login(pclient)
    body = _playback_info(pclient, token, item_guid(portable_env["movie"]))

    first = body["MediaSources"][0]
    assert first["Id"] == media_source_guid(portable_env["portable"])
    assert first["Name"].startswith("便携版")


def test_portable_media_source_borrows_the_source_external_subtitle(
    pclient: TestClient, portable_env: dict, monkeypatch
) -> None:
    """便携版的外挂字幕流来自源文件（同目录 sidecar），字幕投递也挂在便携版的 MediaSource 下。"""
    _external(monkeypatch)
    token = jf_login(pclient)
    body = _playback_info(pclient, token, item_guid(portable_env["movie"]))
    first = body["MediaSources"][0]

    subs = [s for s in first["MediaStreams"] if s["Type"] == "Subtitle"]
    external = [s for s in subs if s["IsExternal"]]
    assert [s["Index"] for s in external] == [0]
    assert external[0]["Language"] == "chi"
    assert external[0]["Path"].endswith(SIDECAR_NAME)
    # 外挂流在前，视频 / 音轨的编号随外挂数整体后移（与源文件的合成编号同一口径）
    assert first["DefaultAudioStreamIndex"] == 2

    resp = pclient.get(external[0]["DeliveryUrl"])
    assert resp.status_code == 200, resp.text
    assert "便携版的外挂字幕" in resp.text
