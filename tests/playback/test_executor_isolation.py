"""「在哪儿跑」与「用哪种硬件跑」分开：本机与远程的同名后端各是各的。

本机探测（hwprobe）的结论只说 NAS 这块硬件；远程 Worker 的能力只看它自己在 hello 里的申报。
两边同名（NAS 插着 NVIDIA 卡、远程也是 Windows NVENC；以后 NAS 核显与低功耗主机都是 QSV）时，
谁也不能覆盖、继承或撤销另一边。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from movieclaw_api.services.playback import backend_health, hwprobe
from movieclaw_api.services.playback import plan as plan_module
from movieclaw_api.services.playback import remote_worker as remote_worker_module
from movieclaw_api.services.playback.ffmpeg_args import (
    NVENC_TONEMAP,
    VAAPI_LOW_POWER,
    WorkerVideoCaps,
    build_hls_command,
    register_encoder_options,
    register_native_tonemap,
)
from movieclaw_api.services.playback.hwprobe import HwBackendStatus
from movieclaw_api.services.playback.remote_worker import RemoteWorkerRegistry
from movieclaw_playback.decide import AudioPlan, PlaybackPlan, PlaybackTier, VideoPlan


class FakeWebSocket:
    async def send_json(self, _message: dict) -> None:
        return None

    async def close(self, **_: object) -> None:
        return None


def _hdr_plan() -> PlaybackPlan:
    return PlaybackPlan(
        tier=PlaybackTier.HARDWARE_TRANSCODE,
        file_id=1,
        container="hls-fmp4",
        video=VideoPlan(
            action="transcode",
            codec="h264",
            height=1080,
            tone_map=True,
            source_codec="hevc",
            source_bit_depth=10,
        ),
        audio=AudioPlan(action="copy"),
    )


def _vf(argv: list[str]) -> str:
    return argv[argv.index("-vf") + 1]


def _command(plan: PlaybackPlan, backend: str, caps: WorkerVideoCaps | None) -> list[str]:
    return build_hls_command(
        plan,
        source_path="/media/a.mkv",
        session_dir=Path("/tmp/s"),
        hw_backend=backend,
        worker_caps=caps,
    ).argv


@pytest.fixture
def registry(monkeypatch):
    """全新的进程级注册表，远程转码开关打开。"""
    fresh = RemoteWorkerRegistry()
    monkeypatch.setattr(remote_worker_module, "_registry", fresh)
    monkeypatch.setattr(remote_worker_module, "remote_worker_enabled", lambda: True)
    return fresh


async def _connect(registry: RemoteWorkerRegistry, worker_id: str, backend: str, **caps):
    encoder = {"videotoolbox": "h264_videotoolbox", "nvenc": "h264_nvenc"}[backend]
    return await registry.register(
        FakeWebSocket(),
        {
            "worker_id": worker_id,
            "capabilities": {"backends": [backend], "encoders": [encoder], **caps},
        },
    )


def test_local_tonemap_probe_and_worker_filters_stay_on_their_own_side():
    """NAS 本机探到 tonemap_cuda、远程 Worker 的 ffmpeg 没有它：本机命令走 GPU 映射，远程命令
    退回软件映射。反过来（本机没有、远程有）也一样，谁也不替谁做主。"""
    plan = _hdr_plan()
    with_cuda = WorkerVideoCaps(filters=frozenset({"scale_cuda", "tonemap_cuda"}))
    without_cuda = WorkerVideoCaps(filters=frozenset({"scale_cuda"}))

    register_native_tonemap("nvenc", NVENC_TONEMAP)
    try:
        assert _vf(_command(plan, "nvenc", None)).startswith("tonemap_cuda=")
        assert not _vf(_command(plan, "nvenc", without_cuda)).startswith("tonemap_cuda=")
    finally:
        register_native_tonemap("nvenc", None)

    assert not _vf(_command(plan, "nvenc", None)).startswith("tonemap_cuda=")
    assert _vf(_command(plan, "nvenc", with_cuda)).startswith("tonemap_cuda=")


def test_remote_command_never_inherits_local_encoder_options():
    """VAAPI 低功耗是 NAS 这块核显自检出来的；同名后端交给远程执行时不能带上它。"""
    plan = PlaybackPlan(
        tier=PlaybackTier.HARDWARE_TRANSCODE,
        file_id=1,
        container="hls-fmp4",
        video=VideoPlan(action="transcode", codec="h264", height=720, source_bit_depth=8),
        audio=AudioPlan(action="copy"),
    )
    register_encoder_options("vaapi", VAAPI_LOW_POWER)
    try:
        assert "-low_power" in _command(plan, "vaapi", None)
        assert "-low_power" not in _command(plan, "vaapi", WorkerVideoCaps())
    finally:
        register_encoder_options("vaapi", ())


@pytest.mark.asyncio
async def test_local_backend_name_does_not_make_a_remote_worker_of_that_kind(registry):
    """NAS 本机有 NVENC、远程只连着一台 Mac：HDR 片分流给远程时只能落到 Mac 上，不能因为
    「nvenc 在可用列表里」就当成有一台 NVENC Worker——那台机器根本不存在，派单必然 503。"""
    await _connect(registry, "mac", "videotoolbox")
    assert plan_module.select_execution_backend(
        _hdr_plan(),
        available=("nvenc", "videotoolbox"),
        local_backends=("nvenc",),
        remote_video_available=True,
    ) == ("videotoolbox", True)
    assert not remote_worker_module.remote_backend_can_run(_hdr_plan(), "nvenc")


@pytest.mark.asyncio
async def test_same_backend_on_both_sides_keeps_two_separate_entries(registry, monkeypatch):
    """本机与远程都是 NVENC：本机的探测条目原样保留（连同它的不可用原因），远程单列一条；
    决策用的可用列表不重复。Worker 下线后立刻只剩本机的结论。"""
    local = HwBackendStatus(
        name="nvenc", label="NVIDIA 显卡（NVENC）", available=False, detail="驱动版本过低"
    )
    monkeypatch.setattr(hwprobe, "_cache", [local])

    connection = await _connect(registry, "win", "nvenc", filters=["scale_cuda"])
    assert hwprobe.probe_backends() == [local]
    assert [(s.name, s.remote, s.available) for s in hwprobe.remote_backend_statuses()] == [
        ("nvenc", True, True)
    ]
    assert hwprobe.available_backends() == ("nvenc",)
    assert hwprobe.available_local_backends() == ()

    await registry.unregister(connection)
    assert hwprobe.available_backends() == ()
    assert hwprobe.remote_backend_statuses() == []
    assert hwprobe.probe_backends() == [local]


@pytest.mark.asyncio
async def test_local_demotion_does_not_block_the_remote_worker_of_the_same_kind(
    registry, monkeypatch
):
    """本机 NVENC 起步即死被熔断，说的是 NAS 这块卡；远程同名 Worker 照常接单。"""
    monkeypatch.setattr(hwprobe, "_cache", [])
    await _connect(registry, "win", "nvenc", filters=["scale_cuda", "tonemap_cuda"])
    sdr = PlaybackPlan(
        tier=PlaybackTier.HARDWARE_TRANSCODE,
        file_id=1,
        container="hls-fmp4",
        video=VideoPlan(action="transcode", codec="h264", height=1080, source_bit_depth=8),
        audio=AudioPlan(action="copy"),
    )
    # 没熔断时普通 SDR 片本地优先（省掉远程供片的延迟）
    assert plan_module.select_execution_backend(
        sdr, available=("nvenc",), local_backends=("nvenc",), remote_video_available=True
    ) == ("nvenc", False)
    backend_health.demote("nvenc")
    try:
        assert plan_module.select_execution_backend(
            sdr,
            available=("nvenc",),
            local_backends=("nvenc",),
            remote_video_available=True,
        ) == ("nvenc", True)
    finally:
        backend_health.reset()
