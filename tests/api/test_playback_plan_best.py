"""起播多候选择优（plan._best）的并列裁决。

预转码 HEVC 格式的省流/高清两档同为 1080p：两产物并存且都直通（同档同高）
时，选择必须确定且质量优先——取码率更高的高清档，不能落在数据库返回顺序上。
"""

from __future__ import annotations

from movieclaw_api.services.playback.plan import _best
from movieclaw_playback.decide import AudioPlan, PlaybackPlan, PlaybackTier, VideoPlan


def _plan(file_id: int, tier: PlaybackTier = PlaybackTier.DIRECT_PLAY) -> PlaybackPlan:
    return PlaybackPlan(
        tier=tier,
        file_id=file_id,
        container="mp4",
        video=VideoPlan(action="copy"),
        audio=AudioPlan(action="copy"),
    )


def test_same_tier_same_height_prefers_higher_bitrate():
    """同档同高：取码率更高的候选（HEVC 部署省流 1.5M vs 高清 4M → 高清）。"""
    saver = (_plan(1), 1080, 1_500_000)
    premium = (_plan(2), 1080, 4_000_000)
    assert _best([saver, premium]).file_id == 2
    assert _best([premium, saver]).file_id == 2  # 与候选顺序无关


def test_unknown_bitrate_loses_to_known_at_same_height():
    """码率未知（None）的候选在同高并列时排后——结果至少确定。"""
    unknown = (_plan(1), 1080, None)
    known = (_plan(2), 1080, 4_000_000)
    assert _best([unknown, known]).file_id == 2
    assert _best([known, unknown]).file_id == 2


def test_height_still_dominates_bitrate():
    """高度优先级高于码率：2160p·低码率 胜过 1080p·高码率（原有语义不变）。"""
    taller = (_plan(1), 2160, 1_500_000)
    shorter = (_plan(2), 1080, 4_000_000)
    assert _best([shorter, taller]).file_id == 1
