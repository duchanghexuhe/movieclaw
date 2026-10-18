"""播放优先的让位与续派（服务端半边，docs/design/worker-unification-todo.md「并发预算」）。

1. 预算：有实时任务在跑时为 0；空闲时整个并发都可用，在途推送从余量里扣；
2. Worker 让位（preload.yielded）：在途记账当场释放，任务当场退回队列（断点保留），不等租约；
3. 归属校验：别的 Worker 的让位消息释放不了这台的在途账；
4. 实时任务全部结束：立刻派一轮排队任务（续派）；
5. 服务端取消发 job.stop 时带 reason=cancel（Worker 据此删掉本地半成品，让位则不删）。
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from tests.api.test_preload_transcode import (  # noqa: I001
    _ADMIN,
    _AUTH,
    _make_client,
    _pair_worker,
    _seed_file,
    _worker_headers,
)

from movieclaw_api.services.playback.remote_worker import (
    REMOTE_WORKER_PROTOCOL_VERSION,
    get_remote_worker_registry,
    reset_remote_worker_registry,
)
from movieclaw_db.models.preload_task import PreloadTaskStatus

_PRELOAD = "/api/v1/preload"
_WS = "/api/v1/transcode-worker/ws"


@pytest.fixture
def client(tmp_path, monkeypatch):
    with _make_client(tmp_path, monkeypatch, name="yield") as c:
        c.post(f"{_AUTH}/bootstrap", json=_ADMIN)
        c.post(f"{_AUTH}/login", json=_ADMIN)
        c.put("/api/v1/transcode-worker/config", json={"enabled": True})
        yield c

    reset_remote_worker_registry()
    from movieclaw_api.core.config import get_settings
    from movieclaw_api.services.auth import reset_auth_state
    from movieclaw_api.settings.store import reset_setting_store
    from movieclaw_db.crypto import reset_secret_box

    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    get_settings.cache_clear()


def _hello(worker_id: str, *, max_jobs: int) -> dict:
    return {
        "type": "worker.hello",
        "protocol_version": REMOTE_WORKER_PROTOCOL_VERSION,
        "worker_id": worker_id,
        "capabilities": {
            "platform": "windows",
            "backends": ["nvenc"],
            "encoders": ["h264_nvenc", "hevc_nvenc"],
            "max_jobs": max_jobs,
            "roles": ["realtime", "preload"],
            "server_config": True,
            "segment_types": ["fmp4"],
            "filters": ["scale_cuda", "hwdownload", "hwupload_cuda", "tonemapx"],
            "hw_decoders": ["h264", "hevc"],
        },
    }


def _queued_task_ids(client: TestClient) -> set[int]:
    rows = client.get(f"{_PRELOAD}/tasks").json()["data"]
    return {t["id"] for t in rows if t["status"] == PreloadTaskStatus.QUEUED.value}


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    """后台派单/回队是事件循环里的协程：轮询等它落地（不靠固定 sleep）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_capacity_is_zero_while_realtime_runs_and_full_slots_when_idle(client, tmp_path):
    """预算：空闲时整个并发都可用（在途推送从中扣）；有实时任务在跑就是 0。"""
    token = _pair_worker(client)
    with client.websocket_connect(_WS, headers=_worker_headers(token)) as ws:
        ws.send_json(_hello("win-cap", max_jobs=2))
        assert ws.receive_json()["type"] == "worker.accepted"
        registry = get_remote_worker_registry()
        connection = registry._workers["win-cap"]
        assert registry.preload_capacity(connection) == 2  # 空闲：并发 2 全给预转码
        registry.note_preload_pushed(connection, 999_001)  # 一个在途推送
        assert registry.preload_capacity(connection) == 1
        with registry._lock:
            connection.jobs.add("job-rt-cap")  # 播放开始
        assert registry.preload_capacity(connection) == 0
        with registry._lock:
            connection.jobs.discard("job-rt-cap")
        registry.release_preload_task(999_001)
        assert registry.preload_capacity(connection) == 2


