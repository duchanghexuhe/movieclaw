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

import asyncio
import json
import shutil
import subprocess
import tempfile
import time
from functools import partial
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from movieclaw_api.services.media_probe import MediaSpec
from movieclaw_db.engine import get_database
from movieclaw_db.models import FileState, LibraryFile, MediaItem, PreloadTask
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


def _write_media(path: Path, workdir: Path) -> None:
    """造源文件：完成上报要从源文件内封字幕，所以源必须是真媒体（1 秒视频 + 音轨 + 一条 SRT）。

    没有 ffmpeg 时退回假字节——只给不走完成链路的用例用（完成链路本身都要求 ffmpeg）。
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        path.write_bytes(b"FAKE-MEDIA-BYTES" * 256)
        return
    srt = workdir / "seed.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:00,500\nseed\n", encoding="utf-8")
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=10:duration=1",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-i", str(srt), "-map", "0:v", "-map", "1:a", "-map", "2:s",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-c:s", "srt", str(path)],
        check=True, timeout=60,
    )


def _seed_file(
    client: TestClient, tmp_path, *, hdr=None, codec="hevc", resolution="2160p",
    color_space: str | None = None, in_dir: bool = False,
) -> int:
    """造一个源文件行（4K HEVC HDR 是预转码的典型对象）。

    ``in_dir``：条目住在自己的刮削目录里（NFO/海报与视频同目录），整条目删除才会整目录清掉。
    """
    _seed_counter["n"] += 1
    n = _seed_counter["n"]

    async def _seed() -> int:
        media_root = tmp_path / "movies"
        media_root.mkdir(exist_ok=True)
        path = media_root / f"movie{n}.mkv"
        if in_dir:
            path = media_root / f"Movie {n} (2026)" / f"movie{n}.mkv"
            path.parent.mkdir(parents=True, exist_ok=True)
            (path.parent / "movie.nfo").write_text("<movie/>", encoding="utf-8")
            (path.parent / "poster.jpg").write_bytes(b"POSTER")
        _write_media(path, tmp_path)
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
                subtitle_streams=[{"codec": "subrip"}],
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
    assert part_file.read_bytes()[:4] == bytes([0x1A, 0x45, 0xDF, 0xA3]), (
        part_file.read_bytes()[:16]
    )
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


def _set_format(client: TestClient, fmt: str) -> None:
    """切全局预转码格式（设置端点）：测 HEVC 链路用（默认格式是 H.264）。"""
    resp = client.put(f"{_PRELOAD}/settings", json={"video_format": fmt})
    assert resp.status_code == 200, resp.text


def test_estimate_and_create_task(client, tmp_path):
    file_id = _seed_file(client, tmp_path, hdr="HDR10")
    est = client.post(f"{_PRELOAD}/estimate", json={"file_id": file_id})
    assert est.status_code == 200, est.text
    data = est.json()["data"]
    assert data["estimated_bytes"] > 0
    assert data["quota_bytes"] > 0

    # 省流是默认档；同片源高清档的预估必然更大（码率帽 3M vs 1.5M）
    premium = client.post(
        f"{_PRELOAD}/estimate", json={"file_id": file_id, "preset_id": "premium"}
    )
    assert premium.json()["data"]["estimated_bytes"] > data["estimated_bytes"]

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
    assert task["preset_id"] == "saver"

    # 幂等：再下一次还是同一个任务
    again = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id})
    assert again.json()["data"]["id"] == task["id"]


def test_format_setting_drives_presets_estimate_and_snapshot(client, tmp_path):
    """格式是全局设置：默认 h264；切 hevc 后档位标题随格式变、高清档预估变
    大（4M vs 3M）；下单把格式快照到任务行——切回设置后，在途任务的命令
    仍按快照格式装配。"""
    file_id = _seed_file(client, tmp_path, hdr="HDR10")

    presets = client.get(f"{_PRELOAD}/presets").json()["data"]
    assert [(p["id"], p["short"]) for p in presets] == [
        ("saver", "省流"),
        ("premium", "高清"),
    ]
    assert "H.264 · 480p" in presets[0]["title"], presets
    est_premium_h264 = client.post(
        f"{_PRELOAD}/estimate", json={"file_id": file_id, "preset_id": "premium"}
    ).json()["data"]["estimated_bytes"]

    assert client.put(
        f"{_PRELOAD}/settings", json={"video_format": "hevc"}
    ).status_code == 200
    presets = client.get(f"{_PRELOAD}/presets").json()["data"]
    assert "HEVC · 1080p" in presets[0]["title"], presets
    est_premium_hevc = client.post(
        f"{_PRELOAD}/estimate", json={"file_id": file_id, "preset_id": "premium"}
    ).json()["data"]["estimated_bytes"]
    assert est_premium_hevc > est_premium_h264  # 4M vs 3M

    # 旧档位 ID（迁移前的注册表）明确拒绝
    assert client.post(
        f"{_PRELOAD}/tasks", json={"file_id": file_id, "preset_id": "saver_1080p"}
    ).status_code == 400

    # 下单快照：hevc 时代下的任务，切回 h264 后命令仍按 hevc 装配
    task_id = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id}).json()["data"]["id"]
    assert client.put(
        f"{_PRELOAD}/settings", json={"video_format": "h264"}
    ).status_code == 200
    token = _pair_worker(client)
    caps = {
        "encoders": ["h264_nvenc", "hevc_nvenc"],
        "filters": ["scale_cuda", "tonemapx"],
        "hw_decoders": ["h264", "hevc"],
    }
    claimed = client.post(
        f"{_WORKER}/claim", json={"caps": caps}, headers=_worker_headers(token)
    ).json()["data"]["task"]
    assert claimed is not None and claimed["task_id"] == task_id
    argv = " ".join(claimed["argv"])
    assert "hevc_nvenc" in argv and "h264_nvenc" not in argv, argv


def test_settings_endpoints_roundtrip(client):
    """格式设置读写：默认 h264；非法值 422；保存后读取一致。"""
    data = client.get(f"{_PRELOAD}/settings").json()["data"]
    assert data == {"video_format": "h264"}

    assert client.put(
        f"{_PRELOAD}/settings", json={"video_format": "hevc"}
    ).status_code == 200
    assert client.get(f"{_PRELOAD}/settings").json()["data"] == {"video_format": "hevc"}

    assert client.put(
        f"{_PRELOAD}/settings", json={"video_format": "mpeg2"}
    ).status_code == 422
    assert client.put(
        f"{_PRELOAD}/settings", json={"video_format": "h264"}
    ).status_code == 200


def test_claim_selects_nvenc_when_worker_reports_caps(client, tmp_path):
    """Worker 上报 NVENC 能力 → 下发全 GPU 链：编码器随格式（默认 H.264）、
    码率帽随档位；HDR 源下色调映射回内存。"""
    file_id = _seed_file(client, tmp_path, hdr="HDR10")
    client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id})
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

    # 补 tonemapx 能力再领：省流档（H.264 默认）→ 硬解下载回内存 tonemapx
    # + H.264 NVENC，码率帽 1.5M
    caps_tx = {**caps, "filters": [*caps["filters"], "tonemapx"]}
    task_tx = client.post(
        f"{_WORKER}/claim", json={"caps": caps_tx}, headers=_worker_headers(token)
    ).json()["data"]["task"]
    argv = " ".join(task_tx["argv"])
    assert "h264_nvenc" in argv and "hevc_nvenc" not in argv, argv
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
    assert "-hwaccel_output_format" in argv2 and "h264_nvenc" in argv2, argv2

    # 同源下高清档：h264_nvenc + 3M 码率帽
    client.post(
        f"{_PRELOAD}/tasks", json={"file_id": file2, "preset_id": "premium"}
    )
    task3 = client.post(
        f"{_WORKER}/claim", json={"caps": caps_tx}, headers=_worker_headers(token)
    ).json()["data"]["task"]
    argv3 = " ".join(task3["argv"])
    assert "h264_nvenc" in argv3 and "hevc_nvenc" not in argv3, argv3
    assert task3["argv"][task3["argv"].index("-maxrate") + 1] == "3M", task3["argv"]

    # 切 HEVC 格式再下省流单（新源：同源同档会幂等撞 task2）：编码器随格式
    assert client.put(
        f"{_PRELOAD}/settings", json={"video_format": "hevc"}
    ).status_code == 200
    file3 = _seed_file(client, tmp_path)
    client.post(f"{_PRELOAD}/tasks", json={"file_id": file3})
    task4 = client.post(
        f"{_WORKER}/claim", json={"caps": caps_tx}, headers=_worker_headers(token)
    ).json()["data"]["task"]
    argv4 = " ".join(task4["argv"])
    assert "hevc_nvenc" in argv4 and "h264_nvenc" not in argv4, argv4
    assert task4["argv"][task4["argv"].index("-maxrate") + 1] == "1.5M", task4["argv"]


def test_create_rejects_already_portable_source(client, tmp_path):
    """高清档：源已是便携档（≤档位高度的 H.264 SDR）时明确拒绝；省流档
    （更低的 480p 档）仍可下单——源比档位高，有要转的东西。"""
    file_id = _seed_file(client, tmp_path, codec="h264", resolution="720p")
    resp = client.post(
        f"{_PRELOAD}/tasks", json={"file_id": file_id, "preset_id": "premium"}
    )
    assert resp.status_code == 400

    saver = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id})
    assert saver.status_code == 200, saver.text
    assert saver.json()["data"]["preset_id"] == "saver"


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
        f"{_PRELOAD}/tasks", json={"file_id": file_id, "preset_id": "premium"}
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
        f"{artifact_url}{sep}part=0",
        content=part_a,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert first.status_code == 201, (first.status_code, first.text)
    gap = client.put(
        f"{artifact_url}{sep}part=2",
        content=b"B" * 8,
        headers={"Content-Type": "application/octet-stream"},
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
    done = client.post(
        f"{_WORKER}/tasks/{task_id}/complete", json={}, headers=_worker_headers(token)
    )
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
    assert derived.derivation["preset_id"] == "premium"
    # 登记行带产物流探测结果：决策引擎按它装配音轨/字幕轨——不填就是
    # 「无音轨」转码计划，播放没声音没字幕
    assert derived.audio_streams == [{"codec": "aac"}]
    assert derived.subtitle_streams == [{"codec": "subrip"}]
    assert derived.bit_rate == 3_900_000
    # 产物文件名带片名与码率（版本列表/下载直接显示）
    assert Path(derived.file_path).name == "示例 - 高清 720p H.264 3Mbps.mkv"


def test_saver_preset_roundtrip_hevc(client, tmp_path, monkeypatch):
    """省流档 HEVC 格式全链路：HEVC NVENC 链 + complete 登记 hevc 派生版本。"""
    _set_format(client, "hevc")
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
    assert codec == "hevc" and preset_id == "saver"
    assert artifact_name == "示例 - 省流 1080p HEVC 1.5Mbps.mkv"
    # 登记行带音轨：决策引擎按它装配音频计划，播放才有声音
    assert audio_streams == [{"codec": "aac", "channels": 2}]


def test_complete_rejects_wrong_codec_for_preset(client, tmp_path, monkeypatch):
    """产物编码不符合档位（HEVC 省流档收了 H.264 产物）→ 整件作废。"""
    _set_format(client, "hevc")
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
            resolution="1080p", video_codec="h264", hdr=None, bit_depth=8,
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
    assert "tonemap_cuda=tonemap=bt2390" in filter_chain and "h264_nvenc" in argv, argv


def test_source_delete_invalidates_derived(client, tmp_path, monkeypatch):
    """源删除 → 预转码产物直接清理（§6）。"""
    file_id = _seed_file(client, tmp_path, hdr="HDR10")
    task_id = client.post(
        f"{_PRELOAD}/tasks", json={"file_id": file_id, "preset_id": "premium"}
    ).json()["data"]["id"]
    token = _pair_worker(client)
    _claim_and_upload(client, token, task_id)
    _fake_probe(monkeypatch)
    done = client.post(
        f"{_WORKER}/tasks/{task_id}/complete", json={}, headers=_worker_headers(token)
    )
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
        f"{_PRELOAD}/tasks", json={"file_id": file_id, "preset_id": "premium"}
    ).json()["data"]["id"]
    token = _pair_worker(client)
    _claim_and_upload(client, token, task_id)
    _fake_probe(monkeypatch)
    done = client.post(
        f"{_WORKER}/tasks/{task_id}/complete", json={}, headers=_worker_headers(token)
    )
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
        tmp_path / "preload" / str(task_id) / "示例 - 高清 720p H.264 3Mbps.mkv"
    ).exists()


def test_artifact_file_name_composition():
    """产物名 = 片名(+年份/剧集号) - 档位 分辨率 编码 码率；Windows 非法字符净化。"""
    from movieclaw_api.services.preload.ffmpeg_preload import effective_preset
    from movieclaw_api.services.preload.service import (
        artifact_file_name,
        sanitize_filename,
    )

    # 电影 + 年份；省流档（HEVC）= 1080p 1.5Mbps
    movie = artifact_file_name(
        media_title="坠落2：死点", year=2026, kind="movie",
        season_number=0, episode_number=0,  # 电影哨兵不进名字
        preset=effective_preset("saver", "hevc"),
    )
    assert movie == "坠落2：死点 (2026) - 省流 1080p HEVC 1.5Mbps.mkv"

    # 剧集：剧名 + SxxEyy；高清档（H.264）= 720p 3Mbps
    episode = artifact_file_name(
        media_title="三体", year=2023, kind="tv",
        season_number=1, episode_number=3,
        preset=effective_preset("premium", "h264"),
    )
    assert episode == "三体 (2023) S01E03 - 高清 720p H.264 3Mbps.mkv"

    # 无年份；半角冒号等 Windows 非法字符折成空格
    plain = artifact_file_name(
        media_title="Movie: The Sequel", year=None, kind="movie",
        season_number=None, episode_number=None,
        preset=effective_preset("saver", "hevc"),
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
                    derivation={"kind": "preload", "preset_id": "saver"},
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


def test_finished_tasks_pruned_to_keep(client, tmp_path):
    """终态任务台账只保留最近 N 条：下单时顺手清理最老的，活跃任务不动。"""
    from movieclaw_api.services.preload.service import FINISHED_TASK_KEEP

    file_id = _seed_file(client, tmp_path, hdr="HDR10")
    seeded: dict = {}

    async def _seed_history() -> None:
        from datetime import timedelta

        from movieclaw_db.models import PreloadTask, PreloadTaskStatus, utcnow

        async with get_database().session() as session:
            # N+1 条已完成（created_at 依次错开 1 分钟，最老的一条可预期被清）
            ids: list[int] = []
            for i in range(FINISHED_TASK_KEEP + 1):
                row = PreloadTask(
                    target_file_id=file_id,
                    preset_id="saver",
                    status=PreloadTaskStatus.COMPLETED,
                    created_at=utcnow() - timedelta(minutes=i + 1),
                    updated_at=utcnow() - timedelta(minutes=i + 1),
                )
                session.add(row)
                await session.flush()
                ids.append(int(row.id))
            # 排队中的不受清理影响；档位不同避开下单幂等去重
            session.add(
                PreloadTask(
                    target_file_id=file_id,
                    preset_id="premium",
                    status=PreloadTaskStatus.QUEUED,
                )
            )
            await session.commit()
            seeded["oldest_id"] = ids[-1]
            seeded["newest_ids"] = ids[:-1]

    client.portal.call(partial(_seed_history))  # type: ignore[attr-defined]

    resp = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id})
    assert resp.status_code == 200

    async def _counts() -> tuple[list[int], int]:
        from sqlmodel import select

        from movieclaw_db.models import PreloadTask, PreloadTaskStatus

        async with get_database().session() as session:
            completed = (
                await session.execute(
                    select(PreloadTask).where(
                        PreloadTask.status == PreloadTaskStatus.COMPLETED
                    )
                )
            ).scalars().all()
            active = (
                await session.execute(
                    select(PreloadTask).where(PreloadTask.status == PreloadTaskStatus.QUEUED)
                )
            ).scalars().all()
            return sorted(int(t.id) for t in completed), len(active)

    completed_ids, active_count = client.portal.call(partial(_counts))  # type: ignore[attr-defined]
    assert len(completed_ids) == FINISHED_TASK_KEEP
    assert seeded["oldest_id"] not in completed_ids
    assert all(i in completed_ids for i in seeded["newest_ids"])
    assert active_count == 2  # 排队中的（旧 1 条 + 新下单 1 条）不被清理


# ---------------------------------------------------------------------------
# 生命周期回归：删除入口 / 整条目删除 / 任务与产物对账 / 失败重试 / 配额 / HEVC / 冷数据预览
# ---------------------------------------------------------------------------


@pytest.fixture
def no_local(monkeypatch):  # type: ignore[no-untyped-def]
    """本地执行器不抢单：硬件探测置空（测试只走 Worker 数据面，避免后台协程改写台账）。"""
    monkeypatch.setattr(
        "movieclaw_api.services.playback.hwprobe.available_local_backends", lambda: []
    )


def _ids_of(client: TestClient, file_id: int) -> tuple[int, int]:
    """源文件所在的 (library_id, media_item_id)。"""

    async def _load() -> tuple[int, int]:
        async with get_database().session() as session:
            row = await session.get(LibraryFile, file_id)
            assert row is not None
            return int(row.library_id), int(row.media_item_id)

    return client.portal.call(partial(_load))  # type: ignore[attr-defined]


def _path_of(client: TestClient, row_id: int) -> Path:
    async def _load() -> Path:
        async with get_database().session() as session:
            row = await session.get(LibraryFile, row_id)
            assert row is not None
            return Path(row.file_path)

    return client.portal.call(partial(_load))  # type: ignore[attr-defined]


def _row_exists(client: TestClient, row_id: int) -> bool:
    async def _check() -> bool:
        async with get_database().session() as session:
            return await session.get(LibraryFile, row_id) is not None

    return client.portal.call(partial(_check))  # type: ignore[attr-defined]


def _age_row(client: TestClient, row_id: int, *, days: int) -> None:
    async def _age() -> None:
        from datetime import timedelta

        from movieclaw_db.models.base import utcnow

        async with get_database().session() as session:
            row = await session.get(LibraryFile, row_id)
            row.created_at = utcnow() - timedelta(days=days)
            session.add(row)
            await session.commit()

    client.portal.call(partial(_age))  # type: ignore[attr-defined]


def _task_row(client: TestClient, task_id: int) -> dict:
    return client.get(f"{_PRELOAD}/tasks/{task_id}").json()["data"]


def _make_product(
    client: TestClient, monkeypatch, token: str, file_id: int, *, preset_id: str = "premium"
) -> tuple[int, int]:
    """下单 → Worker 领取上传 → complete 登记；返回 (任务 id, 产物行 id)。"""
    task_id = client.post(
        f"{_PRELOAD}/tasks", json={"file_id": file_id, "preset_id": preset_id}
    ).json()["data"]["id"]
    _claim_and_upload(client, token, task_id)
    _fake_probe(monkeypatch)
    done = client.post(
        f"{_WORKER}/tasks/{task_id}/complete", json={}, headers=_worker_headers(token)
    )
    assert done.status_code == 200, done.text
    return task_id, done.json()["data"]["file_id"]


def test_portable_version_deletes_via_library_route(client, tmp_path, monkeypatch, no_local):
    """详情页删便携版本：库的单文件删除路由认派生行（产物在 data/preload 下、不在库根里，
    原先报「不在库根路径之内」删不掉）；删完挂着它的已完成任务转已失效。"""
    file_id = _seed_file(client, tmp_path, hdr="HDR10")
    token = _pair_worker(client)
    task_id, derived_id = _make_product(client, monkeypatch, token, file_id)
    library_id, item_id = _ids_of(client, file_id)
    product = _path_of(client, derived_id)
    assert product.is_file()

    preview = client.get(
        f"/api/v1/libraries/{library_id}/items/{item_id}/delete-preview",
        params={"file_id": derived_id},
    )
    assert preview.status_code == 200, preview.text
    assert str(product) in preview.json()["data"]["plan"]["removed_paths"]

    resp = client.delete(f"/api/v1/libraries/{library_id}/items/{item_id}/files/{derived_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["errors"] == [], resp.text
    assert not product.exists()
    assert not _row_exists(client, derived_id)
    assert _task_row(client, task_id)["status"] == PreloadTaskStatus.INVALIDATED.value


def test_whole_item_delete_skips_portable_rows(client, tmp_path, monkeypatch, no_local):
    """整条目删除：预转码版本不再逐个报「部分删除失败」，源行删除时它随之失效清理；
    两个产物并存时也不能再冒 SQLAlchemy 的「expected to delete N row(s)」告警。"""
    import warnings

    file_id = _seed_file(client, tmp_path, hdr="HDR10", in_dir=True)
    token = _pair_worker(client)
    _, premium_id = _make_product(client, monkeypatch, token, file_id, preset_id="premium")
    _, saver_id = _make_product(client, monkeypatch, token, file_id, preset_id="saver")
    library_id, item_id = _ids_of(client, file_id)
    source_dir = _path_of(client, file_id).parent
    products = [_path_of(client, premium_id), _path_of(client, saver_id)]
    assert all(p.is_file() for p in products)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        resp = client.delete(f"/api/v1/libraries/{library_id}/items/{item_id}")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["errors"] == [], data
    assert not source_dir.exists()
    assert not any(p.exists() for p in products)
    assert not _row_exists(client, premium_id) and not _row_exists(client, saver_id)
    messages = [str(w.message) for w in caught]
    assert not [m for m in messages if "expected to delete" in m], messages


def test_last_native_file_delete_upgrades_despite_portable(
    client, tmp_path, monkeypatch, no_local
):
    """条目只剩一个原生文件时删它 = 整条目删除：派生的预转码版本不计入「还剩几个文件」，
    否则会留下只剩 NFO/海报的空刮削目录。"""
    file_id = _seed_file(client, tmp_path, hdr="HDR10", in_dir=True)
    token = _pair_worker(client)
    _, derived_id = _make_product(client, monkeypatch, token, file_id)
    library_id, item_id = _ids_of(client, file_id)
    source_dir = _path_of(client, file_id).parent

    resp = client.delete(f"/api/v1/libraries/{library_id}/items/{item_id}/files/{file_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["errors"] == [], resp.text
    assert not source_dir.exists()  # NFO/海报一并清掉
    assert not _row_exists(client, derived_id)


def test_deleting_product_invalidates_task_and_allows_reorder(
    client, tmp_path, monkeypatch, no_local
):
    """删产物后任务转已失效（源行不再显示「转码完成」）；之后可以重新下单转码。"""
    file_id = _seed_file(client, tmp_path, hdr="HDR10")
    token = _pair_worker(client)
    task_id, derived_id = _make_product(client, monkeypatch, token, file_id)

    resp = client.delete(f"{_PRELOAD}/files/{derived_id}")
    assert resp.status_code == 200, resp.text
    task = _task_row(client, task_id)
    assert task["status"] == PreloadTaskStatus.INVALIDATED.value
    assert task["result_file_id"] is None
    assert "可重新转码" in task["error"]

    reorder = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id, "preset_id": "premium"})
    assert reorder.status_code == 200, reorder.text
    assert reorder.json()["data"]["status"] == PreloadTaskStatus.QUEUED.value
    assert reorder.json()["data"]["id"] != task_id


def test_cold_cleanup_preview_lists_titles_and_invalidates_task(
    client, tmp_path, monkeypatch, no_local
):
    """冷数据清理：预览带片名、季集号、体积与未播天数（不再只有数量）；执行后任务转已失效。"""
    file_id = _seed_file(client, tmp_path, hdr="HDR10")
    token = _pair_worker(client)
    task_id, derived_id = _make_product(client, monkeypatch, token, file_id)
    _age_row(client, derived_id, days=100)

    preview = client.post(f"{_PRELOAD}/cleanup-stale", json={"days": 90, "confirm": False})
    data = preview.json()["data"]
    assert data["count"] == 1
    entry = data["files"][0]
    assert entry["file_id"] == derived_id
    assert entry["title"] == "示例"
    assert entry["season_number"] == 0 and entry["episode_number"] == 0
    assert entry["size_bytes"] > 0 and entry["days_since_play"] >= 90

    run = client.post(f"{_PRELOAD}/cleanup-stale", json={"days": 90, "confirm": True})
    assert run.status_code == 200, run.text
    assert not _row_exists(client, derived_id)
    task = _task_row(client, task_id)
    assert task["status"] == PreloadTaskStatus.INVALIDATED.value
    assert task["result_file_id"] is None


def test_failed_complete_voids_artifact_so_retry_reruns(client, tmp_path, monkeypatch, no_local):
    """complete 校验失败 → 分段与断点台账整件作废，重试从头转。

    原先分段已删而断点台账保留：重领时 Worker 跳过全部段直接 complete，
    永远卡在「没有收到任何产物分段」。
    """
    from movieclaw_api.services.preload import service as _svc

    file_id = _seed_file(client, tmp_path, hdr="HDR10")
    task_id = client.post(
        f"{_PRELOAD}/tasks", json={"file_id": file_id, "preset_id": "premium"}
    ).json()["data"]["id"]
    token = _pair_worker(client)
    _claim_and_upload(client, token, task_id)  # 段 0 已收，断点 resume_parts = 1
    # 产物时长与源偏差 >5%：登记闸拒收
    monkeypatch.setattr(
        "movieclaw_api.api.routes.preload_worker.probe_media",
        lambda path: MediaSpec(
            resolution="720p", video_codec="h264", hdr=None, bit_depth=8,
            duration_seconds=3600, bit_rate=3_000_000,
            audio_streams=[{"codec": "aac"}], subtitle_streams=[{"codec": "subrip"}],
        ),
    )
    done = client.post(
        f"{_WORKER}/tasks/{task_id}/complete", json={}, headers=_worker_headers(token)
    )
    assert done.status_code == 400, done.text
    assert _task_row(client, task_id)["resume_parts"] == 0
    assert not list(_svc.artifact_dir_for(task_id).glob("seg*.mkv"))

    failed = client.post(
        f"{_WORKER}/tasks/{task_id}/fail",
        json={"error": "产物时长与源偏差"},
        headers=_worker_headers(token),
    )
    assert failed.status_code == 200
    assert client.post(f"{_PRELOAD}/tasks/{task_id}/retry").status_code == 200

    # 重领：断点已归零，从段 0 重转上传（原先 resume_parts=1，跳过段后必败）
    claimed = client.post(f"{_WORKER}/claim", headers=_worker_headers(token)).json()["data"]["task"]
    assert claimed["task_id"] == task_id and claimed["resume_parts"] == 0
    _upload_segment(client, claimed)
    _fake_probe(monkeypatch)
    done = client.post(
        f"{_WORKER}/tasks/{task_id}/complete", json={}, headers=_worker_headers(token)
    )
    assert done.status_code == 200, done.text
    assert _task_row(client, task_id)["status"] == PreloadTaskStatus.COMPLETED.value


def test_quota_precheck_counts_inflight_tasks(client, tmp_path, monkeypatch, no_local):
    """配额预检算上在途任务：配额是单子预估的 1.5 倍时，第二单（哪怕是另一部片）必须被拒。"""
    from movieclaw_api.settings import PreloadSetting
    from movieclaw_api.settings.store import get_setting_store

    first = _seed_file(client, tmp_path, hdr="HDR10")
    second = _seed_file(client, tmp_path, hdr="HDR10")
    est = client.post(f"{_PRELOAD}/estimate", json={"file_id": first}).json()["data"][
        "estimated_bytes"
    ]
    quota = int(est * 1.5)

    async def _set_quota() -> None:
        await get_setting_store().set(PreloadSetting(quota_bytes=quota))

    client.portal.call(partial(_set_quota))  # type: ignore[attr-defined]
    assert client.post(f"{_PRELOAD}/tasks", json={"file_id": first}).status_code == 200
    # 第一单还在排队、尚未登记产物：估价接口的剩余配额要扣掉它
    free = client.post(f"{_PRELOAD}/estimate", json={"file_id": second}).json()["data"][
        "free_bytes"
    ]
    assert free == quota - est
    over = client.post(f"{_PRELOAD}/tasks", json={"file_id": second})
    assert over.status_code == 409, over.text
    assert over.json()["code"] == "PRELOAD_QUOTA_EXCEEDED"


def test_hevc_task_only_claimed_by_nvenc_worker(client, tmp_path, monkeypatch, no_local):
    """HEVC 单只派给有 hevc_nvenc + scale_cuda 的执行者；软编 Worker 跳过它（不占队首）。"""
    _set_format(client, "hevc")
    file_id = _seed_file(client, tmp_path)  # SDR 源
    task_id = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id}).json()["data"]["id"]
    token = _pair_worker(client)

    cpu = client.post(
        f"{_WORKER}/claim",
        json={"caps": {"encoders": ["libx264"], "filters": []}},
        headers=_worker_headers(token),
    )
    assert cpu.status_code == 200, cpu.text
    assert cpu.json()["data"]["task"] is None
    assert _task_row(client, task_id)["status"] == PreloadTaskStatus.QUEUED.value

    nvenc = client.post(
        f"{_WORKER}/claim",
        json={"caps": {
            "encoders": ["h264_nvenc", "hevc_nvenc"],
            "filters": ["scale_cuda"],
            "hw_decoders": ["hevc", "h264"],
        }},
        headers=_worker_headers(token),
    )
    task = nvenc.json()["data"]["task"]
    assert task is not None and task["task_id"] == task_id, nvenc.text
    assert task["unusable_reason"] is None
    assert "hevc_nvenc" in task["argv"] and "libx264" not in task["argv"]


def test_hevc_command_needs_nvenc_and_says_so():
    """HEVC 没有 NVENC 时命令直接标不可用（原先静默降成 libx264，整片转完才被登记闸拒收）。"""
    from types import SimpleNamespace

    from movieclaw_api.services.preload import service as _svc

    task = SimpleNamespace(
        id=1, preset_id="saver", video_format="hevc", resume_parts=0, part_bytes=0
    )
    file = SimpleNamespace(
        resolution="2160p", video_codec="hevc", hdr=None, dv_profile=None, color_space=None,
        bit_depth=10, duration_seconds=7200, size_bytes=1, audio_streams=[{"codec": "eac3"}],
        subtitle_streams=[], chapters=[],
    )
    no_gpu = _svc.build_task_command(task, file, worker_caps=None)
    assert no_gpu["argv"] == [] and "hevc_nvenc" in no_gpu["unusable_reason"]
    h264_only = _svc.build_task_command(
        task, file, worker_caps={"encoders": ["h264_nvenc"], "filters": ["scale_cuda"]}
    )
    assert h264_only["argv"] == [] and h264_only["unusable_reason"]
    hevc_gpu = _svc.build_task_command(
        task, file,
        worker_caps={
            "encoders": ["hevc_nvenc"], "filters": ["scale_cuda"], "hw_decoders": ["hevc"],
        },
    )
    assert hevc_gpu["unusable_reason"] is None and "hevc_nvenc" in hevc_gpu["argv"]


def _pgs_sup_bytes() -> bytes:
    """最小 PGS（.sup）：1.0s 出一块白色矩形，3.0s 清屏。ffmpeg 没有 PGS 编码器，只能手造。"""
    import struct

    width, height = 160, 24

    def segment(seg_type: int, pts: int, payload: bytes) -> bytes:
        head = struct.pack(">II", pts, 0) + bytes([seg_type]) + struct.pack(">H", len(payload))
        return b"PG" + head + payload

    def rle_filled(w: int, h: int) -> bytes:
        out = bytearray()
        for _ in range(h):
            if w < 64:
                out += bytes([0x00, 0x80 | w, 1])
            else:
                out += bytes([0x00, 0xC0 | (w >> 8), w & 0xFF, 1])
            out += b"\x00\x00"
        return bytes(out)

    def display_set(pts: int, objects: bool) -> bytes:
        flag = 1 if objects else 0
        pcs = struct.pack(">HHBHBBBB", 1920, 1080, 0x10, 1, 0x80 if objects else 0, 0, 0, flag)
        if objects:
            pcs += struct.pack(">HBBHH", 0, 0, 0x00, 100, 900)
        wds = struct.pack(">BBHHHH", 1, 0, 100, 900, width, height)
        out = segment(0x16, pts, pcs) + segment(0x17, pts, wds)
        if objects:
            pds = bytes([0, 0]) + bytes([0, 16, 128, 128, 0]) + bytes([1, 235, 128, 128, 255])
            rle = rle_filled(width, height)
            ods = struct.pack(">HBB", 0, 0, 0xC0) + struct.pack(">I", 4 + len(rle))[1:]
            ods += struct.pack(">HH", width, height) + rle
            out += segment(0x14, pts, pds) + segment(0x15, pts, ods)
        return out + segment(0x80, pts, b"")

    return display_set(90000, True) + display_set(270000, False)


_RICH_ASS = (
    "[Script Info]\nScriptType: v4.00+\nPlayResX: 320\nPlayResY: 180\n\n"
    "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
    "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, "
    "Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
    "Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
    "0,0,0,0,100,100,0,0,1,2,0,2,10,10,10,1\n\n"
    "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    "Dialogue: 0,0:00:00.00,0:00:02.00,Default,,0,0,0,,cue A\n"
    "Dialogue: 0,0:00:01.50,0:00:04.00,Default,,0,0,0,,cue B\n"
    "Dialogue: 0,0:00:08.00,0:00:12.00,Default,,0,0,0,,cue C spans boundary\n"
    "Dialogue: 0,0:00:10.00,0:00:10.50,Default,,0,0,0,,cue D exact boundary\n"
    "Dialogue: 0,0:00:13.00,0:00:15.00,Default,,0,0,0,,cue E\n"
    "Dialogue: 0,0:00:17.00,0:00:18.90,Default,,0,0,0,,cue F\n"
)


def _seed_rich_media(client: TestClient, tmp_path, *, duration: int = 20) -> int:
    """真媒体源：视频 + 两条音轨（eng 默认 / jpn 原声）+ ASS + 强制 PGS，元数据与文件一致。

    片库语言 en-US、原始语言 ja：默认音轨按策略落到原声（jpn），字幕文本全留、PGS 强制轨留。
    """
    import json

    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg, "端到端字幕/音轨用例需要 ffmpeg"
    workdir = tmp_path / "rich"
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "subs.ass").write_text(_RICH_ASS, encoding="utf-8")
    (workdir / "subs.sup").write_bytes(_pgs_sup_bytes())
    media = tmp_path / "movies" / "rich.mkv"
    media.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate=10:duration={duration}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
         "-f", "lavfi", "-i", f"sine=frequency=660:duration={duration}",
         "-i", str(workdir / "subs.ass"), "-i", str(workdir / "subs.sup"),
         "-map", "0:v", "-map", "1:a", "-map", "2:a", "-map", "3:s", "-map", "4:s",
         "-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "log-level=error",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-ac", "1", "-c:s", "copy",
         "-metadata:s:a:0", "language=eng", "-metadata:s:a:1", "language=jpn",
         "-metadata:s:s:0", "language=eng", "-metadata:s:s:1", "language=eng",
         "-disposition:s:1", "forced", str(media)],
        check=True, timeout=120,
    )
    info = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(media)],
        capture_output=True, text=True, check=True,
    ).stdout)
    audio, subs = [], []
    for stream in info["streams"]:
        tags = stream.get("tags") or {}
        disp = stream.get("disposition") or {}
        if stream["codec_type"] == "audio":
            audio.append({"codec": stream["codec_name"], "channels": stream.get("channels"),
                          "language": tags.get("language"), "title": tags.get("title", ""),
                          "default": bool(disp.get("default"))})
        elif stream["codec_type"] == "subtitle":
            subs.append({"codec": stream["codec_name"], "language": tags.get("language"),
                         "title": tags.get("title", ""), "forced": bool(disp.get("forced")),
                         "default": bool(disp.get("default"))})
    return _insert_source_row(
        client, media, duration=duration, audio=audio, subs=subs, video_codec="hevc"
    )


def _insert_source_row(
    client, media: Path, *, duration: int, audio: list, subs: list, video_codec: str
) -> int:
    async def _seed() -> int:
        from sqlmodel import select

        from movieclaw_db.models import Library

        async with get_database().session() as session:
            library = (
                await session.execute(select(Library).where(Library.name == "电影库"))
            ).scalar_one_or_none()
            if library is None:
                library = await LibraryRepository(session).create(
                    name="电影库", kind="movie", root_paths=[str(media.parent)]
                )
            item = (
                await session.execute(select(MediaItem).where(MediaItem.tmdb_id == 1))
            ).scalar_one_or_none()
            if item is None:
                item = MediaItem(kind="movie", tmdb_id=1, title="示例", original_title="Example")
                session.add(item)
                await session.flush()
            row = LibraryFile(
                library_id=library.id,
                media_item_id=item.id,
                file_path=str(media),
                size_bytes=media.stat().st_size,
                source="scanned",
                state=FileState.IN_PLACE,
                container="mkv",
                video_codec=video_codec,
                resolution="180p",
                duration_seconds=duration,
                audio_streams=audio,
                subtitle_streams=subs,
            )
            session.add(row)
            await session.commit()
            return int(row.id)

    return client.portal.call(partial(_seed))  # type: ignore[attr-defined]


def _set_original_language(client: TestClient, library_locale: str, original: str) -> None:
    """片库语言与原始语言：默认轨策略的上下文（库语言 = 刮削语言，原始语言 = TMDB original）。"""

    async def _set() -> None:
        from movieclaw_db.models import MediaMetadata

        async with get_database().session() as session:
            session.add(MediaMetadata(media_item_id=1, original_language=original))
            library = (await session.execute(_select_library())).scalar_one()
            library.scrape_overrides = {"language_priority": [library_locale]}
            session.add(library)
            await session.commit()

    client.portal.call(partial(_set))  # type: ignore[attr-defined]


def _select_library():  # type: ignore[no-untyped-def]
    from sqlmodel import select

    from movieclaw_db.models import Library

    return select(Library).where(Library.name == "电影库")


def _probe_streams(path: str) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)


def _packet_times(path: str, kind: str) -> list[tuple[float, float]]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", kind, "-show_packets",
         "-show_entries", "packet=pts_time,duration_time", "-of", "csv=p=0", path],
        capture_output=True, text=True, check=True,
    ).stdout
    rows = []
    for line in out.splitlines():
        parts = line.split(",")
        if len(parts) == 2 and parts[0] not in ("", "N/A"):
            duration = float(parts[1]) if parts[1] not in ("", "N/A") else 0.0
            rows.append((float(parts[0]), duration))
    return rows


def _local_command(client: TestClient, task_id: int, *, seconds: int, monkeypatch):  # type: ignore[no-untyped-def]
    """按 NAS 本地（无硬件：软编 H.264）的方式领单并装命令；分段时长在测试里缩短以便多段。"""
    from movieclaw_api.services.preload import service as _svc

    monkeypatch.setattr(_svc, "SEGMENT_SECONDS", seconds)

    async def _claim() -> dict:
        async with get_database().session() as session:
            claimed = await _svc.claim_local_task(session, allow_h264=True, allow_hevc=False)
            assert claimed is not None
            task, file = claimed
            plan = await _svc.plan_for(session, file)
            return _svc.build_task_command(task, file, plan=plan)

    return client.portal.call(partial(_claim))  # type: ignore[attr-defined]


def test_policy_end_to_end_real_ffmpeg(client, tmp_path, monkeypatch, no_local):
    """字幕与音轨策略用真 ffmpeg 端到端跑一遍：ASS 原样、强制 PGS 留、音轨只留原声（立体声 AAC），
    分段 + 拼接后段边界不重复、不丢条，时长与源一致。"""
    from movieclaw_api.services.preload import dispatcher as _dispatcher

    file_id = _seed_rich_media(client, tmp_path)
    _set_original_language(client, "en-US", "ja")
    task_id = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id}).json()["data"]["id"]
    command = _local_command(client, task_id, seconds=4, monkeypatch=monkeypatch)
    assert command["unusable_reason"] is None
    assert command["segment_total"] == 5  # 20 秒 / 4 秒
    # 音轨：只留原声（jpn，在源里是第 2 条音轨 = 0:a:1）；字幕：ASS 与强制 PGS
    assert command["argv"].count("-map") == 2 and "0:a:1" in command["argv"]  # 视频 + 一条音轨
    assert "0:a:0" not in command["argv"]
    client.portal.call(partial(_dispatcher._execute_local_task, task_id, command))  # type: ignore[attr-defined]

    view = client.get(f"{_PRELOAD}/tasks/{task_id}").json()["data"]
    assert view["status"] == "completed", view
    final = Path(_path_of(client, view["result_file_id"]))
    info = _probe_streams(str(final))
    kinds = [(s["codec_type"], s["codec_name"]) for s in info["streams"]]
    expected = [("video", "h264"), ("audio", "aac"), ("subtitle", "ass")]
    expected += [("subtitle", "hdmv_pgs_subtitle")]
    assert kinds == expected, kinds
    audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
    assert audio["channels"] == 2
    assert abs(float(info["format"]["duration"]) - 20.0) < 0.5
    # 字幕：每条 ASS 只出现一次，时间与源文件一致（段边界不重复、不丢、不错位）
    source = _path_of(client, file_id)
    src_ass = _packet_times(str(source), "s:0")
    out_ass = _packet_times(str(final), "s:0")
    assert len(out_ass) == len(src_ass) == 6
    for (sp, _), (op, _) in zip(src_ass, out_ass, strict=True):
        assert abs(sp - op) < 0.05, (src_ass, out_ass)
    assert len(_packet_times(str(final), "s:1")) == len(_packet_times(str(source), "s:1"))


@pytest.mark.preload_local
def test_nas_without_hardware_runs_order_end_to_end(client, tmp_path, monkeypatch):
    """没有硬件、没有 Worker 的 NAS：下单后由调度器交给本地执行者，libx264 软编分段转完、拼接
    登记为版本（§7：本地是基石，没有 Worker 的家庭也能用预转码）。走真调度、真 ffmpeg。"""
    from movieclaw_api.services.preload import dispatcher as _dispatcher
    from movieclaw_api.services.preload import service as _svc

    monkeypatch.setattr(_dispatcher, "detect_local_hw", lambda: None)
    monkeypatch.setattr(_dispatcher, "_software_caps", lambda: None)
    monkeypatch.setattr(_dispatcher, "_playback_transcoding", lambda: False)
    monkeypatch.setattr(_svc, "SEGMENT_SECONDS", 4)
    file_id = _seed_rich_media(client, tmp_path)
    task_id = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id}).json()["data"]["id"]

    deadline = time.monotonic() + 120
    view: dict = {}
    while time.monotonic() < deadline:
        view = client.get(f"{_PRELOAD}/tasks/{task_id}").json()["data"]
        if view["status"] in ("completed", "failed"):
            break
        time.sleep(0.5)
    assert view["status"] == "completed", view
    info = _probe_streams(str(_path_of(client, view["result_file_id"])))
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert video["codec_name"] == "h264"
    assert abs(float(info["format"]["duration"]) - 20.0) < 0.5


def test_local_executor_reports_finalizing_before_merging(client, tmp_path, monkeypatch, no_local):
    """NAS 本地执行者在拼接前也要把 finalizing 写进台账。

    Worker 端 2026-10-20 修过同样的缺陷（收尾状态写进去但没人看得见）；
    本地执行者原来压根不写，活动页会一直挂着最后一段。两边口径必须一致。
    """
    from movieclaw_api.services.preload import dispatcher as _dispatcher
    from movieclaw_api.services.preload import service as _svc

    file_id = _seed_rich_media(client, tmp_path)
    task_id = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id}).json()["data"]["id"]
    command = _local_command(client, task_id, seconds=4, monkeypatch=monkeypatch)

    seen: list[str] = []
    original = _svc.finalize_artifact

    async def spy_finalize(session, task, *, probe):  # noqa: ANN001
        # 拼接真正开始前，台账里应该已经是 finalizing
        async with get_database().session() as probe_session:
            row = await probe_session.get(PreloadTask, task_id)
            seen.append((row.progress or {}).get("phase"))
        return await original(session, task, probe=probe)

    monkeypatch.setattr(_svc, "finalize_artifact", spy_finalize)
    client.portal.call(partial(_dispatcher._execute_local_task, task_id, command))  # type: ignore[attr-defined]

    assert seen == ["finalizing"], seen
    view = client.get(f"{_PRELOAD}/tasks/{task_id}").json()["data"]
    assert view["status"] == "completed", view


def test_local_segments_resume_after_playback_preemption(client, tmp_path, monkeypatch, no_local):
    """本地分段续转（真 ffmpeg）：播放开始 → 正在转的段被杀、半截段删掉、任务回队且断点保留；
    播放结束 → 从断点续转，最终拼成完整产物。"""
    from movieclaw_api.services.preload import dispatcher as _dispatcher
    from movieclaw_api.services.preload import service as _svc

    file_id = _seed_rich_media(client, tmp_path)
    task_id = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id}).json()["data"]["id"]
    command = _local_command(client, task_id, seconds=4, monkeypatch=monkeypatch)
    playing = {"on": False}
    monkeypatch.setattr(_dispatcher, "_playback_transcoding", lambda: playing["on"])

    async def _run_until_second_segment() -> None:
        runner = asyncio.create_task(_dispatcher._execute_local_task(task_id, command))
        # 等第二段落盘（resume_parts 到 2），然后模拟播放开始：看门狗的同款取消
        for _ in range(600):
            async with get_database().session() as session:
                row = await session.get(PreloadTask, task_id)
                if row.resume_parts >= 2:
                    break
            await asyncio.sleep(0.1)
        playing["on"] = True
        _dispatcher._preempt_local_runners()
        await runner

    client.portal.call(partial(_run_until_second_segment))  # type: ignore[attr-defined]

    async def _check_yielded() -> tuple[str, int, int | None, bool]:
        async with get_database().session() as session:
            row = await session.get(PreloadTask, task_id)
            directory = _svc.artifact_dir_for(task_id)
            half_done = list(directory.glob("*.part.mkv"))
            return row.status, row.resume_parts, row.worker_device_id, bool(half_done)

    status, resume, device, has_half = client.portal.call(partial(_check_yielded))  # type: ignore[attr-defined]
    assert status == "queued" and resume >= 2 and device is None, (status, resume, device)
    assert not has_half  # 半截段已删

    playing["on"] = False
    command = _local_command(client, task_id, seconds=4, monkeypatch=monkeypatch)
    client.portal.call(partial(_dispatcher._execute_local_task, task_id, command))  # type: ignore[attr-defined]
    view = client.get(f"{_PRELOAD}/tasks/{task_id}").json()["data"]
    assert view["status"] == "completed", view
    final = Path(_path_of(client, view["result_file_id"]))
    assert abs(float(_probe_streams(str(final))["format"]["duration"]) - 20.0) < 0.5


def test_pinned_task_stays_with_its_executor(client, tmp_path, monkeypatch, no_local):
    """已有分段的任务归产出它的执行者：NAS 产出的，Worker 领不走；Worker 产出的，NAS 也领不走。"""
    from movieclaw_api.services.preload import service as _svc

    file_id = _seed_file(client, tmp_path, hdr=None)
    task_id = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id}).json()["data"]["id"]

    async def _pin_to_local() -> None:
        async with get_database().session() as session:
            row = await session.get(PreloadTask, task_id)
            row.resume_parts = 2
            row.worker_device_id = None
            row.status = PreloadTaskStatus.QUEUED
            await session.commit()

    client.portal.call(partial(_pin_to_local))  # type: ignore[attr-defined]
    token = _pair_worker(client)
    worker = client.post(f"{_WORKER}/claim", json={"caps": {}}, headers=_worker_headers(token))
    assert worker.json()["data"]["task"] is None  # NAS 的分段，Worker 不接

    async def _local_takes() -> bool:
        async with get_database().session() as session:
            claimed = await _svc.claim_local_task(session, allow_h264=False, allow_hevc=False)
            return claimed is not None and claimed[0].id == task_id

    assert client.portal.call(partial(_local_takes))  # type: ignore[attr-defined]  # 有分段时即使有 Worker 在线，NAS 也接着转


def test_nas_yields_new_tasks_to_online_capable_worker(client, tmp_path, monkeypatch, no_local):
    """有能转这一单的 Worker 在线时，NAS 不接新单（让 GPU 清队）；没有时 NAS 接。"""
    from movieclaw_api.services.preload import service as _svc

    file_id = _seed_file(client, tmp_path, hdr=None)
    task_id = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id}).json()["data"]["id"]

    async def _claim(allow: bool) -> int | None:
        async with get_database().session() as session:
            claimed = await _svc.claim_local_task(session, allow_h264=allow, allow_hevc=False)
            if claimed is None:
                return None
            task = claimed[0]
            # 还回队列，供下一次断言复用
            task.status = PreloadTaskStatus.QUEUED
            task.worker_device_id = None
            await session.commit()
            return task.id

    assert client.portal.call(partial(_claim, False)) is None  # type: ignore[attr-defined]
    assert client.portal.call(partial(_claim, True)) == task_id  # type: ignore[attr-defined]


def test_hevc_needs_local_hevc_hardware(client, tmp_path):  # type: ignore[no-untyped-def]
    """HEVC 格式：没有本机 HEVC 硬件编码器（或没有 NVENC）就在装命令时判不可执行；有就出硬件链。"""
    from types import SimpleNamespace

    from movieclaw_api.services.preload import service as _svc
    from movieclaw_api.services.preload.ffmpeg_preload import LocalHw

    task = SimpleNamespace(
        id=1, preset_id="saver", video_format="hevc", resume_parts=0, part_bytes=0
    )
    file = SimpleNamespace(
        resolution="1080p", video_codec="hevc", hdr=None, dv_profile=None, color_space=None,
        bit_depth=10, duration_seconds=7200, size_bytes=1, audio_streams=[{"codec": "eac3"}],
        subtitle_streams=[], chapters=[], file_path="x.mkv",
    )
    no_hw = _svc.build_task_command(task, file, local=None)
    assert no_hw["argv"] == [] and "HEVC" in no_hw["unusable_reason"]
    vaapi = LocalHw(
        backend="vaapi", hevc_encoder="hevc_vaapi", native_tonemap=None, has_tonemapx=False
    )
    ok = _svc.build_task_command(task, file, local=vaapi)
    assert ok["unusable_reason"] is None and "hevc_vaapi" in ok["argv"]
    assert "-hwaccel" in ok["argv"] and "vaapi" in ok["argv"]


def test_estimate_follows_kept_audio_tracks(client, tmp_path):  # type: ignore[no-untyped-def]
    """体积预估按实际保留的音轨数算：同片两条音轨保留两条，预估比一条时多出一条音频码率的部分。"""
    from movieclaw_api.services.preload.ffmpeg_preload import effective_preset, estimate_size_bytes

    preset = effective_preset("saver", "h264")
    one = estimate_size_bytes(duration_seconds=7200, preset=preset, audio_tracks=1)
    two = estimate_size_bytes(duration_seconds=7200, preset=preset, audio_tracks=2)
    assert two - one == int(7200 * 128_000 / 8)  # 省流立体声 AAC 128k
    premium = effective_preset("premium", "h264")
    assert estimate_size_bytes(duration_seconds=7200, preset=premium, audio_tracks=1) - int(
        7200 * 3_000_000 / 8
    ) == int(7200 * 192_000 / 8)  # 高清立体声 AAC 192k


def test_executor_label_names_nas_or_worker(client, tmp_path, monkeypatch, no_local):
    """活动页标明执行者：NAS 本地领的写「NAS 本地」，Worker 领的写它的设备名，排队中的为空。"""
    from movieclaw_api.services.preload import service as _svc

    file_id = _seed_file(client, tmp_path, hdr=None)
    task_id = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id}).json()["data"]["id"]
    assert client.get(f"{_PRELOAD}/tasks/{task_id}").json()["data"]["executor"] is None

    async def _claim_local() -> None:
        async with get_database().session() as session:
            await _svc.claim_local_task(session, allow_h264=True, allow_hevc=False)

    client.portal.call(partial(_claim_local))  # type: ignore[attr-defined]
    assert client.get(f"{_PRELOAD}/tasks/{task_id}").json()["data"]["executor"] == "NAS 本地"

    # 归还给队列，再由一台真实的转码 Worker 领走：执行者显示它的登记设备名
    async def _requeue() -> None:
        async with get_database().session() as session:
            row = await session.get(PreloadTask, task_id)
            row.status = PreloadTaskStatus.QUEUED
            row.worker_device_id = None
            await session.commit()

    client.portal.call(partial(_requeue))  # type: ignore[attr-defined]
    token = _pair_worker(client)
    claim = client.post(f"{_WORKER}/claim", json={"caps": {}}, headers=_worker_headers(token))
    assert claim.json()["data"]["task"]["task_id"] == task_id, claim.text
    label = client.get(f"{_PRELOAD}/tasks/{task_id}").json()["data"]["executor"]
    assert label == "测试预转码 Worker"


def test_local_hdr_paths_use_hardware_chain(monkeypatch):  # type: ignore[no-untyped-def]
    """NAS 本地 HDR（QSV/VAAPI）：有原生 tone-map 走 GPU 链；没有就 CPU tonemapx 下载→映射→回传；
    两者都没有才不可执行。命令形状在这里断言（本机没有 QSV/VAAPI，不能真跑）。"""
    from types import SimpleNamespace

    from movieclaw_api.services.preload import service as _svc
    from movieclaw_api.services.preload.ffmpeg_preload import LocalHw

    task = SimpleNamespace(
        id=1, preset_id="premium", video_format="h264", resume_parts=0, part_bytes=0
    )
    file = SimpleNamespace(
        resolution="2160p", video_codec="hevc", hdr="HDR10", dv_profile=None, color_space=None,
        bit_depth=10, duration_seconds=7200, size_bytes=1, audio_streams=[{"codec": "eac3"}],
        subtitle_streams=[], chapters=[], file_path="x.mkv",
    )
    native = LocalHw(
        backend="vaapi", native_tonemap="tonemap_vaapi=format=nv12:t=bt709:m=bt709:p=bt709",
        has_tonemapx=True,
    )
    gpu = _svc.build_task_command(task, file, local=native)
    assert gpu["unusable_reason"] is None
    assert "tonemap_vaapi=format=nv12:t=bt709:m=bt709:p=bt709" in " ".join(gpu["argv"])
    assert "hwdownload" not in " ".join(gpu["argv"])
    assert gpu["argv"][gpu["argv"].index("-hwaccel_output_format") + 1] == "vaapi"

    cpu = _svc.build_task_command(
        task, file, local=LocalHw(backend="vaapi", has_tonemapx=True)
    )
    assert cpu["unusable_reason"] is None
    flat = " ".join(cpu["argv"])
    assert "tonemapx=tonemap=bt2390" in flat and "hwupload" in flat
    assert "-vaapi_device" in cpu["argv"]

    none = _svc.build_task_command(task, file, local=LocalHw(backend="vaapi", has_tonemapx=False))
    assert none["argv"] == [] and "tonemapx" in none["unusable_reason"]
