"""剧集的音轨 / 字幕记忆按整部剧生效（2026-09-28 用户拍板：「仅记录到影片，每部影片都不一样」）。

观看状态按单集存（``playback_state`` 一集一行），电影一部一行、天然按片记；剧集却是每集各记各的，
新的一集没有记忆就回到默认轨——追剧时每集都要重选一遍国语、中文字幕。

这里在**本集没有记忆**时，取同一部剧最近一次有选择的那一集，把它的选择**按语言换算**成本集文件里的轨：
每集文件的轨序不一定相同（某集多一条评论音轨，下标就错位），外挂字幕的文件名更是每集不同，
只有「语言 + 类型」跨集稳定。换算不出来（本集没有同语言的轨）就不给，交回默认选择策略。

只沿用**用户改过的**选择。记忆本身已经只记用户的选择（上报的就是默认轨时不落库，见
``movieclaw_playback.state.apply_track_selection``）；这里仍按「和那一集的默认挑选一样就不沿用」
再筛一遍（「默认挑选」与记忆写入同一口径：默认轨策略 + 那一集的库语言、原始语言），
兜住原盘这类服务端判断不了、照原样记下的轨——那一集用的就是它的默认轨、或那一集本来
就不开字幕而记着「关闭」，都不是用户的意思，新的一集照自己的默认走——否则第 1 集没字幕，
第 2 集自带的默认中文字幕也会被一并关掉。

本集自己有记忆时永远以本集为准（用户在这一集里特意换过的轨不被别的集覆盖）；
用户在新的一集里一换轨，进度上报就把本集的选择记下来，之后这一集按自己的走。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.services.library.subtitles import LANGUAGE_TOKENS
from movieclaw_api.services.playback.track_context import track_context, unit_files_with_contexts
from movieclaw_db.models import FileState, LibraryFile, PlaybackState
from movieclaw_playback.subtitles import (
    SUBTITLE_OFF,
    parse_embedded_track,
    parse_external_track,
)
from movieclaw_playback.track_policy import (
    NO_CONTEXT,
    TrackContext,
    default_audio,
    default_subtitle,
)

Unit = tuple[int, int, int]

_BITMAP_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "pgssub", "dvdsub", "dvbsub"}


def _language(value: Any) -> str | None:
    """语言码归一成 ISO 639-2/B（zho → chi、deu → ger）；und / 空视为不知道"""
    if not isinstance(value, str) or not value.strip():
        return None
    lowered = value.strip().lower()
    if lowered in {"und", "unk", "mis", "mul", "zxx"}:
        return None
    return LANGUAGE_TOKENS.get(lowered, lowered)


def _is_bitmap(codec: Any) -> bool:
    return isinstance(codec, str) and codec.lower() in _BITMAP_CODECS


def translate_audio(
    ref: str | None,
    source: LibraryFile,
    target: LibraryFile,
    context: TrackContext = NO_CONTEXT,
) -> str | None:
    """上一集用户换过的音轨 → 本集同语言的音轨（编码、声道也一样的优先）；用的就是默认轨时不沿用。

    ``context`` 是上一集文件的默认轨策略上下文（判断「是不是默认轨」用）。
    """
    k = parse_embedded_track(ref) if ref else None
    streams = source.audio_streams or []
    if k is None or k >= len(streams) or k == default_audio(source, context).index:
        return None
    wanted = streams[k]
    language = _language(wanted.get("language"))
    if language is None:
        return None
    best: tuple[int, int] | None = None
    for j, track in enumerate(target.audio_streams or []):
        if _language(track.get("language")) != language:
            continue
        same_codec = track.get("codec") == wanted.get("codec")
        same_channels = track.get("channels") == wanted.get("channels")
        score = int(same_codec) * 2 + int(same_channels)
        if best is None or score > best[0]:
            best = (score, j)
    return f"embedded:{best[1]}" if best is not None else None


def translate_subtitle(
    ref: str | None,
    source: LibraryFile,
    target: LibraryFile,
    context: TrackContext = NO_CONTEXT,
    source_audio: str | None = None,
    target_context: TrackContext = NO_CONTEXT,
) -> str | None:
    """上一集用户换过的字幕 → 本集同语言、同类型的字幕；用户关掉的照旧关闭。

    用的就是那一集的默认字幕、或那一集本来就不开字幕（「关闭」只是默认状态）时不沿用。
    ``context`` 是上一集文件的默认轨策略上下文，``source_audio`` 是那一集放的音轨
    （记着的；没有即默认音轨）——默认字幕要看放的是哪种语言的音轨。``target_context`` 是本集的：
    便携版借用源文件的外挂字幕，本集的外挂候选要看归属行（``metadata_owner``）。
    """
    default_ref = default_subtitle(source, context, source_audio).ref
    if ref is None or ref == default_ref:
        return None
    if ref == SUBTITLE_OFF:
        return SUBTITLE_OFF if default_ref is not None else None
    k = parse_embedded_track(ref)
    if k is not None:
        streams = source.subtitle_streams or []
        if k >= len(streams):
            return None
        wanted = streams[k]
        language = _language(wanted.get("language"))
        bitmap = _is_bitmap(wanted.get("codec"))
        forced = bool(wanted.get("forced"))
        title = wanted.get("title")
    else:
        filename = parse_external_track(ref)
        externals = (context.metadata_owner or source).external_subtitles or []
        entry = next((e for e in externals if e.get("filename") == filename), None)
        if entry is None:
            return None
        language = _language(entry.get("language"))
        bitmap = str(entry.get("format") or "").lower() in {"sup", "sub", "idx"}
        forced = bool(entry.get("forced"))
        title = entry.get("title")
    if language is None:
        return None
    # 候选：(是否外挂与原来一致, 类型一致, 强制一致, 标题一致, 引用)；外挂与内封都收，按分数挑
    was_external = k is None
    best: tuple[tuple[int, ...], str] | None = None
    for j, track in enumerate(target.subtitle_streams or []):
        if _language(track.get("language")) != language:
            continue
        score = (
            int(not was_external),
            int(_is_bitmap(track.get("codec")) == bitmap),
            int(bool(track.get("forced")) == forced),
            int(track.get("title") == title),
        )
        if best is None or score > best[0]:
            best = (score, f"embedded:{j}")
    for entry in (target_context.metadata_owner or target).external_subtitles or []:
        if _language(entry.get("language")) != language or not entry.get("filename"):
            continue
        entry_bitmap = str(entry.get("format") or "").lower() in {"sup", "sub", "idx"}
        score = (
            int(was_external),
            int(entry_bitmap == bitmap),
            int(bool(entry.get("forced")) == forced),
            int(entry.get("title") == title),
        )
        if best is None or score > best[0]:
            best = (score, f"external:{entry['filename']}")
    # 类型对不上（图形换成文字）也算：同语言总比回到默认强
    return best[1] if best is not None else None


async def _unit_file(session: AsyncSession, unit: Unit) -> LibraryFile | None:
    media_item_id, season, episode = unit
    stmt = select(LibraryFile).where(
        LibraryFile.media_item_id == media_item_id,
        LibraryFile.season_number == season,
        LibraryFile.episode_number == episode,
        LibraryFile.state == FileState.IN_PLACE,
    )
    return (await session.execute(stmt)).scalars().first()


def inheritance_source(
    states: Mapping[Unit, PlaybackState], unit: Unit
) -> tuple[Unit, PlaybackState, bool, bool] | None:
    """本集缺记忆时从哪一集沿用：同剧最近一次有选择的那一集，连同本集缺的是不是音轨、字幕。

    电影、本集两样都记着、别的集都没有选择时返回 None。纯内存计算（``states`` 调用方已取好）。
    """
    media_item_id, season, episode = unit
    if season == 0 and episode == 0:
        return None
    own = states.get(unit)
    need_audio = own is None or not own.audio_track
    need_subtitle = own is None or not own.subtitle_track
    if not (need_audio or need_subtitle):
        return None
    candidates = [
        (key, row)
        for key, row in states.items()
        if key != unit and key[0] == media_item_id and (row.audio_track or row.subtitle_track)
    ]
    if not candidates:
        return None
    # 「最近」按上次播放时间比：updated_at 连标记已看、收藏这类写入都会动
    source_unit, source_row = max(
        candidates, key=lambda item: item[1].last_played_at or item[1].updated_at
    )
    return source_unit, source_row, need_audio, need_subtitle


def inherit_tracks(
    source_row: PlaybackState,
    source: LibraryFile,
    target: LibraryFile,
    context: TrackContext,
    *,
    need_audio: bool,
    need_subtitle: bool,
    target_context: TrackContext = NO_CONTEXT,
) -> tuple[str | None, str | None]:
    """上一集（``source`` 文件、``source_row`` 记忆）的选择按语言换算到本集的 ``target`` 文件。"""
    audio = (
        translate_audio(source_row.audio_track, source, target, context) if need_audio else None
    )
    subtitle = (
        translate_subtitle(
            source_row.subtitle_track,
            source,
            target,
            context,
            source_row.audio_track,
            target_context,
        )
        if need_subtitle
        else None
    )
    return audio, subtitle


async def series_track_memory(
    session: AsyncSession,
    states: dict[Unit, PlaybackState],
    unit: Unit,
    target: LibraryFile | None = None,
) -> tuple[str | None, str | None]:
    """本集没有记忆时，同剧最近一集的音轨 / 字幕选择换算到本集；电影、换算不出返回 None。

    ``states`` 是 ``playback_state.get_states`` 取回的这部剧全部单元的状态
    （开会话时本来就取了，不多查）。``target`` 不给时取本集在位的第一个文件。
    """
    picked = inheritance_source(states, unit)
    if picked is None:
        return None, None
    source_unit, source_row, need_audio, need_subtitle = picked
    # 上一集的默认轨策略上下文随文件同一条 SQL 取出（判断它的选择是不是默认挑选）
    sources, contexts = await unit_files_with_contexts(session, source_unit)
    source = sources[0] if sources else None
    target = target or await _unit_file(session, unit)
    if source is None or target is None:
        return None, None
    return inherit_tracks(
        source_row,
        source,
        target,
        contexts.get(source.id or 0, NO_CONTEXT),
        need_audio=need_audio,
        need_subtitle=need_subtitle,
        target_context=await track_context(session, target),
    )
