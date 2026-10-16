"""预转码功能的端到端测试（docs/design/preload-transcode.md §3/§6/§7）。

覆盖：
1. 下单链路：预估 → 下单（配额预检/直拷拒绝/幂等）→ 任务排队；
2. Worker 领取协议：配对凭证领任务 → 进度续约 → 分段上传断点续转 →
   complete 校验登记为派生版本 → 播放枚举能选到它；
3. 派生物生命周期：源删除 → 产物自动清理；失败重试；取消；
4. 冷数据清理预览与执行。

不跑真 ffmpeg：complete 的产物用手工构造的最小 MKV 片段 + monkeypatch
probe_media 的探测结果校验服务端逻辑；命令构造（NVENC 链选择/编码器随
档位/直拷判定）经由 claim 响应的 argv 断言覆盖。
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from functools import partial
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from movieclaw_api.services.media_probe import MediaSpec
from movieclaw_db.engine import get_database
from movieclaw_db.models import FileState, LibraryFile, MediaItem
from movieclaw_db.models.preload_task import PreloadTaskStatus
from movieclaw_db.repositories.library_repo import LibraryRepository

_PRELOAD = "/api/v1/preload"
_WORKER = "/api/v1/preload-worker"
_AUTH = "/api/v1/auth"
_ADMIN = {"username": "admin", "password": "s3cret-pass"}


def _make_client(tmp_path, monkeypatch, *, name: str):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / f'{name}.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / f".{name}_secret_key"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("MOVIECLAW_PRELOAD_DIR", str(tmp_path / "preload"))
    from movieclaw_api.core.config import get_settings

    get_settings.cache_clear()
    from movieclaw_api.services.auth import reset_auth_state
    from movieclaw_api.settings.store import reset_setting_store
    from movieclaw_db.crypto import reset_secret_box

    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    from movieclaw_api.app import create_app

    return TestClient(create_app())


@pytest.fixture
def client(tmp_path, monkeypatch):
    with _make_client(tmp_path, monkeypatch, name="preload") as c:
        c.post(f"{_AUTH}/bootstrap", json=_ADMIN)
        c.post(f"{_AUTH}/login", json=_ADMIN)
        yield c

    from movieclaw_api.core.config import get_settings
    from movieclaw_api.services.auth import reset_auth_state
    from movieclaw_api.settings.store import reset_setting_store
    from movieclaw_db.crypto import reset_secret_box

    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    get_settings.cache_clear()


_seed_counter = {"n": 0}


def _seed_file(
    client: TestClient, tmp_path, *, hdr=None, codec="hevc", resolution="2160p",
    color_space: str | None = None,
) -> int:
    """造一个源文件行（4K HEVC HDR 是预转码的典型对象）。"""
    _seed_counter["n"] += 1
    n = _seed_counter["n"]

    async def _seed() -> int:
        media_root = tmp_path / "movies"
        media_root.mkdir(exist_ok=True)
        path = media_root / f"movie{n}.mkv"
        path.write_bytes(b"FAKE-MEDIA-BYTES" * 256)
        async with get_database().session() as session:
            from sqlmodel import select

            from movieclaw_db.models import Library

            library = (
                await session.execute(select(Library).where(Library.name == "电影库"))
            ).scalar_one_or_none()
            if library is None:
                library = await LibraryRepository(session).create(
                    name="电影库", kind="movie", root_paths=[str(media_root)]
                )
            item = (
                await session.execute(
                    select(MediaItem).where(MediaItem.tmdb_id == 1, MediaItem.kind == "movie")
                )
            ).scalar_one_or_none()
            if item is None:
                item = MediaItem(kind="movie", tmdb_id=1, title="示例", original_title="Example")
                session.add(item)
                await session.flush()
            row = LibraryFile(
                library_id=library.id,
                media_item_id=item.id,
                file_path=str(path),
                size_bytes=path.stat().st_size,
                source="scanned",
                state=FileState.IN_PLACE,
                container="mkv",
                video_codec=codec,
                resolution=resolution,
                hdr=hdr,
                color_space=color_space,
                duration_seconds=7200,
                audio_streams=[{"codec": "eac3", "channels": 6}],
                subtitle_streams=[{"codec": "subrip"}, {"codec": "hdmv_pgs_subtitle"}],
            )
            session.add(row)
            await session.commit()
            return int(row.id)

    return client.portal.call(partial(_seed))  # type: ignore[attr-defined]


def _claim_and_upload(client: TestClient, token: str, task_id: int) -> dict:
    """领取任务并上传第一段**真实 MKV**；ffmpeg 缺失时跳过。"""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("concat 校验需要 ffmpeg")
    task = client.post(f"{_WORKER}/claim", headers=_worker_headers(token)).json()["data"]["task"]
    assert task is not None and task["task_id"] == task_id
    _upload_segment(client, task)
    return task


def _upload_segment(client: TestClient, task: dict) -> None:
    """给已领取的任务上传 seg00000（真实 MKV：complete 走 concat demuxer，
    假字节过不了 EBML 解析）。"""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("concat 校验需要 ffmpeg")
    task_id = task["task_id"]
    artifact_url = "/" + task["artifact_url"]
    sep = "&" if "?" in artifact_url else "?"
    seg = Path(tempfile.gettempdir()) / f"preload-seg-{task_id}.mkv"
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=duration=1:size=320x180:rate=10",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(seg)],
        check=True, timeout=60,
    )
    put = client.put(
        f"{artifact_url}{sep}part=0",
        content=seg.read_bytes(),
        headers={"Content-Type": "application/octet-stream"},
    )
    assert put.status_code == 201, put.text
    # 服务端落盘的段必须是合法 MKV（EBML 魔数 0x1A45DFA3）；曾经因追加模式
    # 写坏段文件，concat 才报 EBML 解析失败——此断言防回归
    from movieclaw_api.services.preload import service as _svc

    part_file = _svc.artifact_dir_for(task_id) / "seg00000.mkv"
    assert part_file.read_bytes()[:4] == bytes([0x1A, 0x45, 0xDF, 0xA3]), part_file.read_bytes()[:16]
    seg.unlink(missing_ok=True)


def _fake_probe(monkeypatch) -> None:
    monkeypatch.setattr(
        "movieclaw_api.api.routes.preload_worker.probe_media",
        lambda path: MediaSpec(
            resolution="1080p", video_codec="h264", hdr=None, bit_depth=8,
            duration_seconds=7200, bit_rate=3_900_000,
            # 登记闸：产物没有音轨（源有）整件作废——探测结果必须带音轨
            audio_streams=[{"codec": "aac"}], subtitle_streams=[{"codec": "subrip"}],
        ),
    )


def _pair_worker(client: TestClient) -> str:
    """配对一台转码 Worker（client_type=worker → scope=transcode）。"""
    resp = client.post(
        f"{_AUTH}/device/authorize",
        json={"client_type": "worker", "client_name": "测试预转码 Worker", "platform": "windows"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    approve = client.post(f"{_AUTH}/devices/requests/{data['user_code']}/approve")
    assert approve.status_code == 200, approve.text
    redeem = client.post(f"{_AUTH}/device/token", json={"device_code": data["device_code"]})
    assert redeem.status_code == 200, redeem.text
    return redeem.json()["data"]["token"]


def _worker_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_estimate_and_create_task(client, tmp_path):
    file_id = _seed_file(client, tmp_path, hdr="HDR10")
    est = client.post(f"{_PRELOAD}/estimate", json={"file_id": file_id})
    assert est.status_code == 200, est.text
    data = est.json()["data"]
    assert data["estimated_bytes"] > 0
    assert data["quota_bytes"] > 0

    # 省流是默认档；同片源高清档的预估必然更大（码率帽 4M vs 1.5M）
    portable = client.post(
        f"{_PRELOAD}/estimate", json={"file_id": file_id, "preset_id": "portable_1080p"}
    )
    assert portable.json()["data"]["estimated_bytes"] > data["estimated_bytes"]

    # 未知档位明确拒绝
    assert client.post(
        f"{_PRELOAD}/estimate", json={"file_id": file_id, "preset_id": "nope"}
    ).status_code == 400
    assert client.post(
        f"{_PRELOAD}/tasks", json={"file_id": file_id, "preset_id": "nope"}
    ).status_code == 400

    created = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id})
    assert created.status_code == 200, created.text
    task = created.json()["data"]
    assert task["status"] == PreloadTaskStatus.QUEUED.value
    assert task["preset_id"] == "saver_1080p"

    # 幂等：再下一次还是同一个任务
    again = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id})
    assert again.json()["data"]["id"] == task["id"]


def test_claim_selects_nvenc_when_worker_reports_caps(client, tmp_path):
    """Worker 上报 NVENC 能力 → 下发全 GPU 链（编码器随档位）；HDR 源下色调映射回内存。"""
    file_id = _seed_file(client, tmp_path, hdr="HDR10")
    task_id = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id}).json()["data"]["id"]
    token = _pair_worker(client)
    caps = {
        "encoders": ["h264_nvenc", "hevc_nvenc"],
        "filters": ["scale_cuda", "hwdownload", "hwupload_cuda"],
        "hw_decoders": ["h264", "hevc"],
    }
    task = client.post(
        f"{_WORKER}/claim", json={"caps": caps}, headers=_worker_headers(token)
    ).json()["data"]["task"]
    # caps 无 tonemapx：HDR 源拒发（见上）
    assert task["argv"] == [] and task["unusable_reason"], task
    # Worker 对不可执行任务上报失败（process_task 的行为），然后取消重下
    client.post(
        f"{_WORKER}/tasks/{task['task_id']}/fail",
        json={"error": task["unusable_reason"]},
        headers=_worker_headers(token),
    )
    client.post(f"{_PRELOAD}/tasks/{task['task_id']}/cancel")
    client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id})

    # 补 tonemapx 能力再领：省流档（默认）→ HDR 硬解下载回内存 tonemapx
    # + HEVC NVENC，码率帽 1.5M
    caps_tx = {**caps, "filters": [*caps["filters"], "tonemapx"]}
    task_tx = client.post(
        f"{_WORKER}/claim", json={"caps": caps_tx}, headers=_worker_headers(token)
    ).json()["data"]["task"]
    argv = " ".join(task_tx["argv"])
    assert "hevc_nvenc" in argv and "h264_nvenc" not in argv, argv
    assert "hwdownload" in argv and "tonemapx=tonemap=bt2390" in argv, argv
    assert task_tx["argv"][task_tx["argv"].index("-maxrate") + 1] == "1.5M", task_tx["argv"]
    assert task_tx["hw_backend"] == "nvenc"

    # 换 SDR 4K 源再领：全 GPU 链（硬解 + scale_cuda，无 hwdownload）
    file2 = _seed_file(client, tmp_path)
    client.post(f"{_PRELOAD}/tasks", json={"file_id": file2})
    task2 = client.post(
        f"{_WORKER}/claim", json={"caps": caps_tx}, headers=_worker_headers(token)
    ).json()["data"]["task"]
    argv2 = " ".join(task2["argv"])
    assert "scale_cuda" in argv2 and "hwdownload" not in argv2, argv2
    assert "-hwaccel_output_format" in argv2 and "hevc_nvenc" in argv2, argv2

    # 同源下高清档：h264_nvenc + 4M 码率帽
    client.post(
        f"{_PRELOAD}/tasks", json={"file_id": file2, "preset_id": "portable_1080p"}
    )
    task3 = client.post(
        f"{_WORKER}/claim", json={"caps": caps_tx}, headers=_worker_headers(token)
    ).json()["data"]["task"]
    argv3 = " ".join(task3["argv"])
    assert "h264_nvenc" in argv3 and "hevc_nvenc" not in argv3, argv3
    assert task3["argv"][task3["argv"].index("-maxrate") + 1] == "4M", task3["argv"]


def test_create_rejects_already_portable_source(client, tmp_path):
    """高清档：源已是便携档（1080p H.264 SDR）时明确拒绝；省流档仍可下单
    （HEVC 重编码，无直拷判定）。"""
    file_id = _seed_file(client, tmp_path, codec="h264", resolution="1080p")
    resp = client.post(
        f"{_PRELOAD}/tasks", json={"file_id": file_id, "preset_id": "portable_1080p"}
    )
    assert resp.status_code == 400

    saver = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id})
    assert saver.status_code == 200, saver.text
    assert saver.json()["data"]["preset_id"] == "saver_1080p"


def test_create_rejects_over_quota(client, tmp_path, monkeypatch):
    """配额预检：占用已满时拒绝入队（§6 三件套之一）。"""
    from movieclaw_api.settings import PreloadSetting
    from movieclaw_api.settings.store import get_setting_store

    file_id = _seed_file(client, tmp_path, hdr="HDR10")

    async def _shrink() -> None:
        await get_setting_store().set(PreloadSetting(quota_bytes=1024))

    client.portal.call(partial(_shrink))  # type: ignore[attr-defined]
    resp = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id})
    assert resp.status_code == 409
    assert resp.json()["code"] == "PRELOAD_QUOTA_EXCEEDED"


def test_full_worker_roundtrip(client, tmp_path, monkeypatch):
    """全链路：下单 → Worker 领取 → 进度 → 分段上传 → complete → 派生版本。"""
    file_id = _seed_file(client, tmp_path, hdr="HDR10")
    task_id = client.post(
        f"{_PRELOAD}/tasks", json={"file_id": file_id, "preset_id": "portable_1080p"}
    ).json()["data"]["id"]
    token = _pair_worker(client)

    # 领取
    claim = client.post(f"{_WORKER}/claim", headers=_worker_headers(token))
    assert claim.status_code == 200, claim.text
    task = claim.json()["data"]["task"]
    assert task is not None and task["task_id"] == task_id
    # HDR 源 + Worker 未上报 tonemapx 能力 → 服务端拒发命令（上游 ffmpeg 的
    # zimg 没有 PQ→linear 路径，装不出正确的色调映射链），错误原文透传
    assert task["argv"] == [], task
    assert "jellyfin-ffmpeg" in task["unusable_reason"], task

    # 领取后队列空
    empty = client.post(f"{_WORKER}/claim", headers=_worker_headers(token))
    assert empty.json()["data"]["task"] is None

    # 进度上报（claim→running）
    progress = client.post(
        f"{_WORKER}/tasks/{task_id}/progress", json={}, headers=_worker_headers(token)
    )
    assert progress.status_code == 200

    # 分段上传（两段，第二段故意乱序一次验证 409）
    artifact_url = "/" + task["artifact_url"]
    sep = "&" if "?" in artifact_url else "?"
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg, "concat 校验需要 ffmpeg"
    seg = Path(tmp_path) / "roundtrip-seg0.mkv"
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=duration=1:size=320x180:rate=10",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(seg)],
        check=True, timeout=60,
    )
    part_a = seg.read_bytes()
    first = client.put(
        f"{artifact_url}{sep}part=0", content=part_a, headers={"Content-Type": "application/octet-stream"}
    )
    assert first.status_code == 201, (first.status_code, first.text)
    gap = client.put(
        f"{artifact_url}{sep}part=2", content=b"B" * 8, headers={"Content-Type": "application/octet-stream"}
    )
    assert gap.status_code == 409  # 跳段拒绝

    # complete：伪造产物文件与探测结果
    monkeypatch.setattr(
        "movieclaw_api.api.routes.preload_worker.probe_media",
        lambda path: MediaSpec(
            resolution="1080p", video_codec="h264", hdr=None, bit_depth=8,
            duration_seconds=7200, bit_rate=3_900_000,
            audio_streams=[{"codec": "aac"}], subtitle_streams=[{"codec": "subrip"}],
        ),
    )
    done = client.post(f"{_WORKER}/tasks/{task_id}/complete", json={}, headers=_worker_headers(token))
    assert done.status_code == 200, done.text
    assert done.json()["data"]["file_id"] > 0

    # 任务完成，产物是派生行
    detail = client.get(f"{_PRELOAD}/tasks/{task_id}").json()["data"]
    assert detail["status"] == PreloadTaskStatus.COMPLETED.value

    async def _check_derived() -> tuple[LibraryFile | None, int | None]:
        async with get_database().session() as session:
            row = await session.get(LibraryFile, done.json()["data"]["file_id"])
            return row, row.derives_from_file_id if row else None

    derived, derives_from = client.portal.call(partial(_check_derived))  # type: ignore[attr-defined]
    assert derived is not None and derives_from == file_id
    assert derived.media_item_id is not None  # 播放枚举能找到它
    assert derived.derivation["preset_id"] == "portable_1080p"
    # 登记行带产物流探测结果：决策引擎按它装配音轨/字幕轨——不填就是
    # 「无音轨」转码计划，播放没声音没字幕
    assert derived.audio_streams == [{"codec": "aac"}]
    assert derived.subtitle_streams == [{"codec": "subrip"}]
    assert derived.bit_rate == 3_900_000
    # 产物文件名带片名与码率（版本列表/下载直接显示）
    assert Path(derived.file_path).name == "示例 - 高清 1080p H.264 4Mbps.mkv"


def test_saver_preset_roundtrip_hevc(client, tmp_path, monkeypatch):
    """省流档（默认档）全链路：HEVC NVENC 链 + complete 登记 hevc 派生版本。"""
    file_id = _seed_file(client, tmp_path, hdr="HDR10")
    task_id = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id}).json()["data"]["id"]
    token = _pair_worker(client)
    caps = {
        "encoders": ["h264_nvenc", "hevc_nvenc"],
        "filters": ["scale_cuda", "hwdownload", "hwupload_cuda", "tonemapx"],
        "hw_decoders": ["h264", "hevc"],
    }
    claim = client.post(f"{_WORKER}/claim", json={"caps": caps}, headers=_worker_headers(token))
    assert claim.status_code == 200, claim.text
    task = claim.json()["data"]["task"]
    assert task is not None and task["task_id"] == task_id
    argv = " ".join(task["argv"])
    assert "hevc_nvenc" in argv and "h264_nvenc" not in argv, argv
    assert task["argv"][task["argv"].index("-maxrate") + 1] == "1.5M", task["argv"]

    _upload_segment(client, task)
    monkeypatch.setattr(
        "movieclaw_api.api.routes.preload_worker.probe_media",
        lambda path: MediaSpec(
            resolution="1080p", video_codec="hevc", hdr=None, bit_depth=8,
            duration_seconds=7200, bit_rate=1_400_000,
            audio_streams=[{"codec": "aac", "channels": 2}],
            subtitle_streams=[{"codec": "subrip"}],
        ),
    )
    done = client.post(
        f"{_WORKER}/tasks/{task_id}/complete", json={}, headers=_worker_headers(token)
    )
    assert done.status_code == 200, done.text
    derived_id = done.json()["data"]["file_id"]

    async def _check() -> tuple[str | None, str, str, list | None]:
        async with get_database().session() as session:
            row = await session.get(LibraryFile, derived_id)
            return (
                row.video_codec if row else None,  # type: ignore[union-attr]
                row.derivation["preset_id"],  # type: ignore[union-attr]
                Path(row.file_path).name,  # type: ignore[union-attr]
                row.audio_streams,  # type: ignore[union-attr]
            )

    codec, preset_id, artifact_name, audio_streams = client.portal.call(partial(_check))  # type: ignore[attr-defined]
    assert codec == "hevc" and preset_id == "saver_1080p"
    assert artifact_name == "示例 - 省流 1080p HEVC 1.5Mbps.mkv"
    # 登记行带音轨：决策引擎按它装配音频计划，播放才有声音
    assert audio_streams == [{"codec": "aac", "channels": 2}]


def test_complete_rejects_wrong_codec_for_preset(client, tmp_path, monkeypatch):
    """产物编码不符合档位（省流档收了 H.264 产物）→ 整件作废。"""
    file_id = _seed_file(client, tmp_path)  # SDR 4K HEVC 源 → 默认省流档
    task_id = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id}).json()["data"]["id"]
    token = _pair_worker(client)
    caps = {
        "encoders": ["h264_nvenc", "hevc_nvenc"],
        "filters": ["scale_cuda"],
        "hw_decoders": ["h264", "hevc"],
    }
    claim = client.post(f"{_WORKER}/claim", json={"caps": caps}, headers=_worker_headers(token))
    task = claim.json()["data"]["task"]
    assert task is not None and "hevc_nvenc" in " ".join(task["argv"]), task

    _upload_segment(client, task)
    monkeypatch.setattr(
        "movieclaw_api.api.routes.preload_worker.probe_media",
        lambda path: MediaSpec(
            resolution="1080p", video_codec="h264", hdr=None, bit_depth=8,
            duration_seconds=7200, bit_rate=3_900_000,
        ),
    )
    done = client.post(
        f"{_WORKER}/tasks/{task_id}/complete", json={}, headers=_worker_headers(token)
    )
    assert done.status_code == 400, done.text
    assert "HEVC" in done.json()["message"], done.text


def test_complete_rejects_artifact_without_audio(client, tmp_path, monkeypatch):
    """产物没有音轨（源有音轨）→ 整件作废：登记出无声片的代价是播放
    没声音（决策引擎按「无音轨」装配），宁可整件重转。"""
    file_id = _seed_file(client, tmp_path)  # 源行带 eac3 音轨
    task_id = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id}).json()["data"]["id"]
    token = _pair_worker(client)
    caps = {
        "encoders": ["h264_nvenc", "hevc_nvenc"],
        "filters": ["scale_cuda"],
        "hw_decoders": ["h264", "hevc"],
    }
    claim = client.post(f"{_WORKER}/claim", json={"caps": caps}, headers=_worker_headers(token))
    task = claim.json()["data"]["task"]
    assert task is not None

    _upload_segment(client, task)
    monkeypatch.setattr(
        "movieclaw_api.api.routes.preload_worker.probe_media",
        lambda path: MediaSpec(
            resolution="1080p", video_codec="hevc", hdr=None, bit_depth=8,
            duration_seconds=7200, bit_rate=1_400_000,
            audio_streams=[],  # 转码链丢了音频
        ),
    )
    done = client.post(
        f"{_WORKER}/tasks/{task_id}/complete", json={}, headers=_worker_headers(token)
    )
    assert done.status_code == 400, done.text
    assert "音轨" in done.json()["message"], done.text


def test_progress_report_persists_payload(client, tmp_path, monkeypatch):
    """进度上报落台账（活动页进行中卡片的数据源）：载荷净化入库、CLAIMED
    翻 RUNNING、租约续约；认不出的字段不进库。"""
    from functools import partial

    file_id = _seed_file(client, tmp_path)
    task_id = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id}).json()["data"]["id"]
    token = _pair_worker(client)
    claim = client.post(f"{_WORKER}/claim", headers=_worker_headers(token))
    assert claim.status_code == 200

    resp = client.post(
        f"{_WORKER}/tasks/{task_id}/progress",
        json={
            "phase": "transcoding",
            "segment_index": 3,
            "segment_total": 14,
            "eta_seconds": 600,
            "downloaded_bytes": 1_000,
            "total_bytes": 2_000,
            "junk": "不该进库",
        },
        headers=_worker_headers(token),
    )
    assert resp.status_code == 200, resp.text

    async def _check() -> PreloadTask:
        async with get_database().session() as session:
            from sqlmodel import select

            from movieclaw_db.models import PreloadTask

            return (
                await session.execute(select(PreloadTask).where(PreloadTask.id == task_id))
            ).scalar_one()

    row = client.portal.call(partial(_check))  # type: ignore[attr-defined]
    assert row.status == PreloadTaskStatus.RUNNING.value
    assert row.progress == {
        "phase": "transcoding",
        "segment_index": 3,
        "segment_total": 14,
        "eta_seconds": 600,
        "downloaded_bytes": 1000,
        "total_bytes": 2000,
    }
    # 任务视图带出进度
    view = client.get(f"{_PRELOAD}/tasks/{task_id}").json()["data"]
    assert view["progress"]["segment_index"] == 3

    # 领取与租约回队清进度：新执行不吃旧执行的最后进度
    resp = client.post(f"{_PRELOAD}/tasks/{task_id}/cancel")
    assert resp.status_code == 200, resp.text


def test_hdr_tonemap_cuda_stays_full_gpu(client, tmp_path):
    """HDR + tonemap_cuda 必须整链留 GPU：-hwaccel_output_format cuda 在位，
    且不再叠加 BT.2020 colorspace 转换（色调映射滤镜本身转 BT.709；曾因转换
    判定强制下载、GPU 滤镜吃到系统内存帧报 -40，真机冒烟抓到）。"""
    file_id = _seed_file(
        client, tmp_path, hdr="HDR10", color_space="BT.2020"
    )
    client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id})
    token = _pair_worker(client)
    caps = {
        "encoders": ["h264_nvenc", "hevc_nvenc"],
        "filters": ["scale_cuda", "hwdownload", "hwupload_cuda", "tonemap_cuda"],
        "hw_decoders": ["h264", "hevc"],
    }
    task = client.post(
        f"{_WORKER}/claim", json={"caps": caps}, headers=_worker_headers(token)
    ).json()["data"]["task"]
    argv = " ".join(task["argv"])
    # 滤镜链里不许出现下载/转换滤镜（输出侧的 -colorspace bt709 标签不在检查内）
    filter_chain = task["argv"][task["argv"].index("-vf") + 1]
    assert "-hwaccel_output_format cuda" in argv, argv
    assert "hwdownload" not in filter_chain and "colorspace=" not in filter_chain, filter_chain
    assert "tonemap_cuda=tonemap=bt2390" in filter_chain and "hevc_nvenc" in argv, argv


def test_source_delete_invalidates_derived(client, tmp_path, monkeypatch):
    """源删除 → 预转码产物直接清理（§6）。"""
    file_id = _seed_file(client, tmp_path, hdr="HDR10")
    task_id = client.post(
        f"{_PRELOAD}/tasks", json={"file_id": file_id, "preset_id": "portable_1080p"}
    ).json()["data"]["id"]
    token = _pair_worker(client)
    _claim_and_upload(client, token, task_id)
    _fake_probe(monkeypatch)
    done = client.post(f"{_WORKER}/tasks/{task_id}/complete", json={}, headers=_worker_headers(token))
    assert done.status_code == 200, done.text
    derived_id = done.json()["data"]["file_id"]

    # 删源（走详情页删除路径：单文件删除服务）
    from movieclaw_api.services.library.recycle import purge_file  # noqa: F401

    async def _delete_source() -> None:
        async with get_database().session() as session:
            source = await session.get(LibraryFile, file_id)
            # 直接走派生物失效（purge_file 的挂钩行为）
            from movieclaw_api.services.preload.service import invalidate_for_source

            await invalidate_for_source(session, file_id)
            await session.delete(source)
            await session.commit()
            assert await session.get(LibraryFile, derived_id) is None

    client.portal.call(partial(_delete_source))  # type: ignore[attr-defined]


def test_fail_and_retry(client, tmp_path):
    file_id = _seed_file(client, tmp_path, hdr="HDR10")
    task_id = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id}).json()["data"]["id"]
    token = _pair_worker(client)
    claim = client.post(f"{_WORKER}/claim", headers=_worker_headers(token))
    assert claim.status_code == 200

    fail = client.post(
        f"{_WORKER}/tasks/{task_id}/fail",
        json={"error": "ffmpeg 退出码 1：测试失败"},
        headers=_worker_headers(token),
    )
    assert fail.status_code == 200
    detail = client.get(f"{_PRELOAD}/tasks/{task_id}").json()["data"]
    assert detail["status"] == PreloadTaskStatus.FAILED.value
    assert "ffmpeg" in detail["error"]

    retry = client.post(f"{_PRELOAD}/tasks/{task_id}/retry")
    assert retry.status_code == 200
    assert retry.json()["data"]["status"] == PreloadTaskStatus.QUEUED.value


def test_cancel_running_task(client, tmp_path):
    file_id = _seed_file(client, tmp_path, hdr="HDR10")
    task_id = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id}).json()["data"]["id"]
    token = _pair_worker(client)
    client.post(f"{_WORKER}/claim", headers=_worker_headers(token))
    cancel = client.post(f"{_PRELOAD}/tasks/{task_id}/cancel")
    assert cancel.status_code == 200
    assert cancel.json()["data"]["status"] == PreloadTaskStatus.CANCELLED.value


def test_task_view_carries_media_identity(client, tmp_path):
    """任务视图带片名与源规格：设置页与活动页不用再对着「源文件 #id」猜。"""
    file_id = _seed_file(client, tmp_path, hdr="HDR10")
    task = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id}).json()["data"]
    assert task["media_title"] == "示例"
    assert task["season_number"] == 0 and task["episode_number"] == 0  # 电影哨兵
    assert task["source_quality"] == "2160p HDR10"
    assert task["updated_at"]

    listed = client.get(f"{_PRELOAD}/tasks").json()["data"]
    view = next(t for t in listed if t["id"] == task["id"])
    assert view["media_title"] == "示例"


