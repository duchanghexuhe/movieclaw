"""播放决策服务层测试（docs/design/web-player.md §3）。

判定逻辑本身由 ``tests/playback/test_decide.py`` 表驱动覆盖；这里只测服务层
真正负责的三件事：**多候选择优、可见库过滤、配置读取**——以及三态结果到
响应模型的翻译。
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from movieclaw_api.core.config import get_settings
from movieclaw_api.schemas.playback import ClientCapabilityIn
from movieclaw_api.services.playback import plan as playback_plan
from movieclaw_api.settings import PlaybackPolicySetting
from movieclaw_api.settings.store import (
    get_setting_store,
    init_setting_store,
    reset_setting_store,
)
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import FileSource, FileState, LibraryFile, MediaItem
from movieclaw_db.repositories.library_repo import LibraryRepository
from movieclaw_playback.capability import AudioSupport, ClientCapability, VideoSupport
from movieclaw_playback.decide import ConsentRequired, PlaybackPlan, PlaybackTier

# 只能解 H.264 + AAC 的浏览器：HEVC 一律要转码
CHROME = ClientCapability(
    video=(VideoSupport("h264"),),
    audio=(AudioSupport("aac"),),
    containers=frozenset({"mp4", "hls-fmp4"}),
)

# HEVC 也能解的浏览器（iPhone / 带 HEVC 硬解的 Chrome）：预转省流档（HEVC）对它
# 只需重封装不重编码——这是外网降档时产物能胜出的前提
HEVC_OK = ClientCapability(
    video=(VideoSupport("h264"), VideoSupport("hevc")),
    audio=(AudioSupport("aac"),),
    containers=frozenset({"mp4", "hls-fmp4"}),
)


def test_capability_from_request_preserves_native_hls_flag():
    capability = playback_plan.capability_from_request(ClientCapabilityIn(native_hls=True))

    assert capability.native_hls is True


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'decide.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    reset_setting_store()
    init_setting_store(database=get_database())
    yield get_database()
    reset_setting_store()
    await dispose_db()
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def stub_probes(monkeypatch):
    """探测层打桩：测试环境没有 ffprobe，也没有真实媒体文件。

    关键帧间隔固定给 4 秒（正常 GOP），硬件加速固定不可用——这样档位差异
    完全由被测的候选择优与配置决定，不受环境影响。
    """
    monkeypatch.setattr(
        playback_plan, "probe_keyframe_interval", lambda path, duration: 4.0
    )
    monkeypatch.setattr(playback_plan, "hardware_available", lambda: False)


def _file(
    library_id: int,
    item_id: int,
    *,
    codec: str,
    resolution: str,
    bit_rate: int | None = None,
    container: str = "mkv",
    hdr: str | None = None,
) -> LibraryFile:
    return LibraryFile(
        library_id=library_id,
        media_item_id=item_id,
        season_number=0,
        episode_number=0,
        file_path=f"/lib{library_id}/{item_id}.{codec}.{resolution}.{container}",
        size_bytes=1,
        source=FileSource.SCANNED,
        state=FileState.IN_PLACE,
        container=container,
        video_codec=codec,
        resolution=resolution,
        hdr=hdr,
        duration_seconds=5400,
        bit_rate=bit_rate,
        audio_streams=[{"codec": "aac", "channels": 6, "default": True}],
    )


def _portable(
    source: LibraryFile,
    *,
    codec: str,
    resolution: str,
    bit_rate: int | None = 1_500_000,
    container: str = "mkv",
) -> LibraryFile:
    """预转码便携版：派生行（``derivation`` 非空，``derives_from_file_id`` 指向源文件），
    与 ``preload/service.py`` 登记产物的形状一致（产物是 .mkv，播放时要重封装）。"""
    row = _file(
        source.library_id,
        source.media_item_id,
        codec=codec,
        resolution=resolution,
        bit_rate=bit_rate,
        container=container,
    )
    row.derives_from_file_id = source.id
    row.derivation = {"kind": "preload", "preset_id": f"portable_{resolution}", "task_id": 1}
    return row


async def test_prefers_direct_playable_version_over_higher_resolution(db):
    """能直通的 1080p 胜过要转码的 2160p——直通率是这个播放器的北极星指标。"""
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=["/movies"]
        )
        item = MediaItem(kind="movie", tmdb_id=1, title="示例", original_title="Example")
        session.add(item)
        await session.flush()
        uhd = _file(library.id, item.id, codec="hevc", resolution="2160p")
        hd = _file(library.id, item.id, codec="h264", resolution="1080p")
        session.add_all([uhd, hd])
        await session.commit()
        await session.refresh(hd)

        decision = await playback_plan.decide_for_files(
            [uhd, hd], CHROME, can_self_enable=True
        )

    assert isinstance(decision, PlaybackPlan)
    assert decision.tier is PlaybackTier.REMUX
    assert decision.file_id == hd.id


async def _library_and_item(db, tmdb_id: int) -> tuple[int, int]:
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=["/movies"]
        )
        item = MediaItem(kind="movie", tmdb_id=tmdb_id, title="示例", original_title="Example")
        session.add(item)
        await session.commit()
        return library.id, item.id


async def _add(db, *rows: LibraryFile) -> None:
    """落库并刷新（派生行要引用源行的 id，所以调用方按「源先、便携版后」分次落库）。"""
    async with db.session() as session:
        session.add_all(rows)
        await session.commit()
        for row in rows:
            await session.refresh(row)


async def test_uplink_step_down_lets_preload_artifact_win(db):
    """外网降档把原片压到转码档（要现场重编码）时，装得下家宽上行的预转省流档
    以重封装档（不重编码）胜出——外网用户自动拿到预转产物，不用等现场转。
    产物是一行派生台账（derivation 非空），外网会话里它本来就默认胜出（见下方的外网便携版用例），
    这里的断言只证明：家宽上行的降档不再是它胜出的前提。"""
    library_id, item_id = await _library_and_item(db, 2)
    original = _file(library_id, item_id, codec="hevc", resolution="2160p", bit_rate=54_000_000)
    await _add(db, original)
    artifact = _portable(original, codec="hevc", resolution="1080p", bit_rate=1_500_000)
    await _add(db, artifact)

    async with db.session():
        wan = await playback_plan.decide_for_files(
            [original, artifact], HEVC_OK, can_self_enable=True,
            uplink_bps=30_000_000, external=True,
        )
        lan = await playback_plan.decide_for_files(
            [original, artifact], HEVC_OK, can_self_enable=True
        )

    # 外网：便携版默认用，上行 30 Mbps 与它无关，不弹降档确认
    assert isinstance(wan, PlaybackPlan)
    assert wan.tier is PlaybackTier.REMUX
    assert wan.file_id == artifact.id
    assert wan.uplink_forced is False
    # 内网：没有上行预算，原片重封装档与便携版同档、分辨率更高——4K 原片赢，
    # 省流档绝不劫持局域网画质
    assert isinstance(lan, PlaybackPlan)
    assert lan.tier is PlaybackTier.REMUX
    assert lan.file_id == original.id


async def test_lan_chrome_keeps_original_when_portable_would_win_on_tier(db):
    """回归：只解 H.264 的浏览器在内网同时面对 4K HEVC 原片与 480p 便携版。

    便携版的重封装档（档 1）原本胜过原片的转码档，内网用户看到 480p。内网里便携版不参与
    择优：原片需要软件转码同意，就照实说「要同意」，而不是悄悄换成 480p。"""
    library_id, item_id = await _library_and_item(db, 5)
    original = _file(library_id, item_id, codec="hevc", resolution="2160p", bit_rate=54_000_000)
    await _add(db, original)
    portable = _portable(original, codec="h264", resolution="480p", bit_rate=800_000)
    await _add(db, portable)

    async with db.session():
        lan = await playback_plan.decide_for_files(
            [original, portable], CHROME, can_self_enable=True
        )
        await get_setting_store().set(PlaybackPolicySetting(software_transcode_enabled=True))
        lan_consented = await playback_plan.decide_for_files(
            [original, portable], CHROME, can_self_enable=True
        )

    assert isinstance(lan, ConsentRequired)
    assert isinstance(lan_consented, PlaybackPlan)
    assert lan_consented.file_id == original.id
    assert lan_consented.tier is PlaybackTier.SOFTWARE_TRANSCODE
    assert lan_consented.portable_source_file_id is None


async def test_lan_portable_joins_only_when_original_is_rejected(db):
    """原片直接放不了（杜比视界、无 GPU：整片拒绝）时，内网才让便携版参与。

    用 HEVC 也能解的浏览器：编码不支持的原片只是「要同意转码」，不是拒绝。"""
    library_id, item_id = await _library_and_item(db, 6)
    original = _file(
        library_id, item_id, codec="hevc", resolution="2160p", bit_rate=54_000_000,
        hdr="Dolby Vision",
    )
    await _add(db, original)
    portable = _portable(original, codec="h264", resolution="720p", bit_rate=2_000_000)
    await _add(db, portable)

    async with db.session():
        decision = await playback_plan.decide_for_files(
            [original, portable], HEVC_OK, can_self_enable=True
        )

    assert isinstance(decision, PlaybackPlan)
    assert decision.file_id == portable.id
    assert decision.portable_source_file_id == original.id
    assert decision.portable_resolution == "720p"


async def test_lan_manually_picked_portable_plays_as_asked(db):
    """手选便携版（只给它的 file_id）不受内网择优规则约束，并带上源文件与分辨率标签。"""
    library_id, item_id = await _library_and_item(db, 7)
    original = _file(library_id, item_id, codec="h264", resolution="1080p", bit_rate=8_000_000)
    await _add(db, original)
    portable = _portable(original, codec="h264", resolution="720p", bit_rate=2_000_000)
    await _add(db, portable)

    async with db.session() as session:
        picked = await playback_plan.decide_for_file(
            session, portable.id, CHROME, can_self_enable=True, visible_library_ids={library_id}
        )
        original_plan = await playback_plan.decide_for_file(
            session, original.id, CHROME, can_self_enable=True, visible_library_ids={library_id}
        )

    assert isinstance(picked, PlaybackPlan)
    assert picked.file_id == portable.id
    assert picked.portable_source_file_id == original.id
    assert picked.portable_resolution == "720p"
    assert isinstance(original_plan, PlaybackPlan)
    assert original_plan.portable_source_file_id is None


async def test_external_portable_wins_without_any_uplink_setting(db):
    """外网有便携版就默认用便携版：未配置家宽上行时，原片 4K 的直通档也让位。"""
    library_id, item_id = await _library_and_item(db, 8)
    original = _file(library_id, item_id, codec="hevc", resolution="2160p", bit_rate=54_000_000)
    await _add(db, original)
    portable = _portable(original, codec="hevc", resolution="1080p", bit_rate=1_500_000)
    await _add(db, portable)

    async with db.session():
        decision = await playback_plan.decide_for_files(
            [original, portable], HEVC_OK, can_self_enable=True, external=True
        )

    assert isinstance(decision, PlaybackPlan)
    assert decision.file_id == portable.id
    assert decision.portable_source_file_id == original.id
    assert decision.portable_resolution == "1080p"


async def test_external_portable_wins_regardless_of_uplink_budget(db):
    """家宽上行配置了也一样：便携版不看上行（它的码率本来就装得下），原片的降档不参与。"""
    library_id, item_id = await _library_and_item(db, 9)
    original = _file(library_id, item_id, codec="hevc", resolution="2160p", bit_rate=54_000_000)
    await _add(db, original)
    portable = _portable(original, codec="hevc", resolution="1080p", bit_rate=1_500_000)
    await _add(db, portable)

    async with db.session():
        decision = await playback_plan.decide_for_files(
            [original, portable], HEVC_OK, can_self_enable=True,
            uplink_bps=30_000_000, external=True,
        )

    assert isinstance(decision, PlaybackPlan)
    assert decision.file_id == portable.id
    assert decision.uplink_forced is False


async def test_external_without_portable_keeps_uplink_step_down(db):
    """没有便携版的条目维持外网降档：源码率超出上行就转码降码率，标 uplink_forced 让界面确认。"""
    library_id, item_id = await _library_and_item(db, 10)
    original = _file(library_id, item_id, codec="hevc", resolution="2160p", bit_rate=54_000_000)
    await _add(db, original)
    await get_setting_store().set(PlaybackPolicySetting(software_transcode_enabled=True))

    async with db.session():
        decision = await playback_plan.decide_for_files(
            [original], HEVC_OK, can_self_enable=True, uplink_bps=30_000_000, external=True
        )

    assert isinstance(decision, PlaybackPlan)
    assert decision.file_id == original.id
    assert decision.uplink_forced is True
    assert decision.tier is PlaybackTier.SOFTWARE_TRANSCODE


async def test_external_portable_transcodes_from_itself_when_both_need_work(db):
    """都要转码时也从便携版转：便携版与原片都得重编码，外网挑便携版（成本低一个数量级），
    不会因为原片分辨率更高就回去转 4K。"""
    library_id, item_id = await _library_and_item(db, 11)
    original = _file(library_id, item_id, codec="hevc", resolution="2160p", bit_rate=54_000_000)
    await _add(db, original)
    portable = _portable(original, codec="hevc", resolution="1080p", bit_rate=1_500_000)
    await _add(db, portable)
    await get_setting_store().set(PlaybackPolicySetting(software_transcode_enabled=True))

    async with db.session():
        decision = await playback_plan.decide_for_files(
            [original, portable], CHROME, can_self_enable=True, external=True
        )

    assert isinstance(decision, PlaybackPlan)
    assert decision.file_id == portable.id
    assert decision.tier is PlaybackTier.SOFTWARE_TRANSCODE
    assert decision.video.action == "transcode"
    assert decision.video.height == 1080


async def test_external_portable_beats_original_direct_play(db):
    """外网：原片能直通（档 0）也让位给能重封装的便携版——外网默认用便携版，省流量。"""
    library_id, item_id = await _library_and_item(db, 12)
    original = _file(
        library_id, item_id, codec="h264", resolution="1080p", bit_rate=8_000_000, container="mp4"
    )
    await _add(db, original)
    # 便携版是 MKV 封装：要重封装（档 1），原片是 MP4 直通（档 0）——外网照样选便携版
    portable = _portable(
        original, codec="h264", resolution="720p", bit_rate=2_000_000, container="mkv"
    )
    await _add(db, portable)

    async with db.session():
        decision = await playback_plan.decide_for_files(
            [original, portable], CHROME, can_self_enable=True, external=True
        )

    assert isinstance(decision, PlaybackPlan)
    assert decision.file_id == portable.id
    assert decision.tier is PlaybackTier.REMUX


async def test_external_picks_the_playable_portable_among_several(db):
    """同一部片有两份便携版（源不同）：外网挑客户端能重封装的那份，不挑要转码的。"""
    library_id, item_id = await _library_and_item(db, 13)
    hevc_source = _file(library_id, item_id, codec="hevc", resolution="2160p", bit_rate=54_000_000)
    await _add(db, hevc_source)
    hevc_portable = _portable(hevc_source, codec="hevc", resolution="1080p", bit_rate=1_500_000)
    h264_source = _file(library_id, item_id, codec="h264", resolution="1080p", bit_rate=9_000_000)
    await _add(db, h264_source)
    h264_portable = _portable(h264_source, codec="h264", resolution="720p", bit_rate=2_000_000)
    await _add(db, hevc_portable, h264_portable)

    async with db.session():
        decision = await playback_plan.decide_for_files(
            [hevc_source, h264_source, hevc_portable, h264_portable],
            CHROME,
            can_self_enable=True,
            external=True,
        )

    assert isinstance(decision, PlaybackPlan)
    assert decision.file_id == h264_portable.id
    assert decision.portable_source_file_id == h264_source.id


async def test_external_portable_yields_to_original_only_when_portable_cannot_play(db):
    """便携版连「同意软件转码」都不成立时（只能转码、开关未开）才回落原片直通。"""
    library_id, item_id = await _library_and_item(db, 14)
    original = _file(
        library_id, item_id, codec="h264", resolution="1080p", bit_rate=8_000_000, container="mp4"
    )
    await _add(db, original)
    portable = _portable(original, codec="hevc", resolution="720p", bit_rate=1_500_000)
    await _add(db, portable)

    async with db.session():
        decision = await playback_plan.decide_for_files(
            [original, portable], CHROME, can_self_enable=True, external=True
        )

    assert isinstance(decision, PlaybackPlan)
    assert decision.file_id == original.id
    assert decision.tier is PlaybackTier.DIRECT_PLAY
    assert decision.portable_source_file_id is None


async def test_file_outside_visible_libraries_is_not_found(db):
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="私藏库", kind="movie", root_paths=["/private"]
        )
        item = MediaItem(kind="movie", tmdb_id=2, title="私藏", original_title="Private")
        session.add(item)
        await session.flush()
        row = _file(library.id, item.id, codec="h264", resolution="1080p")
        session.add(row)
        await session.commit()
        await session.refresh(row)

        assert (
            await playback_plan.decide_for_file(
                session,
                row.id,
                CHROME,
                can_self_enable=True,
                visible_library_ids=set(),
            )
            is None
        )
        # 可见时正常出计划
        decision = await playback_plan.decide_for_file(
            session,
            row.id,
            CHROME,
            can_self_enable=True,
            visible_library_ids={library.id},
        )
    assert isinstance(decision, PlaybackPlan)


async def test_software_transcode_switch_comes_from_settings(db):
    """无硬件 + HEVC → 要么请求同意，要么（开关已开）直接给软转计划。"""
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=["/movies"]
        )
        item = MediaItem(kind="movie", tmdb_id=3, title="示例", original_title="Example")
        session.add(item)
        await session.flush()
        row = _file(library.id, item.id, codec="hevc", resolution="1080p")
        session.add(row)
        await session.commit()

        first = await playback_plan.decide_for_files([row], CHROME, can_self_enable=True)
        assert isinstance(first, ConsentRequired)
        assert first.setting_namespace == "playback.policy"

        await get_setting_store().set(
            PlaybackPolicySetting(software_transcode_enabled=True)
        )
        second = await playback_plan.decide_for_files([row], CHROME, can_self_enable=True)

    assert isinstance(second, PlaybackPlan)
    assert second.tier is PlaybackTier.SOFTWARE_TRANSCODE


async def test_skips_keyframe_probe_for_candidates_that_transcode(db, monkeypatch):
    """视频已经确定要转码时，不应为 Remux 安全门槛读取源片。"""
    calls = []
    monkeypatch.setattr(
        playback_plan,
        "probe_keyframe_interval",
        lambda path, duration: calls.append((path, duration)) or 4.0,
    )

    row = _file(1, 1, codec="hevc", resolution="2160p")
    decision = await playback_plan.decide_for_files(
        [row], CHROME, can_self_enable=True
    )

    assert isinstance(decision, ConsentRequired)
    assert calls == []


async def test_keeps_keyframe_probe_for_remux_candidates(db, monkeypatch):
    """视频复制的 Remux 候选仍需采样，未知时才能安全降级转码。"""
    calls = []
    monkeypatch.setattr(
        playback_plan,
        "probe_keyframe_interval",
        lambda path, duration: calls.append((path, duration)) or 4.0,
    )

    row = _file(1, 1, codec="h264", resolution="1080p")
    decision = await playback_plan.decide_for_files(
        [row], CHROME, can_self_enable=True
    )

    assert isinstance(decision, PlaybackPlan)
    assert decision.tier is PlaybackTier.REMUX
    assert len(calls) == 1


async def test_view_translation_covers_three_outcomes(db):
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=["/movies"]
        )
        item = MediaItem(kind="movie", tmdb_id=4, title="示例", original_title="Example")
        session.add(item)
        await session.flush()
        playable = _file(library.id, item.id, codec="h264", resolution="1080p")
        needs_transcode = _file(library.id, item.id, codec="hevc", resolution="1080p")
        session.add_all([playable, needs_transcode])
        await session.commit()

        plan_view = playback_plan.to_view(
            await playback_plan.decide_for_files([playable], CHROME, can_self_enable=True)
        )
        consent_view = playback_plan.to_view(
            await playback_plan.decide_for_files(
                [needs_transcode], CHROME, can_self_enable=False
            )
        )

    assert plan_view.outcome == "plan"
    assert plan_view.video.action == "copy" and plan_view.audio.action == "copy"
    assert plan_view.reason  # 中文理由是一等公民字段，诊断面板与错误提示共用

    assert consent_view.outcome == "consent"
    assert consent_view.can_self_enable is False  # 普通成员：渲染说明而非按钮
    assert consent_view.cost_hint


def test_best_prefers_higher_resolution_at_equal_tier():
    """同档位并列时取更高分辨率——直通判定放开后 1080p/2160p 经常同档。

    改动前是 `min(key=tier)`，并列取数据库返回顺序的第一个：同一部片今天播
    4K 明天播 1080p，全看行序。同档位代价相同（都不重编码），画质当然取高。
    """
    from movieclaw_api.services.playback.plan import _best
    from movieclaw_playback.decide import AudioPlan, PlaybackPlan, PlaybackTier, VideoPlan

    def plan(file_id: int) -> PlaybackPlan:
        return PlaybackPlan(
            tier=PlaybackTier.REMUX,
            file_id=file_id,
            container="hls-fmp4",
            video=VideoPlan(action="copy"),
            audio=AudioPlan(action="copy"),
        )

    # 1080p 在前、4K 在后：不看高度的话会取到先来的 1080p
    picked = _best([(plan(1), 1080, None), (plan(2), 2160, None)])
    assert picked.file_id == 2

    # 高度未知的候选排最后，结果保持确定
    picked = _best([(plan(3), None, None), (plan(4), 1080, None)])
    assert picked.file_id == 4

    # 档位仍然优先于分辨率：能直通的 1080p 胜过要转码的 4K
    transcode_4k = PlaybackPlan(
        tier=PlaybackTier.HARDWARE_TRANSCODE,
        file_id=5,
        container="hls-fmp4",
        video=VideoPlan(action="transcode", codec="h264"),
        audio=AudioPlan(action="copy"),
    )
    picked = _best([(transcode_4k, 2160, None), (plan(6), 1080, None)])
    assert picked.file_id == 6


async def test_browser_decode_failures_on_copy_tiers_reach_hardware_transcode(db, monkeypatch):
    """真机复现：4K HEVC MKV，浏览器申报能解 HEVC，hls.js 却 bufferAddCodecError。

    网页按「档 1 失败 → 档 2 失败 → 连败两次跳过剩下的直通档」上报 failed_tiers；
    NAS 有 VAAPI 时最后一轮必须是档 3 硬件转码，而不是档 4 软件转码同意框。
    """
    monkeypatch.setattr(playback_plan, "hardware_available", lambda: True)
    hevc_browser = ClientCapability(
        video=(VideoSupport("h264"), VideoSupport("hevc")),
        audio=(AudioSupport("aac"),),
        containers=frozenset({"mp4", "hls-fmp4"}),
    )
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="动漫", kind="tv", root_paths=["/comic"]
        )
        item = MediaItem(kind="tv", tmdb_id=3, title="牧神记", original_title="Tales")
        session.add(item)
        await session.flush()
        row = _file(library.id, item.id, codec="hevc", resolution="2160p")
        session.add(row)
        await session.commit()
        await session.refresh(row)

        async def decide(*failed: PlaybackTier):
            return await playback_plan.decide_for_file(
                session, row.id, hevc_browser, can_self_enable=True,
                failed_tiers=frozenset(failed),
            )

        first = await decide()
        assert isinstance(first, PlaybackPlan) and first.tier is PlaybackTier.REMUX
        second = await decide(PlaybackTier.REMUX)
        assert isinstance(second, PlaybackPlan)
        assert second.tier is PlaybackTier.AUDIO_TRANSCODE
        third = await decide(
            PlaybackTier.DIRECT_PLAY, PlaybackTier.REMUX, PlaybackTier.AUDIO_TRANSCODE
        )
        assert isinstance(third, PlaybackPlan)
        assert third.tier is PlaybackTier.HARDWARE_TRANSCODE
        assert third.video.action == "transcode"


async def test_external_without_portable_or_uplink_plays_original(db):
    """外网、没有便携版、也没配上行：照旧按原片判定，不降档、不弹确认。"""
    library_id, item_id = await _library_and_item(db, 15)
    original = _file(library_id, item_id, codec="hevc", resolution="2160p", bit_rate=54_000_000)
    await _add(db, original)

    async with db.session():
        decision = await playback_plan.decide_for_files(
            [original], HEVC_OK, can_self_enable=True, external=True
        )

    assert isinstance(decision, PlaybackPlan)
    assert decision.file_id == original.id
    assert decision.tier is PlaybackTier.REMUX
    assert decision.uplink_forced is False
