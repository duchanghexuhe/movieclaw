"""Worker 统一调度 v2 的端到端测试（docs/design/worker-unification-todo.md T2）。

覆盖推送派单主链路：
1. 带 preload 角色的 Worker 上线 → 排队任务被推送（不再依赖 REST claim）；
2. 预算（播放优先）：空闲时整个并发都能转预转码；有实时任务在跑时为 0；
3. complete 触发下一个排队任务的派发；
4. job.start 载荷与 claim 时代同构（argv/断点/签名地址）；
5. 推送失败当场回队（无 Worker 时下单不失败，Worker 上线后补派）。
"""

from __future__ import annotations

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
    with _make_client(tmp_path, monkeypatch, name="dispatch") as c:
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


def _hello(worker_id: str, *, max_jobs: int, roles: list[str]) -> dict:
    return {
        "type": "worker.hello",
        "protocol_version": REMOTE_WORKER_PROTOCOL_VERSION,
        "worker_id": worker_id,
        "capabilities": {
            "platform": "windows",
            "backends": ["nvenc"],
            "encoders": ["h264_nvenc", "hevc_nvenc"],
            "max_jobs": max_jobs,
            "roles": roles,
            "server_config": True,
            "segment_types": ["fmp4"],
            # HDR 源的省流档全 GPU 链要 tonemapx（与 claim 时代同一装配规则）
            "filters": ["scale_cuda", "hwdownload", "hwupload_cuda", "tonemapx"],
            "hw_decoders": ["h264", "hevc"],
        },
    }


def _queued_task_ids(client: TestClient) -> set[int]:
    rows = client.get(f"{_PRELOAD}/tasks").json()["data"]
    return {t["id"] for t in rows if t["status"] == PreloadTaskStatus.QUEUED.value}


def test_worker_with_preload_role_receives_queued_task_on_connect(client, tmp_path):
    """下单时没有 Worker；带 preload 角色的 Worker 一上线，排队任务立刻被推送。"""
    file_id = _seed_file(client, tmp_path, hdr="HDR10")
    token = _pair_worker(client)
    created = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id}).json()["data"]
    task_id = created["id"]

    with client.websocket_connect(_WS, headers=_worker_headers(token)) as ws:
        ws.send_json(_hello("win-a", max_jobs=2, roles=["realtime", "preload"]))
        assert ws.receive_json()["type"] == "worker.accepted"
        # 下单在连接之前：Worker 上线触发补派
        message = ws.receive_json()
        assert message["type"] == "job.start"
        assert message["class"] == "preload"
        assert message["task_id"] == task_id
        # 载荷与 claim 时代同构：可执行 argv 与断点字段都在
        assert message["argv"]
        assert "source_url" in message and "artifact_url" in message
        assert message["resume_parts"] == 0

    # 任务已派给该设备（CLAIMED），不再排队
    assert task_id not in _queued_task_ids(client)


def test_ordering_after_create_triggers_dispatch(client, tmp_path):
    """Worker 先在线、任务后下单：create 路由的触发点把任务推过去。"""
    file_id = _seed_file(client, tmp_path, hdr="HDR10")
    token = _pair_worker(client)
    with client.websocket_connect(_WS, headers=_worker_headers(token)) as ws:
        ws.send_json(_hello("win-b", max_jobs=2, roles=["realtime", "preload"]))
        assert ws.receive_json()["type"] == "worker.accepted"
        created = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id}).json()["data"]
        message = ws.receive_json()
        assert message["type"] == "job.start"
        assert message["task_id"] == created["id"]


def test_single_concurrency_idle_worker_gets_preload(client, tmp_path):
    """空闲时整个并发都能转预转码：并发 1 的机器也接（播放来了再让位）。"""
    file_id = _seed_file(client, tmp_path, hdr="HDR10")
    token = _pair_worker(client)
    created = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id}).json()["data"]
    with client.websocket_connect(_WS, headers=_worker_headers(token)) as ws:
        ws.send_json(_hello("win-c", max_jobs=1, roles=["realtime", "preload"]))
        assert ws.receive_json()["type"] == "worker.accepted"
        message = ws.receive_json()
        assert message["type"] == "job.start" and message["task_id"] == created["id"]
    assert created["id"] not in _queued_task_ids(client)


def test_realtime_only_worker_is_not_a_preload_candidate(client, tmp_path):
    """只报 realtime 角色的 Worker（纯实时 CLI / Mac）：不派预转码。"""
    file_id = _seed_file(client, tmp_path, hdr="HDR10")
    token = _pair_worker(client)
    client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id})
    with client.websocket_connect(_WS, headers=_worker_headers(token)) as ws:
        ws.send_json(_hello("win-rt", max_jobs=4, roles=["realtime"]))
        assert ws.receive_json()["type"] == "worker.accepted"
        ws.send_json({"type": "worker.heartbeat", "load": {}})
        assert ws.receive_json()["type"] == "worker.heartbeat.ack"
    assert len(_queued_task_ids(client)) == 1