def test_cancel_failed_task(client, tmp_path):
    """失败任务可以放弃重试：取消后落到已取消，再次取消明确拒绝。"""
    file_id = _seed_file(client, tmp_path, hdr="HDR10")
    task_id = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id}).json()["data"]["id"]
    token = _pair_worker(client)
    client.post(f"{_WORKER}/claim", headers=_worker_headers(token))
    fail = client.post(
        f"{_WORKER}/tasks/{task_id}/fail",
        json={"error": "boom"},
        headers=_worker_headers(token),
    )
    assert fail.status_code == 200

    cancel = client.post(f"{_PRELOAD}/tasks/{task_id}/cancel")
    assert cancel.status_code == 200
    assert cancel.json()["data"]["status"] == PreloadTaskStatus.CANCELLED.value
    assert client.post(f"{_PRELOAD}/tasks/{task_id}/cancel").status_code == 400


def test_cleanup_stale_preview_and_run(client, tmp_path, monkeypatch):
    """冷数据清理（§6 三件套之二）：预览与执行。"""
    file_id = _seed_file(client, tmp_path, hdr="HDR10")
    task_id = client.post(
        f"{_PRELOAD}/tasks", json={"file_id": file_id, "preset_id": "portable_1080p"}
    ).json()["data"]["id"]
    token = _pair_worker(client)
    _claim_and_upload(client, token, task_id)
    _fake_probe(monkeypatch)
    done = client.post(f"{_WORKER}/tasks/{task_id}/complete", json={}, headers=_worker_headers(token))
    assert done.status_code == 200, done.text
    derived_id = done.json()["data"]["file_id"]

    # 预览（刚生成、从未播过 → 在 90 天阈值内为空）
    preview = client.post(f"{_PRELOAD}/cleanup-stale", json={"days": 90, "confirm": False})
    assert preview.status_code == 200
    assert preview.json()["data"]["count"] == 0

    # 1 天阈值：从未播过的产物按生成时间算，刚生成也不满 1 天？——
    # created_at 是刚刚 → 不超期。把阈值调成 0 天语义不允许（ge=1），
    # 因此直接把产物行的 created_at 改老
    async def _age() -> None:
        from datetime import timedelta

        from movieclaw_db.models.base import utcnow

        async with get_database().session() as session:
            row = await session.get(LibraryFile, derived_id)
            row.created_at = utcnow() - timedelta(days=100)
            session.add(row)
            await session.commit()

    client.portal.call(partial(_age))  # type: ignore[attr-defined]

    preview = client.post(f"{_PRELOAD}/cleanup-stale", json={"days": 90, "confirm": False})
    data = preview.json()["data"]
    assert data["count"] == 1 and data["files"][0]["file_id"] == derived_id

    run = client.post(f"{_PRELOAD}/cleanup-stale", json={"days": 90, "confirm": True})
    assert run.status_code == 200
    assert run.json()["data"]["freed_bytes"] > 0

    async def _gone() -> bool:
        async with get_database().session() as session:
            return await session.get(LibraryFile, derived_id) is None

    assert client.portal.call(partial(_gone))  # type: ignore[attr-defined]
    assert not (
        tmp_path / "preload" / str(task_id) / "示例 - 高清 1080p H.264 4Mbps.mkv"
    ).exists()


