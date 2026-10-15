"""硬件后端熔断登记。

决策层选后端靠的是能力清单（ffmpeg 构建、设备节点、滤镜兼容性），它回答的是
「能不能跑」，不是「跑没跑成」。本地转码进程起步即死（滤镜链在真机驱动上装
不起来，2026-10-15 的 VAAPI 4K HDR10 事件：输出侧色彩标签触发 ffmpeg 7.1 重建
滤镜图、软件 auto_scale 接不上硬件帧）时在这里记一笔，让决策层在一段时间内
**优先跳过**它，给在线的远程 Worker 让位。

语义是「降级」不是「拉黑」：没有任何替代后端时，决策层仍会用被熔断的后端——
宁可再试一次坏后端，也不把能放片子的档直接判死。TTL 过期、重新硬件探测成功
（拿到的是新证据）都会解除。
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

#: 起步即死的后端被降级的时长。要够播放器走完一次自动重试，也不至于把一次
#: 偶发失败（磁盘瞬时卡顿之类）放大成十分钟都用不了。
DEMOTE_SECONDS = 600.0

_demoted_at: dict[str, float] = {}
_lock = threading.Lock()


def demote(name: str, *, reason: str = "") -> None:
    """记录一次起步即死，name 在 TTL 内被决策层降级。"""
    with _lock:
        _demoted_at[name] = time.monotonic()
    suffix = f"：{reason}" if reason else ""
    logger.warning("硬件后端 %s 起步失败被降级 %.0f 分钟%s", name, DEMOTE_SECONDS / 60, suffix)


def is_demoted(name: str) -> bool:
    demoted_at = _demoted_at.get(name)
    return demoted_at is not None and time.monotonic() - demoted_at < DEMOTE_SECONDS


def demoted_backends() -> frozenset[str]:
    """当前处于降级期的后端名（过期条目顺手清理）。"""
    now = time.monotonic()
    expired: list[str] = []
    with _lock:
        for name, demoted_at in _demoted_at.items():
            if now - demoted_at >= DEMOTE_SECONDS:
                expired.append(name)
        for name in expired:
            del _demoted_at[name]
        return frozenset(_demoted_at)


def reset(name: str | None = None) -> None:
    """清掉降级记录；``name=None`` 全清。重新硬件探测成功后调用。"""
    with _lock:
        if name is None:
            _demoted_at.clear()
        else:
            _demoted_at.pop(name, None)
