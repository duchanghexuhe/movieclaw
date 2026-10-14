"""预转码的 ffmpeg 参数构造（docs/design/preload-transcode.md §2/§4/§5）。

与实时转码（``ffmpeg_args.build_hls_command``）共用 HDR 色调映射与色彩转换的
**思路**，输出侧完全不同：

- 实时转码：HLS 分片 + 码率阶梯（受播放实时性约束）；
- 预转码：整文件 MKV + 质量优先参数——高功耗 Windows 专门开机干活，
  **越快转完越省电**（用户拍板），所以有 NVENC 就用 NVENC 全速跑。

硬件优先（用户拍板：全功率越快越好）：
- Worker 上报本机能力（``claim`` 携带 ``encoders``/``gpu_filters``）；
- SDR 源 + NVENC：硬解 → ``scale_cuda`` → NVENC 整链留 GPU（编码器随档位：
  省流= ``hevc_nvenc``，高清= ``h264_nvenc``）；
- HDR 源 + NVENC：硬解 → 下载回内存 → 软件 ``tonemap``（上游 ffmpeg 没有
  jellyfin 的 ``tonemapx``/``tonemap_cuda``，色调映射只能 CPU；用 hable
  算法——上游 ``tonemap`` 的可选算法里高光滚降最接近 BT.2390 的观感）→
  回传 GPU → NVENC。色调映射只是一趟滤镜，编码大头仍在 GPU；
- 无 NVENC / 探测失败：libx264 软编回退，参数与原第一版一致。

字幕与音轨（§5）：全部音轨内封（AAC 转码 / 原轨 AAC 直拷），文本字幕全部
内封；图形字幕（PGS/VobSub）第一版跳过，任务结果里说明。
"""

from __future__ import annotations

from dataclasses import dataclass

from movieclaw_api.services.playback.ffmpeg_args import (
    _BT709_OUTPUT_TAGS,
    _COLOR_CONVERT_INPUT,
)

#: 档位注册表（§2「档位是接口」）：加档只是新增枚举。省流档是用户拍板的
#: 默认（预转码产物只给手机外网看，家里电视看原画）——HEVC 同画质省一半
#: 流量，一部 2 小时电影约 1.5 GB；高清档留给局域网大屏。
PRESET_SAVER_1080P = "saver_1080p"
PRESET_PORTABLE_1080P = "portable_1080p"
PRESET_DEFAULT = PRESET_SAVER_1080P

PRESETS: dict[str, dict] = {
    PRESET_SAVER_1080P: {
        "short": "省流",
        "title": "省流 1080p（HEVC · 视频上限 1.5 Mbps · 手机/外网）",
        "height": 1080,
        "maxrate": "1.5M",
        "bufsize": "3M",
        "audio_bitrate": "192k",
        "video_codec": "hevc",
    },
    PRESET_PORTABLE_1080P: {
        "short": "高清",
        "title": "高清 1080p（H.264 · 视频上限 4 Mbps · 局域网/大屏）",
        "height": 1080,
        "maxrate": "4M",
        "bufsize": "8M",
        "audio_bitrate": "256k",
        "video_codec": "h264",
    },
}

#: 断点续转的产物分段大小（§7）：64 MiB 一段，上传失败只重传一段。
PRELOAD_PART_BYTES = 64 * 1024 * 1024

#: 图形字幕编码名集合（第一版跳过，不进产物）
GRAPHIC_SUBTITLE_CODECS = frozenset({"hdmv_pgs_subtitle", "dvd_subtitle", "dvdsub", "pgssub"})

#: jellyfin-ffmpeg 的 tonemapx（HDR 色调映射的唯一软件路径；上游 ffmpeg
#: 的 zimg 缺 PQ→linear 路径，tonemap 滤镜到不了——见 build_preload_command）
_SW_TONEMAP_JELLYFIN = (
    "tonemapx=tonemap=bt2390:desat=0:p=bt709:t=bt709:m=bt709:format=yuv420p"
)


@dataclass(frozen=True)
class PreloadCommand:
    """预转码命令与元数据：argv 由 Worker 原样执行。"""

    argv: list[str]
    preset_id: str
    tone_map: bool
    video_copy: bool  # 源已低于档位：视频直拷不升档（§2）
    hw_backend: str | None  # nvenc | None（软编）
    skipped_subtitle_indexes: list[int]  # 跳过的图形字幕轨（0 基，ffmpeg 流序）
    has_chapters: bool
    #: 非空 = 这条命令装不出来（HDR 源 + Worker 无 tonemapx），领取方据此拒做并说明
    unusable_reason: str | None = None