def test_artifact_file_name_composition():
    """产物名 = 片名(+年份/剧集号) - 档位 分辨率 编码 码率；Windows 非法字符净化。"""
    from movieclaw_api.services.preload.ffmpeg_preload import PRESETS
    from movieclaw_api.services.preload.service import (
        artifact_file_name,
        sanitize_filename,
    )

    # 电影 + 年份；省流档 = HEVC 1.5Mbps
    movie = artifact_file_name(
        media_title="坠落2：死点", year=2026, kind="movie",
        season_number=0, episode_number=0,  # 电影哨兵不进名字
        preset=PRESETS["saver_1080p"],
    )
    assert movie == "坠落2：死点 (2026) - 省流 1080p HEVC 1.5Mbps.mkv"

    # 剧集：剧名 + SxxEyy；高清档 = H.264 4Mbps
    episode = artifact_file_name(
        media_title="三体", year=2023, kind="tv",
        season_number=1, episode_number=3,
        preset=PRESETS["portable_1080p"],
    )
    assert episode == "三体 (2023) S01E03 - 高清 1080p H.264 4Mbps.mkv"

    # 无年份；半角冒号等 Windows 非法字符折成空格
    plain = artifact_file_name(
        media_title="Movie: The Sequel", year=None, kind="movie",
        season_number=None, episode_number=None,
        preset=PRESETS["saver_1080p"],
    )
    assert plain == "Movie The Sequel - 省流 1080p HEVC 1.5Mbps.mkv"

    assert sanitize_filename("Atlas: Earth <4K>") == "Atlas Earth 4K"
    assert sanitize_filename("   ") == "未命名"


