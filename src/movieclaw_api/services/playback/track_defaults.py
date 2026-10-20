"""详情页上的「默认会放哪条」（默认轨策略改造第 2 步，2026-09-29 用户拍板）。

详情页的音轨 / 字幕区原来按容器里的默认旗标标「默认」，那只是片源的标注；真正起播放哪条要看
这个成员的记忆、同剧上一集的选择和默认轨策略（原声 / 库语言）。这里按起播同一口径算好交给详情页：

- 本集记着的（用户换过的）> 沿用同剧最近一集的选择（按语言换算）> 默认轨策略；
- 字幕看的是将要放的那条音轨；
- 附一句中文原因，详情页直接展示。

全是内存计算：观看状态由详情路由一次查询取完这个条目的全部单元，上下文用详情路由手里的
库与条目元数据拼，上一集的文件从详情页本来就有的文件里找，不另查库。原盘（服务端读不到
盘内的轨，以播放器引擎读到的为准）和还没探测过轨道的文件不给。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace

from movieclaw_api.services.playback.track_memory import inherit_tracks, inheritance_source
from movieclaw_db.models import LibraryFile, PlaybackState
from movieclaw_playback.subtitles import SUBTITLE_OFF, parse_embedded_track
from movieclaw_playback.track_policy import (
    AudioChoice,
    SubtitleChoice,
    TrackContext,
    resolve_audio,
    resolve_subtitle,
)

Unit = tuple[int, int, int]

_AUDIO_NOTES = {
    "remembered": "你上次看时换的音轨",
    "series": "沿用这部剧上一集换的音轨",
    "original_language": "影片原声",
    "default_flag": "片源标注的默认音轨",
    "first": "第一条音轨",
    "none": "没有音轨",
}
_SUBTITLE_NOTES = {
    "remembered": "你上次看时选的字幕",
    "series": "沿用这部剧上一集选的字幕",
    "library_language": "媒体库语言的字幕",
    "forced": "原声就是媒体库语言，只开强制字幕（片中外语对白的翻译）",
    "same_language_off": "原声就是媒体库语言，默认不开字幕",
    "no_language_match": "原声就是媒体库语言、又没有这种语言的字幕，默认不开",
    "external": "外挂字幕优先",
    "default_flag": "片源标注的默认字幕",
    "forced_only": "只有强制字幕",
    "none": "没有要自动开的字幕",
}
#: 记着「关」时的说法（ref 为 off）
_SUBTITLE_OFF_NOTES = {
    "remembered": "你上次看时关掉了字幕",
    "series": "沿用这部剧上一集：不开字幕",
}


@dataclass(frozen=True)
class FileTrackDefaults:
    """一个文件不经用户操作时会放的音轨与字幕。"""

    audio: AudioChoice
    subtitle: SubtitleChoice

    @property
    def subtitle_ref(self) -> str | None:
        """将要开的字幕；None = 不开（含记着关）。"""
        ref = self.subtitle.ref
        return None if ref in (None, SUBTITLE_OFF) else ref

    @property
    def audio_note(self) -> str:
        return _AUDIO_NOTES.get(self.audio.reason, "")

    @property
    def subtitle_note(self) -> str:
        if self.subtitle.ref == SUBTITLE_OFF:
            return _SUBTITLE_OFF_NOTES.get(self.subtitle.reason, "")
        return _SUBTITLE_NOTES.get(self.subtitle.reason, "")


def file_track_defaults(
    file: LibraryFile,
    context: TrackContext,
    states: Mapping[Unit, PlaybackState],
    unit_files: Mapping[Unit, LibraryFile],
    owners: Mapping[int, LibraryFile] | None = None,
) -> FileTrackDefaults | None:
    """这个文件起播时（用户不动菜单）会放的音轨与字幕，和开会话同一口径。

    ``states`` 是这个成员在该条目上的全部观看状态，``unit_files`` 是详情页手里各单元的
    在位文件（找上一集的文件用）；详情页只看一个库里的一个条目，库语言与原始语言共用一份。
    ``owners`` 是便携版（派生行）id → 归属行（源文件，见 ``metadata_owner``）：便携版的外挂字幕
    候选借用它的，上一集的文件也按它自己的归属行算。
    原盘、还没探测过轨道的文件返回 None（界面退回按片源标注的默认旗标展示）。
    """
    if file.is_disc() or file.audio_streams is None or file.subtitle_streams is None:
        return None
    owners = owners or {}

    def owned(item: LibraryFile) -> TrackContext:
        return replace(context, metadata_owner=owners.get(item.id or -1))

    unit = (file.media_item_id or 0, file.season_number or 0, file.episode_number or 0)
    own = states.get(unit)
    target = owned(file)
    inherited_audio = inherited_subtitle = None
    picked = inheritance_source(states, unit)
    if picked is not None:
        source_unit, source_row, need_audio, need_subtitle = picked
        source = unit_files.get(source_unit)
        if source is not None:
            inherited_audio, inherited_subtitle = inherit_tracks(
                source_row,
                source,
                file,
                owned(source),
                need_audio=need_audio,
                need_subtitle=need_subtitle,
                target_context=target,
            )
    audio = resolve_audio(file, own.audio_track if own else None, target)
    if audio.reason != "remembered" and inherited_audio is not None:
        audio = AudioChoice(parse_embedded_track(inherited_audio), "series")
    subtitle = resolve_subtitle(file, own.subtitle_track if own else None, target, audio.ref)
    if subtitle.reason != "remembered" and inherited_subtitle is not None:
        subtitle = SubtitleChoice(inherited_subtitle, "series")
    return FileTrackDefaults(audio, subtitle)
