"""预转码的持久化配置域（docs/design/preload-transcode.md §6）。"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from movieclaw_api.settings.base import SettingSchema, register_setting

#: 总配额默认值：磁盘 15% 与 200 GB 取较小——「15%」要先知道磁盘大小，
#: 配置存的是**数字**，磁盘自适应在服务层读取时计算（estimate_quota）。
DEFAULT_PRELOAD_QUOTA_BYTES = 200 * 1024**3
#: 冷数据阈值默认：90 天未播可清（§6 三件套之二/三）
DEFAULT_PRELOAD_STALE_DAYS = 90


@register_setting(namespace="library.preload_transcode", title="预转码")
class PreloadSetting(SettingSchema):
    """预转码的网页配置。

    Worker 凭证同样是逐台设备配对签发（device-auth.md），这里没有令牌字段。
    服务端不设「启用」开关：配对 Worker 与下单都是用户的显式动作，配额又
    逐单兜底——没有订单时队列什么都不做，一个拦派发的开关只会制造
    「任务永远排队中却无人派发」的死状态（没有 UI 出口可打开它）。
    """

    quota_bytes: int = Field(
        default=DEFAULT_PRELOAD_QUOTA_BYTES,
        gt=0,
        description="预转码产物总配额（字节）；下单配额预检的兜底上限",
    )
    stale_days: int = Field(
        default=DEFAULT_PRELOAD_STALE_DAYS,
        ge=1,
        description="「N 天未播」冷数据阈值；手动清理与自动清理共用",
    )
    auto_cleanup: bool = Field(
        default=False, description="自动清理：开启后定期删除超过 N 天未播的预转码产物"
    )
    video_format: Literal["h264", "hevc"] = Field(
        default="h264",
        description=(
            "预转码编码格式：h264 全设备兼容（省流 480p / 高清 720p，与播放页"
            "清晰度同值）；hevc 同流量下清晰度更高（省流 1080p / 高清 1080p·4M），"
            "但不支持的设备会回退服务器实时转码"
        ),
    )


__all__ = [
    "DEFAULT_PRELOAD_QUOTA_BYTES",
    "DEFAULT_PRELOAD_STALE_DAYS",
    "PreloadSetting",
]