def test_yield_requeues_task_at_once_and_releases_account(client, tmp_path):
    """Worker 为播放让位：在途记账当场释放，任务当场退回队列；不等 10 分钟租约。"""
    file_id = _seed_file(client, tmp_path, hdr="HDR10")
    token = _pair_worker(client)
    created = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id}).json()["data"]
    task_id = created["id"]
    with client.websocket_connect(_WS, headers=_worker_headers(token)) as ws:
        ws.send_json(_hello("win-yield", max_jobs=1))
        assert ws.receive_json()["type"] == "worker.accepted"
        pushed = ws.receive_json()
        assert pushed["type"] == "job.start" and pushed["task_id"] == task_id
        registry = get_remote_worker_registry()
        connection = registry._workers["win-yield"]
        # 播放进来：服务端这边也登记了实时任务（reserve 的结果），Worker 随即让位
        with registry._lock:
            connection.jobs.add("job-rt-yield")
        ws.send_json({"type": "preload.yielded", "task_id": task_id})
        assert _wait_until(lambda: task_id in _queued_task_ids(client))
        assert _wait_until(lambda: connection.preload_jobs == {})
        # 有实时任务在跑：回队后不会被推回这台（心跳确认连接正常，中间没有 job.start）
        ws.send_json({"type": "worker.heartbeat", "load": {}})
        assert ws.receive_json()["type"] == "worker.heartbeat.ack"
        with registry._lock:
            connection.jobs.discard("job-rt-yield")


def test_yield_from_another_worker_cannot_release_our_account(client, tmp_path):
    """归属校验：别的 Worker 发来的让位消息，不能释放这台 Worker 的在途账。"""
    file_id = _seed_file(client, tmp_path, hdr="HDR10")
    token_a = _pair_worker(client)
    token_b = _pair_worker(client)
    created = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id}).json()["data"]
    task_id = created["id"]
    with client.websocket_connect(_WS, headers=_worker_headers(token_a)) as ws_a:
        ws_a.send_json(_hello("win-owner", max_jobs=1))
        assert ws_a.receive_json()["type"] == "worker.accepted"
        assert ws_a.receive_json()["task_id"] == task_id
        with client.websocket_connect(_WS, headers=_worker_headers(token_b)) as ws_b:
            ws_b.send_json(_hello("win-intruder", max_jobs=1))
            assert ws_b.receive_json()["type"] == "worker.accepted"
            ws_b.send_json({"type": "preload.yielded", "task_id": task_id})
            ws_b.send_json({"type": "worker.heartbeat", "load": {}})
            assert ws_b.receive_json()["type"] == "worker.heartbeat.ack"
        registry = get_remote_worker_registry()
        owner = registry._workers["win-owner"]
        assert task_id in owner.preload_jobs  # 账还在
        assert task_id not in _queued_task_ids(client)  # 任务没被退回


def test_realtime_end_triggers_resume_dispatch(client, tmp_path):
    """播放结束（实时任务全部结束）：立刻派一轮排队任务，不等下一个触发点。"""
    file_id = _seed_file(client, tmp_path, hdr="HDR10")
    token = _pair_worker(client)
    registry = get_remote_worker_registry()
    with client.websocket_connect(_WS, headers=_worker_headers(token)) as ws:
        ws.send_json(_hello("win-resume", max_jobs=1))
        assert ws.receive_json()["type"] == "worker.accepted"
        # 播放在跑：走正常的 reserve 占住并发位（预转码不推）
        registry.reserve("job-rt-resume", backend="nvenc")
        created = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id}).json()["data"]
        ws.send_json({"type": "worker.heartbeat", "load": {}})
        assert ws.receive_json()["type"] == "worker.heartbeat.ack"
        assert created["id"] in _queued_task_ids(client)
        # 播放结束：实时在途释放（生产路径是 job.finished/failed 或 stop 的收尾）
        client.portal.call(registry.release_job, "job-rt-resume")
        # 派单在事件循环里后台进行：先等任务离开队列（有上限，回归时不会卡死在 receive 上）
        assert _wait_until(lambda: created["id"] not in _queued_task_ids(client))
        message = ws.receive_json()
        assert message["type"] == "job.start" and message["task_id"] == created["id"]
        assert message["class"] == "preload"


def test_server_cancel_sends_stop_with_reason_cancel(client, tmp_path):
    """服务端取消：job.stop 带 reason=cancel，Worker 据此删本地半成品（让位则不带）。"""
    file_id = _seed_file(client, tmp_path, hdr="HDR10")
    token = _pair_worker(client)
    created = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id}).json()["data"]
    task_id = created["id"]
    registry = get_remote_worker_registry()
    with client.websocket_connect(_WS, headers=_worker_headers(token)) as ws:
        ws.send_json(_hello("win-cancel", max_jobs=1))
        assert ws.receive_json()["type"] == "worker.accepted"
        assert ws.receive_json()["task_id"] == task_id
        device_id = registry._workers["win-cancel"].login_device_id
        client.portal.call(registry.send_preload_stop, device_id, task_id)
        stop = ws.receive_json()
        assert stop == {
            "type": "job.stop",
            "class": "preload",
            "task_id": task_id,
            "reason": "cancel",
        }
