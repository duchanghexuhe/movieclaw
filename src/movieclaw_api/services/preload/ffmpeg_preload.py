"""预转码的 ffmpeg 参数构造（docs/design/preload-transcode.md §2/§4/§5）。

与实时转码（``ffmpeg_args.build_hls_command``）共用 HDR 色调映射、硬件链与色彩转换的
**思路与参数表**（``HW_BACKENDS`` / 原生 tone-map 自检 / ``tonemapx``），输出侧完全不同：

- 实时转码：HLS 分片 + 码率阶梯（受播放实时性约束）；
- 预转码：整文件 MKV + 质量优先参数——闲时干活，**越快转完越省电**，所以有硬件就用硬件。

执行端的硬件链（用户拍板：硬件优先）：
- Worker 上报本机能力（claim 携带 ``encoders``/``filters``）；NVENC 全 GPU 链；
- NAS 本地（``LocalHw``，自检结果来自 ``hwprobe``）：QSV / VAAPI；HDR 有原生 tone-map 用
  原生，没有就 CPU ``tonemapx`` 下载→映射→回传 GPU 编码（jellyfin-ffmpeg 镜像都有）；
- 没有硬件：libx264 软编（H.264）。HEVC 没有硬件编码器就判为不可执行（软编 HEVC 太慢）。

**字幕与音轨（§5，用户拍板）**：

- 音轨：只留默认轨（``track_policy.default_audio``），默认轨是配音且与原声不同时再加原声；
  最多两条，统一下混成立体声 AAC（省流 128k、高清 192k），评论轨丢弃；
- 字幕：文本字幕（SRT/ASS/SSA）原样内封（``-c:s copy``）；PGS 只留默认字幕、库语言字幕
  （含简繁/双语）和强制字幕；VobSub 跳过（播放器不支持）。外挂/AI 字幕不复制。
- 字幕**不进分段**：分段用 ``-ss``（输入侧）切时，字幕包既不裁剪、时间戳也不跟着平移，
  concat 后会重复、错位（真机实测，见 ``final_mux_argv``）。所以分段只含视频与音频，
  字幕在拼接时从源文件整轨一次拷进去（时间轴与源一致，段边界零问题）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from movieclaw_api.services.playback.ffmpeg_args import (
    _BT709_OUTPUT_TAGS,
    _COLOR_CONVERT_INPUT,
    _SOFTWARE_TONEMAP,
    HW_BACKENDS,
    HwBackend,
)
from movieclaw_playback.subtitles import parse_embedded_track
from movieclaw_playback.track_policy import (
    NO_CONTEXT,
    TrackContext,
    default_audio,
    default_subtitle,
    dubbed_default_original,
    library_subtitle_indexes,
)

#: 预转码编码格式（全局设置，下单时快照到任务行）：h264 全设备兼容；
#: hevc 同流量下清晰度更高，但不支持的设备回退服务器实时转码。
FORMAT_H264 = "h264"
FORMAT_HEVC = "hevc"
VIDEO_FORMATS = (FORMAT_H264, FORMAT_HEVC)
FORMAT_DEFAULT = FORMAT_H264

#: 档位注册表（§2「档位是接口」）：**只有「省流/高清」两个名字**——用户
#: 不接触参数，规格由格式各自定义。分辨率档位与播放页清晰度对应（产物
#: 高度 ≤ 播放页所选上限时直通/重封装命中，零转码）：
#: - 省流 = 各编码下最省流量的点：H.264 480p·1.5M（= 播放页 480p 档），
#:   HEVC 1080p·1.5M（原省流档原样——效率红利在这：同流量清晰度更高）；
#: - 高清 = 各编码下更清晰的点：H.264 720p·3M（= 播放页 720p 档），
#:   HEVC 1080p·4M（原高清档换编码，码率不动）。
#: 音频：统一立体声 AAC，省流 128k、高清 192k（§5，用户拍板）。
PRESET_SAVER = "saver"
PRESET_PREMIUM = "premium"
PRESET_IDS = (PRESET_SAVER, PRESET_PREMIUM)
PRESET_DEFAULT = PRESET_SAVER

_PRESET_SPECS: dict[str, dict] = {
    PRESET_SAVER: {
        "short": "省流",
        "variants": {
            FORMAT_H264: {
                "height": 480, "maxrate": "1.5M", "bufsize": "3M",
                "audio_bitrate": "128k", "video_codec": "h264",
            },
            FORMAT_HEVC: {
                "height": 1080, "maxrate": "1.5M", "bufsize": "3M",
                "audio_bitrate": "128k", "video_codec": "hevc",
            },
        },
    },
    PRESET_PREMIUM: {
        "short": "高清",
        "variants": {
            FORMAT_H264: {
                "height": 720, "maxrate": "3M", "bufsize": "6M",
                "audio_bitrate": "192k", "video_codec": "h264",
            },
            FORMAT_HEVC: {
                "height": 1080, "maxrate": "4M", "bufsize": "8M",
                "audio_bitrate": "192k", "video_codec": "hevc",
            },
        },
    },
}


def effective_preset(preset_id: str, video_format: str | None) -> dict:
    """档位 × 格式 → 与旧版同形的扁平档位 dict（下游零改动消费）。

    未知格式（历史行 NULL）回退 H.264；未知档位回退默认档——登录验收/
    显示路径的兜底，下单路径用 ``preset_available`` 做严格校验。
    """
    spec = _PRESET_SPECS.get(preset_id) or _PRESET_SPECS[PRESET_DEFAULT]
    fmt = video_format if video_format in VIDEO_FORMATS else FORMAT_DEFAULT
    variant = spec["variants"][fmt]
    return {"id": preset_id, "short": spec["short"], **variant}


def preset_available(preset_id: str, video_format: str) -> bool:
    """下单/预估的严格校验：档位存在（格式 × 档位当前恒有组合）。"""
    return preset_id in _PRESET_SPECS and video_format in VIDEO_FORMATS


def presets_for_format(video_format: str) -> list[dict]:
    """某格式下的全部档位（presets 接口/标题动态化的数据源）。"""
    return [effective_preset(pid, video_format) for pid in PRESET_IDS]


#: 断点续转的产物分段大小（§7）：64 MiB 一段，上传失败只重传一段。
PRELOAD_PART_BYTES = 64 * 1024 * 1024

#: 文本字幕：原样内封（``-c:s copy``），ASS 特效由网页端 JASSUB 渲染（§5）
TEXT_SUBTITLE_CODECS = frozenset({"subrip", "srt", "ass", "ssa", "mov_text", "webvtt"})
#: PGS 图形字幕：只留默认 / 库语言 / 强制三类（§5，网页端 libbitsub 渲染，不烧录）
PGS_SUBTITLE_CODECS = frozenset({"hdmv_pgs_subtitle", "pgssub"})

#: 音轨统一下混成的声道数（§5：立体声 AAC）
AUDIO_CHANNELS = 2
#: 最多保留的音轨数：默认轨 + 原声（§5）
MAX_AUDIO_TRACKS = 2

#: NAS 本地硬件后端（§7）：只用 QSV / VAAPI，设备路径与 ``HW_BACKENDS`` 同源
LOCAL_HW_BACKENDS = ("vaapi", "qsv")
_VAAPI_DEVICE = "/dev/dri/renderD128"


@dataclass(frozen=True)
class TrackPlan:
    """这一单保留哪些轨（源 ``audio_streams`` / ``subtitle_streams`` 的下标）。

    ``audio`` 的顺序即输出顺序：默认轨在前。
    """

    audio: tuple[int, ...] = ()
    subtitles: tuple[int, ...] = ()


def plan_tracks(file: Any, context: TrackContext = NO_CONTEXT) -> TrackPlan:
    """选轨（§5）：音轨只留默认轨（+ 配音时的原声），字幕文本全留、PGS 按规则留。

    ``file`` 只需要 ``audio_streams`` / ``subtitle_streams`` / ``external_subtitles`` /
    ``file_path`` 这几个属性（LibraryFile 与测试用的轻量对象都行）。
    """
    choice = default_audio(file, context)
    audio: list[int] = [] if choice.index is None else [choice.index]
    extra = dubbed_default_original(file, context)
    if extra is not None and extra not in audio and len(audio) < MAX_AUDIO_TRACKS:
        audio.append(extra)

    pgs_wanted = _pgs_wanted(file, context, choice.ref)
    subtitles: list[int] = []
    for index, raw in enumerate(file.subtitle_streams or []):
        codec = str(raw.get("codec") or "").lower() if isinstance(raw, dict) else ""
        if codec in TEXT_SUBTITLE_CODECS or (codec in PGS_SUBTITLE_CODECS and index in pgs_wanted):
            subtitles.append(index)
    return TrackPlan(audio=tuple(audio), subtitles=tuple(subtitles))


def _pgs_wanted(file: Any, context: TrackContext, audio_ref: str | None) -> set[int]:
    """PGS 要留的下标：默认字幕（按将要放的音轨算）+ 库语言字幕 + 强制字幕（§5）。"""
    wanted = set(library_subtitle_indexes(file, context))
    default_ref = default_subtitle(file, context, audio_ref).ref
    default_index = parse_embedded_track(default_ref) if default_ref else None
    if default_index is not None:
        wanted.add(default_index)
    for index, raw in enumerate(file.subtitle_streams or []):
        if isinstance(raw, dict) and raw.get("forced"):
            wanted.add(index)
    return wanted


def _source_needs_tone_map(hdr: str | None, dv_profile: int | None) -> bool:
    """HDR10 / HLG / 杜比视界（按 HDR10 兼容层处理）都要色调映射到 SDR（§4）。"""
    return bool(hdr) or dv_profile is not None


def _source_color_convert(color_space: str | None) -> str | None:
    """非 BT.709 的 SDR 源要显式色彩转换（与实时链同一张表）。"""
    spec = _COLOR_CONVERT_INPUT.get(color_space or "")
    return f"colorspace={spec}:all=bt709:format=yuv420p" if spec else None


def _dolby_vision_treatment(source: dict) -> str | None:
    """这一单要不要靠杜比视界的动态元数据还原色彩（``dolby_vision="apply"``）。

    与播放端 ``decide._transcode_dolby_vision`` 同一口径：基础层能直接解读（P7/P8 带 HDR10
    基础层）吃不吃都行，交给滤镜默认；P5（以及 profile 未知）的基础层是 IPTPQc2，不按 RPU
    还原就是整片偏紫/发绿——这类必须 ``apply``，本机核显的硬件色调映射做不到。

    台账的 ``dv_bl_compatible`` 是关键：探测层没探到的老行是 NULL，按「不知道」保守算作
    不能直接解读（和播放端一致）。
    """
    hdr = source.get("hdr")
    if hdr != "Dolby Vision" and source.get("dv_profile") is None:
        return None
    if source.get("dv_bl_compatible"):
        return None  # 基础层本身是 HDR10，吃不吃 RPU 都对
    return "apply"


@dataclass(frozen=True)
class LocalHw:
    """NAS 本机的硬件能力快照（由 ``hwprobe`` 自检得出，装配层只读它，不跑 ffmpeg）。"""

    backend: str  # vaapi | qsv
    #: 本机真跑通过 HEVC 硬件编码的编码器名；None = 没有（HEVC 格式不能在本机执行）
    hevc_encoder: str | None = None
    #: 自检确认的原生 tone-map 滤镜表达式；None = 只能走 CPU 的 tonemapx
    native_tonemap: str | None = None
    #: 本机 ffmpeg 有 tonemapx（jellyfin-ffmpeg）：CPU 色调映射可用
    has_tonemapx: bool = False
    #: 自检确认、跟在编码器后面的参数（VAAPI 低功耗编码）
    encoder_opts: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class PreloadCommand:
    """预转码命令与元数据：argv 由执行者原样执行。"""

    argv: list[str]
    preset_id: str
    tone_map: bool
    video_copy: bool  # 源已低于档位：视频直拷不升档（§2）
    hw_backend: str | None  # nvenc | vaapi | qsv | None（软编）
    skipped_subtitle_indexes: list[int]  # 没留的字幕轨（0 基，源的字幕序）
    has_chapters: bool
    tracks: TrackPlan = field(default_factory=TrackPlan)
    #: 非空 = 这条命令装不出来（缺 tonemapx / 缺 HEVC 硬件编码器），领取方据此拒做并说明
    unusable_reason: str | None = None


def _gpu_filters(
    *,
    needs_tone_map: bool,
    needs_scale: bool,
    height_cap: int,
    color_space: str | None,
    has_tonemapx: bool,
    has_tonemap_cuda: bool,
) -> list[str]:
    """NVENC 链的滤镜序列（帧全程或大部分留在 GPU 上）。

    SDR：``scale_cuda``（输出 nv12 8-bit，NVENC 只吃这个）。
    HDR 有 ``tonemap_cuda``（jellyfin-ffmpeg）：色调映射也在 GPU 上；
    HDR 只有 tonemapx：下载回内存做软件色调映射，再回传 GPU 编码（慢路）。
    """
    if needs_tone_map and has_tonemap_cuda:
        parts = [
            "tonemap_cuda=tonemap=bt2390:desat=0:p=bt709:t=bt709:m=bt709:format=nv12"
        ]
        if needs_scale:
            parts.append(f"scale_cuda=-2:{height_cap}:format=nv12")
        return parts
    if needs_tone_map:
        parts = [f"hwdownload,format=p010le,{_SOFTWARE_TONEMAP}"]
        if needs_scale:
            parts.append(f"hwupload_cuda,scale_cuda=-2:{height_cap}:format=nv12")
        return parts
    parts: list[str] = []
    convert = _source_color_convert(color_space)
    if convert:
        # 色彩空间转换是 CPU 滤镜：下载 → 转换 → 回传
        parts.append(f"hwdownload,format={convert},hwupload_cuda")
    if needs_scale:
        parts.append(f"scale_cuda=-2:{height_cap}:format=nv12")
    elif not parts:
        # 不缩放不转换也要钉 8-bit：硬件帧直接给 NVENC 会吃到 10-bit P010
        parts.append("scale_cuda=format=nv12")
    return parts


#: NVENC 编码器随格式：HEVC 同流量清晰度更高，H.264 全设备兼容。
#: HEVC 的编码器名是 hevc_nvenc（无 h265_nvenc 别名），必须与 Worker 上报的 caps 名一致
_NVENC_ENCODER_BY_CODEC = {"h264": "h264_nvenc", "hevc": "hevc_nvenc"}


def _nvenc_video_args(preset: dict, filters: list[str]) -> list[str]:
    """NVENC 编码参数 + maxrate 约束。速度档 p2（用户盲测 p2/p4 无差别后拍板，真机实测
    快约 35%；p1 不再更快）+ spatial AQ 保暗场。"""
    codec = preset.get("video_codec", "h264")
    args: list[str] = []
    if filters:
        args += ["-vf", ",".join(filters)]
    args += ["-c:v", _NVENC_ENCODER_BY_CODEC.get(codec, "h264_nvenc")]
    if codec == "hevc":
        # HEVC 没有 high profile（main 起步）；level 交给驱动按分辨率定
        args += ["-profile:v", "main"]
    else:
        args += ["-profile:v", "high", "-level:v", "4.1"]
    args += [
        "-preset", "p2",
        "-b:v", "0",  # 交给 -maxrate 均值码率模式
        "-maxrate", preset["maxrate"],
        "-bufsize", preset["bufsize"],
        "-spatial_aq", "1",
    ]
    return args


def _x264_video_args(preset: dict, filters: list[str]) -> list[str]:
    """软编回退：质量优先参数。只有 H.264 走软编（HEVC 软编太慢，见模块说明）。"""
    args: list[str] = []
    if filters:
        args += ["-vf", ",".join(filters)]
    args += [
        "-c:v", "libx264",
        "-preset", "superfast",
        "-crf", "21",
        "-profile:v", "high",
        "-level:v", "4.1",
        "-pix_fmt", "yuv420p",
        "-maxrate", preset["maxrate"],
        "-bufsize", preset["bufsize"],
    ]
    return args


def _local_hw_video(
    *,
    preset: dict,
    local: LocalHw,
    needs_tone_map: bool,
    needs_scale: bool,
    color_space: str | None,
    dolby_vision: str | None = None,
) -> tuple[list[str], list[str], list[str], str | None, bool]:
    """NAS 本地的硬件链（QSV / VAAPI）。

    返回 (编码前的设备参数, 输入侧 -hwaccel 参数, 视频参数, 不可行原因, 是否纯硬件帧链)。

    解码留在 GPU（和播放同一套 ``-hwaccel`` 参数）。原生 tone-map 与缩放都在 GPU 上做；
    遇到只有 CPU 能做的步骤（色彩空间转换、没有原生滤镜的 HDR）时，解码帧下载回内存，
    CPU 滤镜处理完再 ``hwupload`` 回 GPU 编码——与 NVENC 的慢路同形。

    ``dolby_vision="apply"``（P5 这类基础层不能直接解读的片）本机不接：VAAPI/QSV 的原生
    tone-map 不认 RPU，硬转出来整片偏紫/偏绿（播放端同一条判定见 ``plan.py``）。
    """
    codec = preset.get("video_codec", "h264")
    if codec == "hevc" and local.hevc_encoder is None:
        return [], [], [], (
            "HEVC 格式需要本机的 HEVC 硬件编码器（QSV/VAAPI 自检未通过）；"
            "请交给带 NVENC 的 Worker，或在设置里改用 H.264"
        ), False
    if needs_tone_map and local.native_tonemap is None and not local.has_tonemapx:
        return [], [], [], (
            "HDR 源需要原生 tone-map 或 jellyfin-ffmpeg 的 tonemapx 滤镜，本机都没有"
        ), False
    if dolby_vision == "apply" and local.native_tonemap is not None:
        return [], [], [], (
            "这部片是杜比视界（基础层不能直接解读），需要按动态元数据还原色彩；"
            "本机核显的硬件色调映射不认杜比视界元数据，转出来会偏色"
        ), False
    backend: HwBackend = HW_BACKENDS[local.backend]
    encoder = local.hevc_encoder if codec == "hevc" else backend.encoder
    height = int(preset["height"])
    # 帧要不要下载回内存走 CPU 滤镜，只有两个来源：
    # ① 要 tone-map 但本机没有原生滤镜（只能用 tonemapx）；② SDR 源要做色彩空间转换。
    # **tone-map 在的时候不算 ②**：原生滤镜（tonemap_vaapi 的 t/m/p、vpp_qsv 的 out_color_*）
    # 本身就把画面落到 BT.709 了，再叠一个 colorspace 转换既多余又致命——它是软件滤镜，
    # 必须先把硬件帧 hwdownload 回来，整条链就从「GPU 直通」退化成 CPU 转码。
    # 真机 4K HDR10（BT.2020）因此只有 0.05× 实时（2026-10-20）；NVENC 那条链 10-14 就修过
    # 同一个坑（`_gpu_filters` 的 tone-map 分支不插 colorspace），本地 VAAPI 这条漏了。
    software = (needs_tone_map and local.native_tonemap is None) or (
        not needs_tone_map and bool(_source_color_convert(color_space))
    )

    hwaccel = ["-hwaccel", backend.hwaccel]
    if backend.device:
        hwaccel += ["-hwaccel_device", backend.device]
    pre_input: list[str] = []
    filters: list[str] = []
    if not software:
        # 整链留在 GPU：输出硬件帧（tone-map 与缩放都是 GPU 滤镜）
        hwaccel += ["-hwaccel_output_format", backend.hwaccel_output_format or backend.hwaccel]
        if needs_tone_map:
            filters.append(local.native_tonemap or "")
        if needs_scale or needs_tone_map:
            scale = f"{backend.scale_filter}=w=-2:h={height}:format=nv12" if needs_scale else ""
            if scale:
                filters.append(scale)
        if not needs_tone_map and not needs_scale:
            filters.append(f"{backend.scale_filter}=format=nv12")  # 钉死 8-bit
        filters = [f for f in filters if f]
    else:
        # 软件段：解码帧下载回内存（不发 -hwaccel_output_format），CPU 滤镜后再上传
        if backend.name == "vaapi":
            pre_input = ["-vaapi_device", backend.device or _VAAPI_DEVICE]
            upload = "hwupload"
        else:
            pre_input = ["-init_hw_device", "qsv=hw", "-filter_hw_device", "hw"]
            upload = "hwupload=extra_hw_frames=64"
        convert = _source_color_convert(color_space)
        if convert:
            filters.append(convert)
        if needs_tone_map:
            filters.append(_SOFTWARE_TONEMAP)
        if needs_scale:
            filters.append(f"scale=-2:{height}")
        filters += ["format=nv12", upload]

    args: list[str] = []
    if filters:
        args += ["-vf", ",".join(filters)]
    args += ["-c:v", encoder or backend.encoder, *local.encoder_opts]
    if codec == "hevc":
        args += ["-profile:v", "main"]
    else:
        args += ["-profile:v", "high", "-level:v", "4.1"]
    args += ["-b:v", "0", "-maxrate", preset["maxrate"], "-bufsize", preset["bufsize"]]
    # 纯硬件帧链（帧从不落回内存）不能在输出侧写色彩三件套/range：ffmpeg 7.1 在首帧发现源
    # 色彩属性与声明不符时重建滤镜图，重建插进来的软件 auto_scale 接不上硬件帧，整链以
    # 「Impossible to convert … / Function not implemented」当场崩（真机 4K DV P5 复现，
    # 与实时链 cbf8475d 同一根因）。原生 tone-map 已在滤镜内声明输出色彩，标签不受影响
    return pre_input, hwaccel, args, None, not software


def build_preload_command(
    *,
    source_path: str,
    output_path: str,
    preset: dict,
    source: dict,
    tracks: TrackPlan | None = None,
    hw_backend: str | None = None,
    worker_caps: dict | None = None,
    local: LocalHw | None = None,
) -> PreloadCommand:
    """构造预转码 ffmpeg 命令（视频与音频；字幕由拼接时的 :func:`final_mux_argv` 内封）。

    ``source`` 是源文件的台账规格（``LibraryFile`` 的探测列 dict）。
    ``tracks`` 是 :func:`plan_tracks`
    的结果（不给时：第一条音轨 + 全部文本字幕，只用于没有上下文的兜底）。

    执行者二选一：``local``（NAS 本机的 QSV/VAAPI 能力）或 ``worker_caps``（Worker 上报的
    NVENC 能力，``hw_backend="nvenc"``）；都没有就 libx264 软编（只限 H.264）。
    """
    height_cap = preset["height"]
    caps = worker_caps or {}
    encoders = {str(e).lower() for e in (caps.get("encoders") or [])}
    filters_available = {str(f).lower() for f in (caps.get("filters") or [])}

    audio_streams = source.get("audio_streams") or []
    subtitle_streams = source.get("subtitle_streams") or []
    if tracks is None:
        tracks = TrackPlan(
            audio=(0,) if audio_streams else (),
            subtitles=tuple(
                i for i, t in enumerate(subtitle_streams)
                if (t.get("codec") or "").lower() in TEXT_SUBTITLE_CODECS
            ),
        )

    # -- 视频链 --------------------------------------------------------------
    src_codec = (source.get("video_codec") or "").lower()
    src_height = _height_of(source.get("resolution"))
    needs_tone_map = _source_needs_tone_map(source.get("hdr"), source.get("dv_profile"))
    # 直拷只属于高清档（§2：源 ≤1080p 且已是 H.264/SDR = 已经是目标档）。
    # 省流档永远重编码：台账没有源视频码率，判断不了"已经够小"，而省流的全部意义在体积上限
    video_copy = (
        preset.get("video_codec", "h264") == "h264"
        and src_codec == "h264"
        and src_height is not None
        and 0 < src_height <= height_cap
        and not needs_tone_map
    )
    needs_scale = src_height is None or src_height > height_cap
    color_space = source.get("color_space")

    pre_input: list[str] = []
    input_args: list[str] = []
    video_args: list[str]
    hw_backend_used: str | None = None
    unusable_reason: str | None = None
    hw_frames = False
    if video_copy:
        video_args = ["-c:v", "copy"]
    elif local is not None and hw_backend is None:
        pre_input, input_args, video_args, unusable_reason, hw_frames = _local_hw_video(
            preset=preset, local=local, needs_tone_map=needs_tone_map,
            needs_scale=needs_scale, color_space=color_space,
            dolby_vision=_dolby_vision_treatment(source),
        )
        hw_backend_used = None if unusable_reason else local.backend
    else:
        # Worker 路径（NVENC）：能力缺一个就整链软编
        preset_encoder = _NVENC_ENCODER_BY_CODEC.get(
            preset.get("video_codec", "h264"), "h264_nvenc"
        )
        use_nvenc = (
            hw_backend == "nvenc"
            and preset_encoder in encoders
            and "scale_cuda" in filters_available
        )
        has_tonemapx = "tonemapx" in filters_available
        # HDR 色调映射的最低门槛是 tonemapx（jellyfin-ffmpeg）：上游 ffmpeg 的 zimg 没有
        # PQ→linear 路径，拒绝比转出一部偏色的片子好
        if needs_tone_map and not has_tonemapx and "tonemap_cuda" not in filters_available:
            unusable_reason = (
                "HDR 源需要 jellyfin-ffmpeg（tonemapx 滤镜）做色调映射，"
                "当前 Worker 的 ffmpeg 不含它；请给 Worker 换 jellyfin-ffmpeg "
                "的 Windows build 后重试"
            )
        elif preset.get("video_codec") == "hevc" and not use_nvenc:
            unusable_reason = (
                "HEVC 格式需要 NVENC 硬件编码（hevc_nvenc + scale_cuda），当前执行端没有；"
                "请交给带 NVENC 的 Worker，或在设置里改用 H.264"
            )
        if unusable_reason is None:
            if use_nvenc:
                has_tonemap_cuda = "tonemap_cuda" in filters_available
                filters = _gpu_filters(
                    needs_tone_map=needs_tone_map,
                    needs_scale=needs_scale,
                    height_cap=height_cap,
                    color_space=color_space,
                    has_tonemapx=has_tonemapx,
                    has_tonemap_cuda=has_tonemap_cuda,
                )
                # 需要下载回内存做 CPU 滤镜（HDR 走软件 tonemapx）时，硬解也要能 hwdownload；
                # tonemap_cuda 全 GPU 链不需要下载。BT.2020 色彩转换只在 SDR 源才轮得到
                needs_download = needs_tone_map and not has_tonemap_cuda
                if not needs_tone_map:
                    needs_download = needs_download or bool(
                        _source_color_convert(color_space)
                    )
                hw_decodable = bool(caps.get("hw_decoders")) and src_codec in caps["hw_decoders"]
                if hw_decodable and not needs_download:
                    input_args = ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
                elif hw_decodable:
                    input_args = ["-hwaccel", "cuda"]
                video_args = _nvenc_video_args(preset, filters)
                hw_backend_used = "nvenc"
            else:
                filters = []
                if needs_tone_map:
                    filters.append(_SOFTWARE_TONEMAP)
                else:
                    convert = _source_color_convert(color_space)
                    if convert:
                        filters.append(convert)
                if needs_scale:
                    filters.append(f"scale=-2:{height_cap}")
                if filters:
                    filters.append("format=yuv420p")
                video_args = _x264_video_args(preset, filters)
        else:
            video_args = []

    # -- 音轨（§5：默认轨 [+ 原声]，统一立体声 AAC）----------------------------
    audio_maps: list[str] = []
    audio_args: list[str] = []
    for out_index, src_index in enumerate(tracks.audio):
        audio_maps += ["-map", f"0:a:{src_index}"]
        audio_args += [
            f"-c:a:{out_index}", "aac",
            f"-b:a:{out_index}", preset["audio_bitrate"],
            f"-ac:a:{out_index}", str(AUDIO_CHANNELS),
            f"-profile:a:{out_index}", "aac_low",
        ]

    # -- 装配 ----------------------------------------------------------------
    argv: list[str] = ["-hide_banner", "-nostdin", "-y", *pre_input, *input_args]
    # 源是执行者能读到的文件（Worker 落盘的临时名 / NAS 原文件）
    argv += ["-i", source_path]
    argv += ["-map", "0:v:0", *audio_maps]
    if tracks.audio:
        # -af 全局作用于所有音频流：聚拢时间戳，分段的起点不留缝
        argv += ["-af", "aresample=async=1"]
    argv += audio_args
    # 分段只含视频与音频：字幕与章节在拼接时从源文件一次拷入（见模块说明）
    argv += ["-sn", "-map_chapters", "-1", "-map_metadata", "-1"]
    argv += video_args
    if (
        not video_copy
        and unusable_reason is None
        and not hw_frames  # 纯硬件帧链不写输出侧色彩标签，理由见 _local_hw_video 末尾
        and (
            needs_tone_map
            or color_space == "BT.709"
            or color_space in _COLOR_CONVERT_INPUT
        )
    ):
        argv += _BT709_OUTPUT_TAGS
    argv += ["-f", "matroska", output_path]

    skipped = [
        i for i in range(len(subtitle_streams)) if i not in set(tracks.subtitles)
    ]
    return PreloadCommand(
        argv=argv if unusable_reason is None else [],
        preset_id=preset["id"],
        tone_map=needs_tone_map,
        video_copy=video_copy,
        hw_backend=hw_backend_used,
        skipped_subtitle_indexes=skipped,
        has_chapters=bool(source.get("chapters")),
        tracks=tracks,
        unusable_reason=unusable_reason,
    )


def segment_total(duration_seconds: int | None, segment_seconds: int) -> int:
    """按分段时长切成几段（至少一段；时长未知或不分段时为一段）。"""
    if not segment_seconds or not duration_seconds or duration_seconds <= 0:
        return 1
    return max(1, math.ceil(duration_seconds / segment_seconds))


def segment_argv(
    base: list[str], *, part: int, total: int, seconds: int, source: str, output: str
) -> list[str]:
    """第 ``part`` 段的命令：在整片命令上换源、换输出，并切时长。

    与 Worker 的分段完全同形（worker/worker.py）：``-ss`` 插在 ``-i`` 前（快速寻址），
    ``-t`` 插在输入路径之后（输出侧）——输入侧 ``-t`` 切不动字幕包，所以字幕不走分段。
    末段不加 ``-t``，转到 EOF。
    """
    argv = list(base)
    argv[-1] = output
    input_index = argv.index("-i")
    argv[input_index + 1] = source
    if seconds:
        if part > 0:
            argv[input_index:input_index] = ["-ss", str(part * seconds)]
            input_index += 2
        if part < total - 1:
            argv[input_index + 2:input_index + 2] = ["-t", str(seconds)]
    return argv


def final_mux_argv(
    *, concat_list: str, source_path: str, output: str, subtitle_indexes: tuple[int, ...]
) -> list[str]:
    """拼接 + 字幕内封（一趟 ``-c copy``）：视频/音频来自拼接清单，字幕与章节取自源文件。

    字幕在源文件上的时间轴与分段拼接后的视频一致，所以不会重复、不会错位（真机实测）。
    """
    argv = [
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", concat_list,
        "-i", source_path,
        "-map", "0",
    ]
    for index in subtitle_indexes:
        argv += ["-map", f"1:s:{index}"]
    argv += ["-c", "copy", "-map_chapters", "1", "-map_metadata", "-1", output]
    return argv


def estimate_size_bytes(
    *,
    duration_seconds: int | None,
    preset: dict,
    audio_tracks: int = 1,
) -> int:
    """体积预估（§2）：视频按档位码率上限（保守值）+ 实际保留的音轨数 × 音频码率。

    字幕体积可忽略（文本几 KB，PGS 每条几 KB）。
    """
    if not duration_seconds or duration_seconds <= 0:
        return 0
    total_bps = _bitrate_bps(preset["maxrate"]) + max(audio_tracks, 0) * _bitrate_bps(
        preset["audio_bitrate"]
    )
    return int(duration_seconds * total_bps / 8)


def _bitrate_bps(spec: str) -> int:
    """ffmpeg 码率字面量（"1.5M"/"128k"）→ bps。"""
    text = spec.strip().lower()
    if text.endswith("k"):
        return int(float(text[:-1]) * 1_000)
    if text.endswith("m"):
        return int(float(text[:-1]) * 1_000_000)
    return int(float(text))


def _height_of(resolution: str | None) -> int | None:
    """台账分辨率标签（2160p/1080p/…）→ 行高；未知返回 None。"""
    if not resolution or not resolution.endswith("p"):
        return None
    try:
        return int(resolution[:-1])
    except ValueError:
        return None