def test_realtime_jobs_reduce_preload_budget(client, tmp_path):
    """并发 2 的机器：空闲时先推 1 个；此后有实时任务在跑 → 预算 0，新预转码不派。"""
    file_id = _seed_file(client, tmp_path, hdr="HDR10")
    token = _pair_worker(client)
    client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id})
    with client.websocket_connect(_WS, headers=_worker_headers(token)) as ws:
        ws.send_json(_hello("win-d", max_jobs=2, roles=["realtime", "preload"]))
        assert ws.receive_json()["type"] == "worker.accepted"
        # 连接触发派发了第一个任务（并发 2 的一个位）
        first = ws.receive_json()
        assert first["type"] == "job.start" and first["class"] == "preload"
        # 一个实时任务开始在跑（模拟播放会话 reserve）
        registry = get_remote_worker_registry()
        connection = registry._workers["win-d"]
        with registry._lock:
            connection.jobs.add("job-rt-1")
        # 此时再下单：有实时任务在跑，预算 0，新任务留在队列
        file2 = _seed_file(client, tmp_path, hdr="HDR10", resolution="1080p")
        second = client.post(f"{_PRELOAD}/tasks", json={"file_id": file2}).json()["data"]
        ws.send_json({"type": "worker.heartbeat", "load": {}})
        assert ws.receive_json()["type"] == "worker.heartbeat.ack"
        assert second["id"] in _queued_task_ids(client)
        with registry._lock:
            connection.jobs.discard("job-rt-1")


def test_push_accounting_bounds_queue_dispatch(client, tmp_path):
    """队列深于余量时只推余量内数量：不会把整条队列一口气推给同一台 Worker。"""
    file_ids = [_seed_file(client, tmp_path, hdr="HDR10") for _ in range(3)]
    token = _pair_worker(client)
    for file_id in file_ids:
        client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id})
    with client.websocket_connect(_WS, headers=_worker_headers(token)) as ws:
        ws.send_json(_hello("win-f", max_jobs=2, roles=["realtime", "preload"]))
        assert ws.receive_json()["type"] == "worker.accepted"
        # 余量 = 并发 2 − 在途 0 = 2：推两个；第三个留在队列，不会有第三条 job.start
        pushed = [ws.receive_json(), ws.receive_json()]
        assert all(m["type"] == "job.start" and m.get("class") == "preload" for m in pushed)
        assert len(_queued_task_ids(client)) == 1
        assert not {m["task_id"] for m in pushed} & _queued_task_ids(client)


# ---------------------------------------------------------------------------
# Q3 自动调度：Worker 优先 / 本地兜底 / 避让
# ---------------------------------------------------------------------------


def test_online_worker_takes_task_before_local(client, tmp_path, monkeypatch):
    """Worker 优先：有在线 Worker 时任务推给 Worker，本地槽不动。"""
    import movieclaw_api.services.preload.dispatcher as dispatcher

    monkeypatch.setattr(dispatcher, "_local_capacity", lambda: 1)
    ran: list[tuple] = []

    def fake_run(task, file, command):  # 与真 _run_local 同为同步签名
        ran.append((task.id, tuple(command["argv"]) != ()))

    monkeypatch.setattr(dispatcher, "_run_local", fake_run)
    token = _pair_worker(client)
    # hevc 源：本地软编可执行（h264 1080p SDR 源会被判定直拷形态在下单时拒掉）
    file_id = _seed_file(client, tmp_path, hdr=None, codec="hevc", resolution="1080p")
    with client.websocket_connect(_WS, headers=_worker_headers(token)) as ws:
        ws.send_json(_hello("win-first", max_jobs=2, roles=["realtime", "preload"]))
        assert ws.receive_json()["type"] == "worker.accepted"
        # 下单发生在 Worker 已注册之后：派单只会走远程
        created = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id}).json()["data"]
        message = ws.receive_json()
        assert message["type"] == "job.start" and message["task_id"] == created["id"]
        assert not ran  # 本地槽没抢活


def test_local_executor_picks_up_when_no_worker_online(client, tmp_path, monkeypatch):
    """本地兜底：没有任何 Worker 在线时，任务由本地槽执行。"""
    import movieclaw_api.services.preload.dispatcher as dispatcher

    monkeypatch.setattr(dispatcher, "_local_capacity", lambda: 1)
    ran: list[int] = []

    def fake_run(task, file, command):  # 同步签名对齐真 _run_local
        ran.append(task.id)

    monkeypatch.setattr(dispatcher, "_run_local", fake_run)
    file_id = _seed_file(client, tmp_path, hdr=None, codec="hevc", resolution="1080p")
    created = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id}).json()["data"]
    # create 的触发点是丢进事件循环的后台任务；在 portal 里同步补一轮，
    # 保证断言时派单已确定跑完（不依赖后台任务的调度时机）
    client.portal.call(dispatcher.dispatch_queued_tasks)
    assert ran == [created["id"]]