def test_invalidate_orphans_respects_registered_dirs(client, tmp_path):
    """孤儿目录清理按目录归属判定（产物名是动态的）：已登记产物（含旧命名行）
    所在目录不动，无主残留目录清掉。"""
    from movieclaw_api.services.preload import service as _svc

    file_id = _seed_file(client, tmp_path, hdr="HDR10")

    async def _scenario() -> None:
        async with get_database().session() as session:
            source = await session.get(LibraryFile, file_id)
            # 有已登记产物的目录（动态文件名，任务行不存在=任务行已被清理的形态）
            keep_dir = _svc.preload_root() / "777"
            keep_dir.mkdir(parents=True, exist_ok=True)
            keep_file = keep_dir / "示例 - 省流 1080p HEVC 1.5Mbps.mkv"
            keep_file.write_bytes(b"x")
            session.add(
                LibraryFile(
                    library_id=source.library_id,  # type: ignore[union-attr]
                    media_item_id=source.media_item_id,  # type: ignore[union-attr]
                    file_path=str(keep_file),
                    size_bytes=1,
                    source="imported",
                    state=FileState.IN_PLACE,
                    container="mkv",
                    resolution="1080p",
                    derives_from_file_id=file_id,
                    derivation={"kind": "preload", "preset_id": "saver_1080p"},
                )
            )
            # 无主孤儿目录：任务行已消失，里面只有残留文件
            orphan = _svc.preload_root() / "999"
            orphan.mkdir(parents=True, exist_ok=True)
            (orphan / "artifact.mkv").write_bytes(b"x")
            await session.commit()

            removed = await _svc.invalidate_orphans(session)
            await session.commit()
            assert removed == 1

    client.portal.call(partial(_scenario))  # type: ignore[attr-defined]
    assert not (_svc.preload_root() / "999").exists()
    assert (_svc.preload_root() / "777" / "示例 - 省流 1080p HEVC 1.5Mbps.mkv").exists()