def _source_needs_tone_map(hdr: str | None, dv_profile: int | None) -> bool:
    """HDR10 / HLG / 杜比视界（按 HDR10 兼容层处理）都要色调映射到 SDR（§4）。"""
    return bool(hdr) or dv_profile is not None


def _source_color_convert(color_space: str | None) -> str | None:
    """非 BT.709 的 SDR 源要显式色彩转换（与实时链同一张表）。"""
    spec = _COLOR_CONVERT_INPUT.get(color_space or "")
    return f"colorspace={spec}:all=bt709:format=yuv420p" if spec else None


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
    HDR 有 ``tonemap_cuda``（jellyfin-ffmpeg）：色调映射也在 GPU 上
    （参数与软件档同源，BT.2390 + DV 元数据由滤镜的 apply_dovi 还原），
    整条链硬解→映射→缩放→编码帧不出 GPU——真机实测 4K DV 源 14× 实时，
    比回内存软件映射快 5 倍以上（2026-10-14，RTX 2070 SUPER）。
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
        parts = [f"hwdownload,format=p010le,{_SW_TONEMAP_JELLYFIN}"]
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


#: NVENC 编码器随档位：省流= HEVC（同画质省一半流量），高清= H.264。
#: HEVC 的编码器名是 hevc_nvenc（2026-10-14 jf-ffmpeg 8.1.3 实测 -encoders
#: 只此一名，无 h265_nvenc 别名），必须与 Worker 上报的 caps 名一致
_NVENC_ENCODER_BY_CODEC = {"h264": "h264_nvenc", "hevc": "hevc_nvenc"}


def _nvenc_video_args(preset: dict, filters: list[str]) -> list[str]:
    """NVENC 编码参数：p4 质量档 + maxrate 约束（NVENC 不认 CRF 模式下的
    恒质量……实际上支持 -cq，但与 maxrate 混用的行为随驱动版本漂；用
    纯码率模式最稳，速度是第一目标）。编码器与 profile 随档位。"""
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
        "-preset", "p4",
        "-b:v", "0",  # 交给 -maxrate 均值码率模式
        "-maxrate", preset["maxrate"],
        "-bufsize", preset["bufsize"],
        "-spatial_aq", "1",
    ]
    return args


def _x264_video_args(preset: dict, filters: list[str]) -> list[str]:
    """软编回退：质量优先参数（无 NVENC 的机器仍能干活）。两个档位都用
    libx264——libx265 软编慢一个量级（一部电影要转大半天），回退场景要的
    是"能出活"，体积目标靠档位的 maxrate 帽兜住。"""
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


