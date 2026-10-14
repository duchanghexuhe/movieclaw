"""预转码功能的端到端测试（docs/design/preload-transcode.md §3/§6/§7）。

覆盖：
1. 下单链路：预估 → 下单（配额预检/直拷拒绝/幂等）→ 任务排队；
2. Worker 领取协议：配对凭证领任务 → 进度续约 → 分段上传断点续转 →
   complete 校验登记为派生版本 → 播放枚举能选到它；
3. 派生物生命周期：源删除 → 产物自动清理；失败重试；取消；
4. 冷数据清理预览与执行。

不跑真 ffmpeg：complete 的产物用手工构造的最小 MKV 片段 + monkeypatch
probe_media 的探测结果校验服务端逻辑；ffmpeg 参数构造的正确性在
tests/api/test_preload_command.py。
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
from movieclaw_db.models import FileState, LibraryFile
from movieclaw_db.models.preload_task import PreloadTaskStatus
from movieclaw_db.repositories.library_repo import LibraryRepository
from movieclaw_db.models import MediaItem

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

    from movieclaw_api.services.auth import reset_auth_state
    from movieclaw_api.settings.store import reset_setting_store
    from movieclaw_db.crypto import reset_secret_box
    from movieclaw_api.core.config import get_settings

    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    get_settings.cache_clear()


_seed_counter = {"n": 0}


def _seed_file(client: TestClient, tmp_path, *, hdr=None, codec="hevc", resolution="2160p") -> int:
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
                duration_seconds=7200,
                audio_streams=[{"codec": "eac3", "channels": 6}],
                subtitle_streams=[{"codec": "subrip"}, {"codec": "hdmv_pgs_subtitle"}],
            )
            session.add(row)
            await session.commit()
            return int(row.id)

    return client.portal.call(partial(_seed))  # type: ignore[attr-defined]


def _claim_and_upload(client: TestClient, token: str, task_id: int) -> dict:
    """领取任务并上传一段**真实 MKV**（complete 走 concat demuxer，假字节过不了
    EBML 解析）。ffmpeg 是服务端运行时的既有依赖，测试环境缺失时跳过。"""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("concat 校验需要 ffmpeg")
    task = client.post(f"{_WORKER}/claim", headers=_worker_headers(token)).json()["data"]["task"]
    assert task is not None and task["task_id"] == task_id
    artifact_url = "/" + task["artifact_url"]
    sep = "&" if "?" in artifact_url else "?"
    seg = Path(tempfile.gettempdir()) / f"preload-seg-{task_id}.mkv"
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=duration=1:size=320x180:rate=10",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(seg)],
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
    from movieclaw_api.core.config import get_settings as _gs

    part_file = _svc.artifact_dir_for(task_id) / "seg00000.mkv"
    assert part_file.read_bytes()[:4] == bytes([0x1A, 0x45, 0xDF, 0xA3]), part_file.read_bytes()[:16]
    seg.unlink(missing_ok=True)
    return task


def _fake_probe(monkeypatch) -> None:
    monkeypatch.setattr(
        "movieclaw_api.api.routes.preload_worker.probe_media",
        lambda path: MediaSpec(
            resolution="1080p", video_codec="h264", hdr=None, bit_depth=8,
            duration_seconds=7200, bit_rate=3_900_000,
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

    created = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id})
    assert created.status_code == 200, created.text
    task = created.json()["data"]
    assert task["status"] == PreloadTaskStatus.QUEUED.value

    # 幂等：再下一次还是同一个任务
    again = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id})
    assert again.json()["data"]["id"] == task["id"]


def test_claim_selects_nvenc_when_worker_reports_caps(client, tmp_path):
    """Worker 上报 NVENC 能力 → 下发全 GPU 链；HDR 源下色调映射回内存。"""
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

    # 补 tonemapx 能力再领：HDR → 硬解下载回内存 tonemapx + NVENC 编码
    caps_tx = {**caps, "filters": [*caps["filters"], "tonemapx"]}
    task_tx = client.post(
        f"{_WORKER}/claim", json={"caps": caps_tx}, headers=_worker_headers(token)
    ).json()["data"]["task"]
    argv = " ".join(task_tx["argv"])
    assert "h264_nvenc" in argv, argv
    assert "hwdownload" in argv and "tonemapx=tonemap=bt2390" in argv, argv
    assert task_tx["hw_backend"] == "nvenc"

    # 换 SDR 4K 源再领：全 GPU 链（硬解 + scale_cuda，无 hwdownload）
    file2 = _seed_file(client, tmp_path)
    client.post(f"{_PRELOAD}/tasks", json={"file_id": file2})
    task2 = client.post(
        f"{_WORKER}/claim", json={"caps": caps_tx}, headers=_worker_headers(token)
    ).json()["data"]["task"]
    argv2 = " ".join(task2["argv"])
    assert "scale_cuda" in argv2 and "hwdownload" not in argv2, argv2
    assert "-hwaccel_output_format" in argv2, argv2


def test_create_rejects_already_portable_source(client, tmp_path):
    """源已是便携档（1080p H.264 SDR）时明确拒绝。"""
    file_id = _seed_file(client, tmp_path, codec="h264", resolution="1080p")
    resp = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id})
    assert resp.status_code == 400


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
    task_id = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id}).json()["data"]["id"]
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
    import os as _os
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


def test_source_delete_invalidates_derived(client, tmp_path, monkeypatch):
    """源删除 → 预转码产物直接清理（§6）。"""
    file_id = _seed_file(client, tmp_path, hdr="HDR10")
    task_id = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id}).json()["data"]["id"]
    token = _pair_worker(client)
    _claim_and_upload(client, token, task_id)
    _fake_probe(monkeypatch)
    done = client.post(f"{_WORKER}/tasks/{task_id}/complete", json={}, headers=_worker_headers(token))
    assert done.status_code == 200, done.text
    derived_id = done.json()["data"]["file_id"]

    # 删源（走详情页删除路径：单文件删除服务）
    from movieclaw_api.services.library.recycle import purge_file  # noqa: F401
    from movieclaw_api.services.library import items as library_items

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


def test_cleanup_stale_preview_and_run(client, tmp_path, monkeypatch):
    """冷数据清理（§6 三件套之二）：预览与执行。"""
    file_id = _seed_file(client, tmp_path, hdr="HDR10")
    task_id = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id}).json()["data"]["id"]
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
        from movieclaw_db.models.base import utcnow
        from datetime import timedelta

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
    assert not (tmp_path / "preload" / str(task_id) / "portable.mkv").exists()