# ---------------------------------------------------------------------------
# 状态闸与认领轮次（2026-10-16 审查回归）
# ---------------------------------------------------------------------------


def test_fail_report_ignored_after_cancel(client, tmp_path):
    """取消后迟到的失败上报不能把任务翻成 FAILED（用户看到假失败）。"""
    file_id = _seed_file(client, tmp_path, hdr="HDR10")
    token = _pair_worker(client)
    task = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id}).json()["data"]
    task_id = task["id"]
    claimed = client.post(f"{_WORKER}/claim", headers=_worker_headers(token)).json()["data"]["task"]
    assert claimed is not None and claimed["task_id"] == task_id
    assert client.post(f"{_PRELOAD}/tasks/{task_id}/cancel", json={}).status_code == 200
    resp = client.post(
        f"{_WORKER}/tasks/{task_id}/fail",
        json={"error": "迟到的失败"},
        headers=_worker_headers(token),
    )
    assert resp.status_code == 200
    view = client.get(f"{_PRELOAD}/tasks/{task_id}").json()["data"]
    assert view["status"] == "cancelled"  # 不被翻成 failed


def test_artifact_put_rejected_after_cancel(client, tmp_path):
    """取消后分段上传被拒：在途上传不会重建已被删除的任务目录。"""
    from movieclaw_api.services.preload import service as _svc

    file_id = _seed_file(client, tmp_path, hdr="HDR10")
    token = _pair_worker(client)
    task = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id}).json()["data"]
    task_id = task["id"]
    claimed = client.post(f"{_WORKER}/claim", headers=_worker_headers(token)).json()["data"]["task"]
    artifact_url = "/" + claimed["artifact_url"]
    sep = "&" if "?" in artifact_url else "?"
    assert client.post(f"{_PRELOAD}/tasks/{task_id}/cancel", json={}).status_code == 200
    put = client.put(
        f"{artifact_url}{sep}part=0",
        content=b"x" * 32,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert put.status_code == 400
    assert not (_svc.artifact_dir_for(task_id) / "seg00000.mkv").exists()


def test_artifact_put_rejected_for_stale_claimer(client, tmp_path):
    """凭据绑定认领轮次：任务被重派给别的执行者后，旧 Worker 的长凭据写不进产物。"""
    file_id = _seed_file(client, tmp_path, hdr="HDR10")
    token = _pair_worker(client)
    _pair_worker(client)  # 第二台设备：重派目标（FK 需要真实设备行）
    task = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id}).json()["data"]
    task_id = task["id"]
    claimed = client.post(f"{_WORKER}/claim", headers=_worker_headers(token)).json()["data"]["task"]
    artifact_url = "/" + claimed["artifact_url"]
    sep = "&" if "?" in artifact_url else "?"

    async def _reclaim_to_other_device() -> None:
        from sqlmodel import select

        from movieclaw_db.models import LoginDevice, PreloadTask

        async with get_database().session() as session:
            device_ids = list(
                (await session.execute(select(LoginDevice.id).order_by(LoginDevice.id))).scalars()
            )
            row = (
                await session.execute(select(PreloadTask).where(PreloadTask.id == task_id))
            ).scalar_one()
            # 仍处执行态但认领者换成另一台设备：旧凭据只该被「设备绑定」闸拦下
            row.worker_device_id = device_ids[-1]
            await session.commit()

    client.portal.call(partial(_reclaim_to_other_device))  # type: ignore[attr-defined]
    put = client.put(
        f"{artifact_url}{sep}part=0",
        content=b"x" * 32,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert put.status_code == 400


def test_local_executor_lands_artifact_under_dynamic_name(client, tmp_path, monkeypatch):
    """本地执行器产物必须先改名成动态片名再登记——按协议名登记会被 complete 拦死。"""
    from functools import partial

    from movieclaw_api.services.preload import dispatcher as _dispatcher
    from movieclaw_api.services.preload import service as _svc

    # SDR 源：本地软编链装得出来（HDR 源在无 caps 时 unusable、argv 为空）
    file_id = _seed_file(client, tmp_path)
    task = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id}).json()["data"]
    task_id = task["id"]

    class _FakeProcess:
        returncode = 0

        async def communicate(self):
            # 模拟 ffmpeg 写出协议名产物（argv 尾部的输出路径）
            out = Path(command_holder["argv"][-1])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"FAKE-ARTIFACT" * 64)
            return b"", b""

    command_holder: dict = {}

    async def _fake_exec(*argv, **kwargs):  # type: ignore[no-untyped-def]
        command_holder["argv"] = list(argv)
        return _FakeProcess()

    monkeypatch.setattr(_dispatcher.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(
        "movieclaw_api.services.media_probe.probe_media",
        lambda path: MediaSpec(
            resolution="1080p", video_codec="hevc", hdr=None, bit_depth=8,
            duration_seconds=7200, bit_rate=1_500_000,
            # 登记闸：产物没有音轨（源有）整件作废
            audio_streams=[{"codec": "aac"}],
        ),
    )

    async def _claim_and_run() -> None:
        from sqlmodel import select

        from movieclaw_db.models import PreloadTask

        async with get_database().session() as session:
            claimed = await _svc.claim_next_task(session, device_id=None)
            assert claimed is not None
            task_row, file_row = claimed
            command = _svc.build_task_command(task_row, file_row, worker_caps=None)
            await _dispatcher._execute_local_task(task_row.id, file_row, command["argv"])
        async with get_database().session() as session:
            row = (
                await session.execute(select(PreloadTask).where(PreloadTask.id == task_id))
            ).scalar_one()
            assert row.status == PreloadTaskStatus.COMPLETED, row.error

    client.portal.call(partial(_claim_and_run))  # type: ignore[attr-defined]
    # 产物以动态片名落盘且已登记；协议名不残留
    directory = _svc.artifact_dir_for(task_id)
    dynamic = directory / "示例 - 省流 1080p HEVC 1.5Mbps.mkv"
    assert dynamic.exists()
    assert not (directory / "artifact.mkv").exists()
    view = client.get(f"{_PRELOAD}/tasks/{task_id}").json()["data"]
    assert view["status"] == "completed"
    assert view["result_file_id"]