def build_preload_command(
    *,
    source_path: str,
    output_path: str,
    preset_id: str = PRESET_DEFAULT,
    source: dict,
    hw_backend: str | None = None,
    worker_caps: dict | None = None,
) -> PreloadCommand:
    """构造预转码 ffmpeg 命令。

    ``source`` 是源文件的台账规格（``LibraryFile`` 的探测列 dict）：
    resolution/video_codec/hdr/dv_profile/color_space/bit_depth/duration_seconds/
    audio_streams/subtitle_streams。缺探测值时按最保守假设处理。

    ``hw_backend``：``"nvenc"``（Worker 上报有 h264_nvenc）或 ``None``（软编）。
    ``worker_caps``：Worker 的能力集（``encoders``/``gpu_filters``/``filters``），
    用于判断 scale_cuda 与 tonemapx 可用性。

    档位是**直拷判定的产品语义**（§2）：源 ≤1080p 且已是 H.264/SDR 时直拷。
    """
    preset = PRESETS[preset_id]
    height_cap = preset["height"]
    caps = worker_caps or {}
    encoders = {str(e).lower() for e in (caps.get("encoders") or [])}
    filters_available = {str(f).lower() for f in (caps.get("filters") or [])}

    audio_streams = source.get("audio_streams") or []
    subtitle_streams = source.get("subtitle_streams") or []

    # -- 视频链 --------------------------------------------------------------
    src_codec = (source.get("video_codec") or "").lower()
    src_height = _height_of(source.get("resolution"))
    needs_tone_map = _source_needs_tone_map(source.get("hdr"), source.get("dv_profile"))
    # 直拷只属于高清档（§2：源 ≤1080p 且已是 H.264/SDR = 已经是目标档）。
    # 省流档永远重编码：台账没有源视频码率，判断不了"已经够小"，而省流的
    # 全部意义在体积上限，宁可多转一次
    video_copy = (
        preset.get("video_codec", "h264") == "h264"
        and src_codec == "h264"
        and src_height is not None
        and 0 < src_height <= height_cap
        and not needs_tone_map
    )

    # 硬编可用性：档位要的 NVENC 编码器 + scale_cuda 都在才算全 GPU 链可用
    # （缺 scale_cuda 时 4K 源没法在 GPU 上缩放，链会断）
    preset_encoder = _NVENC_ENCODER_BY_CODEC.get(preset.get("video_codec", "h264"), "h264_nvenc")
    use_nvenc = (
        hw_backend == "nvenc"
        and preset_encoder in encoders
        and "scale_cuda" in filters_available
    )
    has_tonemapx = "tonemapx" in filters_available
    # HDR 色调映射的最低门槛是 tonemapx（jellyfin-ffmpeg）：上游 ffmpeg 的
    # zimg 没有编进 PQ(HDR10/杜比视界)→linear 的转换路径（zscale 报
    # "no path between colorspaces"，2026-10-14 真机实测），上游 tonemap
    # 滤镜又只吃 linear 输入——没有 tonemapx 时 HDR 源装不出任何正确的链。
    # 拒绝比转出一部偏色的片子好：错误信息直接告诉用户换 build。
    unusable_reason: str | None = None
    if needs_tone_map and not has_tonemapx and "tonemap_cuda" not in filters_available:
        unusable_reason = (
            "HDR 源需要 jellyfin-ffmpeg（tonemapx 滤镜）做色调映射，"
            "当前 Worker 的 ffmpeg 不含它；请给 Worker 换 jellyfin-ffmpeg "
            "的 Windows build 后重试"
        )
    # 硬解可用性：解码器在 Worker 上报的 hw_decoders 里（缺省视为可硬解——
    # ffmpeg 自己会回退软解，不作为整条链的开关）
    hw_decodable = bool(caps.get("hw_decoders")) and src_codec in caps["hw_decoders"]

    if unusable_reason:
        return PreloadCommand(
            argv=[], preset_id=preset_id, tone_map=needs_tone_map, video_copy=False,
            hw_backend=None, skipped_subtitle_indexes=[], has_chapters=False,
            unusable_reason=unusable_reason,
        )

    input_args: list[str] = []
    if not video_copy:
        if use_nvenc:
            needs_scale = src_height is None or src_height > height_cap
            has_tonemap_cuda = "tonemap_cuda" in filters_available
            filters = _gpu_filters(
                needs_tone_map=needs_tone_map,
                needs_scale=needs_scale,
                height_cap=height_cap,
                color_space=source.get("color_space"),
                has_tonemapx=has_tonemapx,
                has_tonemap_cuda=has_tonemap_cuda,
            )
            # 需要下载回内存做 CPU 滤镜（HDR 走软件 tonemapx）时，硬解帧也要能
            # hwdownload——用 nv12/p010 硬解输出格式；tonemap_cuda 全 GPU 链不
            # 需要下载。BT.2020 色彩转换只在 SDR 源才轮得到：HDR 源的色调映射
            # 滤镜（tonemap_cuda/tonemapx）本身就把画面转到 BT.709，再挂
            # colorspace 是重复转换；若因它强制下载，全 GPU 滤镜链会吃到系统
            # 内存帧直接报错（真机 -40，2026-10-14 冒烟实测）
            needs_download = needs_tone_map and not has_tonemap_cuda
            if not needs_tone_map:
                needs_download = needs_download or bool(
                    _source_color_convert(source.get("color_space"))
                )
            if hw_decodable and not needs_download:
                # 整链留 GPU：硬解输出 cuda 帧
                input_args = ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
            elif hw_decodable:
                # 硬解但后面要下载：不用显式 hwaccel_output_format，ffmpeg 自行协商
                input_args = ["-hwaccel", "cuda"]
            video_args = _nvenc_video_args(preset, filters)
            hw_backend_used: str | None = "nvenc"
        else:
            filters: list[str] = []
            if needs_tone_map:
                # 到这里必然 has_tonemapx（无 tonemapx 已在上面拒掉）
                filters.append(_SW_TONEMAP_JELLYFIN)
            else:
                convert = _source_color_convert(source.get("color_space"))
                if convert:
                    filters.append(convert)
            if src_height is None or src_height > height_cap:
                filters.append(f"scale=-2:{height_cap}")
            if filters:
                filters.append("format=yuv420p")
            video_args = _x264_video_args(preset, filters)
            hw_backend_used = None
    else:
        video_args = ["-c:v", "copy"]
        hw_backend_used = None

    # -- 音轨（§5：全部内封；AAC 原轨直拷，其余 AAC 转码）--------------------
    audio_args: list[str] = []
    for i, track in enumerate(audio_streams):
        codec = (track.get("codec") or "").lower()
        if codec == "aac":
            audio_args += [f"-c:a:{i}", "copy"]
        else:
            audio_args += [
                f"-c:a:{i}", "aac",
                f"-b:a:{i}", preset["audio_bitrate"],
                f"-profile:a:{i}", "aac_low",
            ]

    # -- 字幕（§5：文本内封，图形跳过）--------------------------------------
    text_subtitle_indexes = [
        i for i, t in enumerate(subtitle_streams)
        if (t.get("codec") or "").lower() not in GRAPHIC_SUBTITLE_CODECS
    ]
    skipped_subtitle_indexes = [
        i for i, t in enumerate(subtitle_streams)
        if (t.get("codec") or "").lower() in GRAPHIC_SUBTITLE_CODECS
    ]

    # -- 装配 ----------------------------------------------------------------
    argv: list[str] = ["-hide_banner", "-nostdin", "-y"]
    argv += input_args
    # 源是 Worker 本地落盘的临时文件（服务端签名 URL 下载而来）
    argv += ["-i", source_path]
    argv += ["-map", "0:v:0"]
    if audio_streams:
        argv += ["-map", "0:a?", "-map", "0:s?"]
        argv += audio_args
        # -af 全局作用于所有音频流，直拷轨吃不了滤镜；有任一轨要转码才挂
        if any((t.get("codec") or "").lower() != "aac" for t in audio_streams):
            argv += ["-af", "aresample=async=1"]
        if text_subtitle_indexes:
            argv += ["-c:s", "srt"]
    # 图形字幕显式排除（-map 0:s? 会把它们也拖进来；第一版口径是"不含该轨
    # 并在任务结果里说明"，保持产物与声明一致）
    for index in skipped_subtitle_indexes:
        argv += ["-map", f"-0:s:{index}"]
    if not text_subtitle_indexes:
        argv += ["-sn"]

    argv += video_args
    if not video_copy and (
        needs_tone_map
        or source.get("color_space") == "BT.709"
        or source.get("color_space") in _COLOR_CONVERT_INPUT
    ):
        argv += _BT709_OUTPUT_TAGS

    # 章节随源内封（mkv 原生支持）
    argv += ["-map_chapters", "0"]
    # 元数据不外泄：把源文件的标题轨清掉（压制组常写奇怪的字段）
    argv += ["-map_metadata", "-1"]

    argv += ["-f", "matroska", output_path]

    return PreloadCommand(
        argv=argv,
        preset_id=preset_id,
        tone_map=needs_tone_map,
        video_copy=video_copy,
        hw_backend=hw_backend_used,
        skipped_subtitle_indexes=skipped_subtitle_indexes,
        has_chapters=bool(source.get("chapters")),
    )


def estimate_size_bytes(
    *,
    duration_seconds: int | None,
    preset: dict,
) -> int:
    """体积预估（§2）：码率 × 时长 + 音轨/字幕开销（±20% 内即可）。

    视频按档位码率上限算保守值，音频按档位码率算一轨，字幕每轨忽略不计。
    """
    if not duration_seconds or duration_seconds <= 0:
        return 0
    total_bps = _bitrate_bps(preset["maxrate"]) + _bitrate_bps(preset["audio_bitrate"])
    return int(duration_seconds * total_bps / 8)


def _bitrate_bps(spec: str) -> int:
    """ffmpeg 码率字面量（"1.5M"/"256k"）→ bps。"""
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
