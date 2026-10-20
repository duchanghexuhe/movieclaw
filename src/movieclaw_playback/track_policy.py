"""默认音轨 / 字幕策略（默认轨策略改造第 2 步，2026-09-29 用户拍板）。

不经用户选择时放哪条音轨、开哪条字幕，全端（网页、App、Jellyfin 客户端、详情页展示）
只在这里算。**音轨跟片子走，字幕跟人走**：

- **音轨按影片原始语言**（刮削存下的 TMDB original_language）：英文片默认英语原声、
  港片默认粤语原声（TMDB 用 ``cn`` 表示粤语；``zh`` 分不清普通话与粤语，两种都算原声）。
  **只有标签明确说片源的默认轨不是原声，才换**：默认轨就是原声语言、或没标语言，照片源的来
  （直出放的就是它，网页端不必为换轨重封装、App 不必本机转音频）；要换时在原声语言的轨里
  挑音质最好的（无损 > 高码率有损 > 普通，再比声道数）。导评、口述影像这类特殊轨不自动选。
  原声语言的轨一条都没有时，退回旧规则（容器默认旗标 → 第一条）。
- **字幕按媒体库的元数据主语言**（用户读什么语言，库就刮成什么语言）：库语言的完整字幕
  优先（简繁按库的地区，zh-CN 简体优先），再跟**将要放的那条音轨**对一下：
  - 原声是外语 → 开库语言的完整字幕；
  - 原声就是库语言 → 中文库照样开（国内观众看国产片也习惯开字幕），其它语言的库
    只开库语言的强制字幕（片中外语对白的翻译），没有就不开；
  - 导评字幕不自动选，听障（SDH）排在普通字幕之后，只有特效 / 歌词的字幕当强制字幕看待；
  - 库语言的字幕一条都没有：原声是库语言时不开（不能给国产片挂一条英文字幕），
    原声是外语时退回旧规则（外挂 > AI > 默认旗标 > 强制）；
  - 要关掉的是片源默认开、却没标语言的字幕时照片源的来——标签没说它不对
    （国产片的默认字幕常常就是没标语言的中文字幕）。
- **播放时相信标签**：语言按轨道的语言标记、标题、外挂字幕文件名里的标记认，标错了是
  片库整理的事，不在播放这一刻去猜（用户拍板：这样代价最低）。

本模块是纯函数：不碰数据库、不碰磁盘，上下文（库语言、原始语言）由调用方取好传进来，
没有上下文（``NO_CONTEXT``）时结果与旧规则一致。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any

from movieclaw_db.models import LibraryFile
from movieclaw_playback.subtitles import (
    SUBTITLE_OFF,
    embedded_track,
    external_track,
    is_ai_generated,
    parse_embedded_track,
    pick_default_subtitle,
    subtitle_track_exists,
)

# -- 语言归一 ---------------------------------------------------------------

#: 原始语言 → 算原声的音轨语言：TMDB 的 ``zh`` 只说是中文，普通话、粤语都可能是原声
_ORIGINAL_AUDIO_MATCHES: dict[str, frozenset[str]] = {"chi": frozenset({"chi", "yue"})}

#: 各种写法 → ISO 639-2/B 三字码（与外挂字幕台账的口径一致：中文 chi、德语 ger、法语 fre）
_LANGUAGE_ALIASES: dict[str, str] = {
    # ISO 639-1
    "zh": "chi", "en": "eng", "ja": "jpn", "ko": "kor", "fr": "fre", "de": "ger", "es": "spa",
    "ru": "rus", "it": "ita", "pt": "por", "th": "tha", "hi": "hin", "ar": "ara", "vi": "vie",
    "id": "ind", "ms": "may", "tr": "tur", "pl": "pol", "nl": "dut", "sv": "swe", "da": "dan",
    "no": "nor", "fi": "fin", "cs": "cze", "hu": "hun", "el": "gre", "he": "heb", "uk": "ukr",
    "ro": "rum", "fa": "per", "ta": "tam", "te": "tel", "tl": "tgl",
    # ISO 639-2/T → B
    "zho": "chi", "deu": "ger", "fra": "fre", "ces": "cze", "nld": "dut", "ell": "gre",
    "fas": "per", "ron": "rum", "msa": "may",
    # 中文的细分写法（字幕组、ffprobe、BCP 47）
    "cmn": "chi", "chs": "chi", "cht": "chi", "zh-cn": "chi", "zh-sg": "chi", "zh-hans": "chi",
    "zh-tw": "chi", "zh-hk": "chi", "zh-mo": "chi", "zh-hant": "chi",
    # 英文名
    "chinese": "chi", "mandarin": "chi", "english": "eng", "japanese": "jpn", "korean": "kor",
    "french": "fre", "german": "ger", "spanish": "spa", "russian": "rus", "italian": "ita",
    "portuguese": "por", "thai": "tha", "cantonese": "yue", "hindi": "hin", "arabic": "ara",
}  # fmt: skip
_UNKNOWN_LANGUAGES = {"", "und", "unk", "mis", "mul", "zxx", "qaa"}
_SIMPLIFIED_TAGS = {"chs", "zh-cn", "zh-sg", "zh-hans", "sc", "gb"}
_TRADITIONAL_TAGS = {"cht", "zh-tw", "zh-hk", "zh-mo", "zh-hant", "tc", "big5"}


def normalize_language(value: Any) -> str | None:
    """语言标记 → 三字码；不认识的原样小写返回，没标 / und 返回 None。"""
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower().replace("_", "-")
    if lowered in _UNKNOWN_LANGUAGES:
        return None
    if lowered in _LANGUAGE_ALIASES:
        return _LANGUAGE_ALIASES[lowered]
    base = lowered.split("-", 1)[0]
    return _LANGUAGE_ALIASES.get(base, base if len(base) == 3 else lowered)


def _script_of_tag(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower().replace("_", "-")
    if lowered in _SIMPLIFIED_TAGS:
        return "hans"
    if lowered in _TRADITIONAL_TAGS:
        return "hant"
    return None


def original_language_code(value: Any) -> str | None:
    """TMDB 的 original_language → 三字码。TMDB 用 ``cn`` 表示粤语（``zh`` 是普通话）。"""
    if isinstance(value, str) and value.strip().lower() == "cn":
        return "yue"
    return normalize_language(value)


@dataclass(frozen=True)
class TrackContext:
    """算默认轨要的上下文：媒体库的元数据主语言（及简繁）、影片的原始语言，都可能不知道。"""

    library_language: str | None = None
    #: 库语言是中文时的简繁：zh-CN / zh-SG → hans，zh-TW / zh-HK → hant
    library_script: str | None = None
    original_language: str | None = None

    @classmethod
    def build(cls, library_locale: str | None, original_language: str | None) -> TrackContext:
        """``library_locale`` 是刮削语言（如 ``zh-CN`` / ``en-US``），
        ``original_language`` 是 TMDB 原始语言码。"""
        return cls(
            library_language=normalize_language(library_locale),
            library_script=_script_of_tag(library_locale),
            original_language=original_language_code(original_language),
        )


#: 没有上下文（不知道库语言与原始语言）：选轨结果与旧规则一致
NO_CONTEXT = TrackContext()


# -- 轨道属性（相信标签：语言标记 + 标题 + 外挂文件名里的标记）---------------------

_CANTONESE_WORDS = ("粤语", "粵語", "粤", "粵", "cantonese")
_MANDARIN_WORDS = ("国语", "國語", "普通话", "普通話", "mandarin")
_SIMPLIFIED_WORDS = ("简", "簡", "simplified")
_TRADITIONAL_WORDS = ("繁", "traditional")
_CHINESE_WORDS = ("中文", "中字", "chinese", "华语", "華語")
_BILINGUAL_WORDS = ("双语", "雙語", "中英", "简英", "繁英", "簡英", "bilingual")
_SDH_WORDS = ("听障", "聽障", "hearing impaired")
_COMMENTARY_WORDS = ("commentary", "评论", "評論", "导评", "導評", "解说", "解說", "花絮")
_DESCRIPTION_WORDS = ("audio description", "descriptive", "口述", "描述音轨", "描述音軌")
_PARTIAL_WORDS = ("signs", "songs", "特效", "歌词", "歌詞", "forced", "强制", "強制", "foreign")


def _latin_tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", text.lower()))


def _has_word(text: str, words: tuple[str, ...]) -> bool:
    """英文单词按整词比（免得 sc 命中 script），中文与带空格的短语按子串比。"""
    lowered = text.lower()
    tokens = _latin_tokens(lowered)
    return any(
        (word in tokens) if word.isascii() and " " not in word else (word in lowered)
        for word in words
    )


@dataclass(frozen=True)
class _Subtitle:
    ref: str
    order: int
    external: bool
    ai: bool
    default_flag: bool
    #: 覆盖到的语言（双语字幕两种都算）
    languages: frozenset[str]
    script: str | None
    bilingual: bool
    #: 只翻译局部的字幕：强制字幕、只有特效 / 歌词的
    partial: bool
    sdh: bool
    commentary: bool


def _subtitle_from(
    ref: str,
    order: int,
    *,
    external: bool,
    language: Any,
    texts: list[str],
    default_flag: bool,
    forced: bool,
    sdh: bool,
    ai: bool,
) -> _Subtitle:
    text = " ".join(t for t in texts if t)
    tokens = _latin_tokens(text)
    languages: set[str] = set()
    code = normalize_language(language)
    if code:
        languages.add(code)
    script = _script_of_tag(language)
    for token in tokens:
        # 字幕组写法（chs / cht）与 BCP 47 写法（AI 字幕的 zh-Hans，拆词后是 hans / hant）
        if token in _SIMPLIFIED_TAGS or token == "hans":
            script = script or "hans"
            languages.add("chi")
        elif token in _TRADITIONAL_TAGS or token == "hant":
            script = script or "hant"
            languages.add("chi")
        elif token in {"eng", "english"}:
            languages.add("eng")
        elif token in {"jpn", "japanese"}:
            languages.add("jpn")
        elif token in {"kor", "korean"}:
            languages.add("kor")
    if _has_word(text, _SIMPLIFIED_WORDS):
        script = script or "hans"
        languages.add("chi")
    if _has_word(text, _TRADITIONAL_WORDS):
        script = script or "hant"
        languages.add("chi")
    if _has_word(text, _CHINESE_WORDS):
        languages.add("chi")
    bilingual = _has_word(text, _BILINGUAL_WORDS) or len(languages) > 1
    if bilingual and _has_word(text, ("中英", "简英", "繁英", "簡英")):
        languages.update({"chi", "eng"})
    return _Subtitle(
        ref=ref,
        order=order,
        external=external,
        ai=ai,
        default_flag=default_flag,
        languages=frozenset(languages),
        script=script,
        bilingual=bilingual,
        partial=forced or _has_word(text, _PARTIAL_WORDS),
        sdh=sdh or "sdh" in tokens or _has_word(text, _SDH_WORDS),
        commentary=_has_word(text, _COMMENTARY_WORDS),
    )


def _external_marks(file: LibraryFile, filename: str) -> str:
    """外挂字幕文件名里视频名之后、扩展名之前的那段标记（``Movie.chs&eng.ass`` → ``chs&eng``）。

    只读这一段：视频名本身可能带着 TC（枪版）、Chinese 这类字样，整个文件名一起读
    会误判语言和简繁。
    """
    stem = PurePath(filename).stem
    video_stem = PurePath(file.file_path or "").stem
    if video_stem and stem.lower().startswith(video_stem.lower()):
        return stem[len(video_stem) :].lstrip(".")
    return ""


def _subtitles(file: LibraryFile) -> list[_Subtitle]:
    result: list[_Subtitle] = []
    order = 0
    for index, raw in enumerate(file.subtitle_streams or []):
        raw = raw if isinstance(raw, dict) else {}
        result.append(
            _subtitle_from(
                embedded_track(index),
                order,
                external=False,
                language=raw.get("language"),
                texts=[raw.get("title") or ""],
                default_flag=bool(raw.get("default")),
                forced=bool(raw.get("forced")),
                sdh=False,
                ai=False,
            )
        )
        order += 1
    for entry in file.external_subtitles or []:
        if not isinstance(entry, dict) or not entry.get("filename"):
            continue
        filename = str(entry["filename"])
        result.append(
            _subtitle_from(
                external_track(filename),
                order,
                external=True,
                language=entry.get("language"),
                # 文件名里的 chs / cht / 简英 这类标记入台账时被当成语言吃掉了，这里再读一遍
                texts=[entry.get("title") or "", _external_marks(file, filename)],
                default_flag=bool(entry.get("default")),
                forced=bool(entry.get("forced")),
                sdh=bool(entry.get("sdh")),
                ai=is_ai_generated(entry.get("title")),
            )
        )
        order += 1
    return result


def _audio_language(raw: dict) -> str | None:
    """音轨语言：先看标题里的粤语 / 国语（很多片把粤语也标成 chi），再看语言标记。"""
    title = raw.get("title") or ""
    if _has_word(title, _CANTONESE_WORDS):
        return "yue"
    code = normalize_language(raw.get("language"))
    if code in (None, "chi") and _has_word(title, _MANDARIN_WORDS):
        return "chi"
    if code is None and _has_word(title, _CHINESE_WORDS):
        return "chi"
    return code


def _audio_special(raw: dict) -> bool:
    title = raw.get("title") or ""
    return _has_word(title, _COMMENTARY_WORDS) or _has_word(title, _DESCRIPTION_WORDS)


def _audio_quality(raw: dict) -> tuple[int, int]:
    """（音质档，声道数）：无损 3 > 高码率有损 2 > 普通有损 1 > 其余 0。"""
    codec = (raw.get("codec") or "").lower()
    profile = (raw.get("profile") or "").lower()
    if (
        codec in {"truehd", "mlp", "flac", "alac"}
        or codec.startswith("pcm_")
        or (codec == "dts" and ("hd ma" in profile or "dts:x" in profile))
    ):
        tier = 3
    elif codec == "eac3" or (codec == "dts" and "hd" in profile):
        tier = 2
    elif codec in {"dts", "ac3"}:
        tier = 1
    else:
        tier = 0
    channels = raw.get("channels")
    return tier, channels if isinstance(channels, int) else 2


# -- 选轨 -------------------------------------------------------------------


@dataclass(frozen=True)
class AudioChoice:
    #: audio_streams 的下标；没有音轨为 None
    index: int | None
    #: 为什么是它：remembered（记着的）/ original_language（原声）/
    #: default_flag（文件标的默认）/ first（第一条）/ none（没有音轨）
    reason: str

    @property
    def ref(self) -> str | None:
        return embedded_track(self.index) if self.index is not None else None


@dataclass(frozen=True)
class SubtitleChoice:
    #: 中性引用；None = 不开字幕（记着「关」时是 ``off``）
    ref: str | None
    #: 为什么是它：
    #: - remembered：记着的（含记着关）；
    #: - library_language：库语言的完整字幕；forced：库语言的强制字幕；
    #: - same_language_off：原声就是库语言，不开；
    #: - no_language_match：原声是库语言、但没有库语言字幕，不开；
    #: - 旧规则的 external / default_flag / forced_only / none
    #:   （外挂、默认旗标、只有强制字幕、都没有）
    reason: str


def default_audio(file: LibraryFile, context: TrackContext = NO_CONTEXT) -> AudioChoice:
    """不经用户选择时放的音轨。"""
    streams = [(i, raw) for i, raw in enumerate(file.audio_streams or []) if isinstance(raw, dict)]
    if not streams:
        return AudioChoice(None, "none")
    usable = [s for s in streams if s[1].get("codec")] or streams
    normal = [s for s in usable if not _audio_special(s[1])] or usable
    # 片源的默认轨（直出放的就是它）：标了默认的第一条，否则第一条
    flagged = next((s for s in normal if s[1].get("default")), None)
    container = flagged or normal[0]
    legacy = AudioChoice(container[0], "default_flag" if flagged else "first")
    original = context.original_language
    if not original:
        return legacy
    wanted = _ORIGINAL_AUDIO_MATCHES.get(original, frozenset({original}))
    current = _audio_language(container[1])
    # 只有标签明确说默认轨不是原声才换：它就是原声、或没标语言，照片源的来
    # （NAS 实测：没标语言的默认轨、TMDB 标 zh 的粤语片，换走都是错的）
    if current in wanted:
        return AudioChoice(container[0], "original_language")
    if current is None:
        return legacy
    matched = [s for s in normal if _audio_language(s[1]) in wanted]
    if not matched:
        return legacy
    best = max(matched, key=lambda s: (*_audio_quality(s[1]), -s[0]))
    return AudioChoice(best[0], "original_language")


def dubbed_default_original(file: LibraryFile, context: TrackContext = NO_CONTEXT) -> int | None:
    """默认音轨是配音（标了语言、且不是原始语言）时，返回原始语言轨的下标；否则 None。

    给便携版（预转码）选轨用：默认轨之外还要留一条原声，观众能切回原声。
    注意 :func:`default_audio` 在原始语言轨存在时本来就选原声，所以这里只在它退回
    旧规则（没有可比的原声）时才会有结果。
    """
    original = context.original_language
    if not original:
        return None
    choice = default_audio(file, context)
    if choice.index is None or choice.reason == "original_language":
        return None
    streams = [(i, raw) for i, raw in enumerate(file.audio_streams or []) if isinstance(raw, dict)]
    usable = [s for s in streams if s[1].get("codec")] or streams
    normal = [s for s in usable if not _audio_special(s[1])] or usable
    wanted = _ORIGINAL_AUDIO_MATCHES.get(original, frozenset({original}))
    current = _audio_language(file.audio_streams[choice.index])
    if current is None or current in wanted:
        return None
    matched = [s for s in normal if _audio_language(s[1]) in wanted]
    if not matched:
        return None
    return max(matched, key=lambda s: (*_audio_quality(s[1]), -s[0]))[0]


def library_subtitle_indexes(file: LibraryFile, context: TrackContext = NO_CONTEXT) -> list[int]:
    """库语言的内封字幕下标（含简繁、双语；不含评论字幕）。库语言未知时为空。"""
    library = context.library_language
    if not library:
        return []
    indexes: list[int] = []
    for subtitle in _subtitles(file):
        if subtitle.external or subtitle.commentary or library not in subtitle.languages:
            continue
        index = parse_embedded_track(subtitle.ref)
        if index is not None:
            indexes.append(index)
    return indexes


def default_subtitle(
    file: LibraryFile, context: TrackContext = NO_CONTEXT, audio_ref: str | None = None
) -> SubtitleChoice:
    """不经用户选择时开的字幕。

    ``audio_ref`` 是将要放的音轨（用户选的或默认的），不给按默认音轨算。
    """
    library = context.library_language
    if library:
        audio = _playing_audio(file, context, audio_ref)
        subtitles = _subtitles(file)
        candidates = [s for s in subtitles if not s.commentary and library in s.languages]
        full = [s for s in candidates if not s.partial]
        partial = [s for s in candidates if s.partial]
        same_language = audio == library
        if same_language and library != "chi":
            if partial:
                return SubtitleChoice(_best(partial, context).ref, "forced")
            return _off_unless_unlabeled(file, subtitles, "same_language_off")
        if full:
            return SubtitleChoice(_best(full, context).ref, "library_language")
        if same_language:
            if partial:
                return SubtitleChoice(_best(partial, context).ref, "forced")
            return _off_unless_unlabeled(file, subtitles, "no_language_match")
    return _legacy_subtitle(file)


def _off_unless_unlabeled(
    file: LibraryFile, subtitles: list[_Subtitle], reason: str
) -> SubtitleChoice:
    """原声就是库语言时不开字幕——但旧规则会开的那条没标语言时照片源的来：标签没说它不对
    （NAS 实测：国产片的默认字幕常常就是没标语言的中文字幕）。"""
    legacy = _legacy_subtitle(file)
    unlabeled = {s.ref for s in subtitles if not s.languages}
    if legacy.ref is not None and legacy.ref in unlabeled:
        return legacy
    return SubtitleChoice(None, reason)


def default_tracks(
    file: LibraryFile, context: TrackContext = NO_CONTEXT, preferred_audio: str | None = None
) -> tuple[AudioChoice, SubtitleChoice]:
    """默认音轨与字幕一起算：字幕看的是将要放的音轨（用户选了就按用户选的）。"""
    audio = default_audio(file, context)
    playing = preferred_audio if _audio_exists(file, preferred_audio) else audio.ref
    return audio, default_subtitle(file, context, playing)


def resolve_audio(
    file: LibraryFile, remembered: str | None, context: TrackContext = NO_CONTEXT
) -> AudioChoice:
    """这次放哪条音轨：记着的（还有效）优先，否则默认轨策略。"""
    if _audio_exists(file, remembered):
        return AudioChoice(parse_embedded_track(remembered), "remembered")
    return default_audio(file, context)


def resolve_subtitle(
    file: LibraryFile,
    remembered: str | None,
    context: TrackContext = NO_CONTEXT,
    audio_ref: str | None = None,
) -> SubtitleChoice:
    """这次开哪条字幕：记着「关」就关（ref 为 ``off``）、记着的轨还在就用它，
    否则默认轨策略（ref 为 None = 策略也不开）。``audio_ref`` 是这次放的音轨，
    不给按默认音轨算。"""
    if remembered == SUBTITLE_OFF:
        return SubtitleChoice(SUBTITLE_OFF, "remembered")
    if remembered is not None and subtitle_track_exists(file, remembered):
        return SubtitleChoice(remembered, "remembered")
    return default_subtitle(file, context, audio_ref)


def default_subtitle_or_off(
    file: LibraryFile, context: TrackContext = NO_CONTEXT, audio_ref: str | None = None
) -> str:
    """默认字幕的中性引用，不开时是 ``off``（轨记忆比对「是不是默认挑选」用）。"""
    return default_subtitle(file, context, audio_ref).ref or SUBTITLE_OFF


def _audio_exists(file: LibraryFile, ref: str | None) -> bool:
    k = parse_embedded_track(ref) if ref else None
    return k is not None and 0 <= k < len(file.audio_streams or [])


def _playing_audio(file: LibraryFile, context: TrackContext, audio_ref: str | None) -> str | None:
    """将要放的那条音轨的语言；轨上没标语言时按原始语言算（放的多半就是原声）。"""
    streams = file.audio_streams or []
    k = parse_embedded_track(audio_ref) if audio_ref else None
    if k is None or not 0 <= k < len(streams):
        k = default_audio(file, context).index
    if k is None or not isinstance(streams[k], dict):
        return context.original_language
    return _audio_language(streams[k]) or context.original_language


def _best(subtitles: list[_Subtitle], context: TrackContext) -> _Subtitle:
    """同是库语言的几条字幕里挑一条：
    简繁对得上 > 非听障 > AI 生成 > 外挂 > 单语 > 默认旗标 > 原顺序。"""

    def script_rank(s: _Subtitle) -> int:
        if context.library_script is None or s.script == context.library_script:
            return 0
        return 1 if s.script is None else 2

    return min(
        subtitles,
        key=lambda s: (
            script_rank(s),
            s.sdh,
            not s.ai,
            not s.external,
            s.bilingual,
            not s.default_flag,
            s.order,
        ),
    )


def _legacy_subtitle(file: LibraryFile) -> SubtitleChoice:
    """没有库语言可比时的旧规则（外挂 > AI > 默认旗标 > 强制），原因按选中的那条说。"""
    ref = pick_default_subtitle(file)
    if ref is None:
        return SubtitleChoice(None, "none")
    if ref.startswith("external:"):
        return SubtitleChoice(ref, "external")
    k = parse_embedded_track(ref)
    raw = (file.subtitle_streams or [])[k] if k is not None else {}
    if isinstance(raw, dict) and raw.get("default"):
        return SubtitleChoice(ref, "default_flag")
    return SubtitleChoice(ref, "forced_only")


__all__ = [
    "NO_CONTEXT",
    "AudioChoice",
    "SubtitleChoice",
    "TrackContext",
    "default_audio",
    "default_subtitle",
    "default_subtitle_or_off",
    "default_tracks",
    "normalize_language",
    "original_language_code",
    "resolve_audio",
    "resolve_subtitle",
]