def test_busy_worker_leaves_task_to_local(client, tmp_path, monkeypatch):
    """Worker 在线但有播放在跑（预算 0）：任务落回本地槽，Worker 不收到 job.start。"""
    import movieclaw_api.services.preload.dispatcher as dispatcher

    monkeypatch.setattr(dispatcher, "_local_capacity", lambda: 1)
    ran: list[int] = []

    def fake_run(task, file, command):  # 同步签名对齐真 _run_local
        ran.append(task.id)

    monkeypatch.setattr(dispatcher, "_run_local", fake_run)
    token = _pair_worker(client)
    file_id = _seed_file(client, tmp_path, hdr=None, codec="hevc", resolution="1080p")
    with client.websocket_connect(_WS, headers=_worker_headers(token)) as ws:
        ws.send_json(_hello("win-full", max_jobs=1, roles=["realtime", "preload"]))
        assert ws.receive_json()["type"] == "worker.accepted"
        # 一个播放正在这台 Worker 上转码：预算 0，预转码不推给它
        registry = get_remote_worker_registry()
        with registry._lock:
            registry._workers["win-full"].jobs.add("job-rt-1")
        created = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id}).json()["data"]
        # 心跳确认连接正常，且中间没有 job.start 插队
        ws.send_json({"type": "worker.heartbeat", "load": {}})
        assert ws.receive_json()["type"] == "worker.heartbeat.ack"
        # create 触发的派单是后台任务，这里同步补一轮再断言
        client.portal.call(dispatcher.dispatch_queued_tasks)
        assert ran == [created["id"]]
        with registry._lock:
            registry._workers["win-full"].jobs.discard("job-rt-1")


def test_local_video_copy_never_reaches_queue(client, tmp_path, monkeypatch):
    """直拷形态（h264 720p SDR 源 + 高清档）在下单时就被拒，进不了队列——
    调度器的直拷防御分支正常情况下到不了。"""
    import movieclaw_api.services.preload.dispatcher as dispatcher

    monkeypatch.setattr(dispatcher, "_local_capacity", lambda: 1)
    ran: list[int] = []

    def fake_run(task, file, command):  # 同步签名对齐真 _run_local
        ran.append(task.id)

    monkeypatch.setattr(dispatcher, "_run_local", fake_run)
    file_id = _seed_file(client, tmp_path, hdr=None, codec="h264", resolution="720p")
    response = client.post(
        f"{_PRELOAD}/tasks", json={"file_id": file_id, "preset_id": "premium"}
    )
    assert response.status_code == 400  # 「已经是便携档」直接拒绝
    assert not ran


def test_local_avoids_running_playback_sessions(client, tmp_path, monkeypatch):
    """有播放转码会话在跑：本地让路，任务推给远程 Worker。"""
    import movieclaw_api.services.preload.dispatcher as dispatcher

    monkeypatch.setattr(dispatcher, "_local_capacity", lambda: 0)  # 避让=容量 0
    token = _pair_worker(client)
    file_id = _seed_file(client, tmp_path, hdr=None, codec="h264", resolution="1080p")
    created = client.post(f"{_PRELOAD}/tasks", json={"file_id": file_id}).json()["data"]
    with client.websocket_connect(_WS, headers=_worker_headers(token)) as ws:
        ws.send_json(_hello("win-yield", max_jobs=2, roles=["realtime", "preload"]))
        assert ws.receive_json()["type"] == "worker.accepted"
        message = ws.receive_json()
        assert message["type"] == "job.start" and message["task_id"] == created["id"]


def test_local_capacity_is_zero_without_hardware(monkeypatch):
    """NAS 无硬件（available_local_backends 空）：本地容量恒 0，不接预转码。"""
    import movieclaw_api.services.preload.dispatcher as dispatcher
    from movieclaw_api.services.playback import hwprobe

    monkeypatch.setattr(hwprobe, "available_local_backends", lambda: ())
    monkeypatch.setattr(
        dispatcher, "_LOCAL_RUNNING", {}
    )
    assert dispatcher._local_capacity() == 0


def test_local_capacity_is_zero_while_playback_transcodes(monkeypatch):
    """播放转码会话在跑：本地预转码让路。"""
    import movieclaw_api.services.preload.dispatcher as dispatcher
    from movieclaw_api.services.playback import hwprobe
    from movieclaw_api.services.playback.session import get_session_manager

    monkeypatch.setattr(hwprobe, "available_local_backends", lambda: ("vaapi",))
    sessions = get_session_manager()
    original = sessions.active

    def fake_active():
        from types import SimpleNamespace

        return [SimpleNamespace(tier=3)]  # tier 3 = 硬件转码

    monkeypatch.setattr(sessions, "active", fake_active)
    try:
        assert dispatcher._local_capacity() == 0
    finally:
        monkeypatch.setattr(sessions, "active", original)

