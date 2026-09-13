"""网页媒体库总开关（library.web）测试。

关闭后浏览/播放面整体 404（管理员也一样），而库的管理/整理端点照常
可用；/auth/me 下发开关快照；开关读写端点仅管理员可用。订阅/下载/
刮削链路不经这些路由，由各自专项测试保证，不在这里重复。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from movieclaw_api.core.config import get_settings
from movieclaw_api.settings.store import reset_setting_store


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    get_settings.cache_clear()
    reset_setting_store()

    from movieclaw_api.api.routes import libraries as library_routes
    from movieclaw_api.app import create_app

    async def skip_initial_scan(*_args, **_kwargs) -> None:  # noqa: ANN002, ANN003
        """配置类用例不执行异步扫描（扫描由 test_library_scan 覆盖）。"""

    monkeypatch.setattr(library_routes, "enqueue_scan_job", skip_initial_scan)

    app = create_app()
    with TestClient(app) as c:
        c.post(
            "/api/v1/auth/bootstrap",
            json={"username": "admin", "password": "s3cret-pass"},
        )
        yield c

    get_settings.cache_clear()
    reset_setting_store()


def _create_library(client: TestClient) -> dict:
    resp = client.post(
        "/api/v1/libraries",
        json={"name": "电影库", "kind": "movie", "root_paths": ["/media/movies"]},
    )
    assert resp.status_code == 200
    return resp.json()["data"]


def _set_enabled(client: TestClient, enabled: bool) -> None:
    resp = client.put("/api/v1/libraries/feature", json={"enabled": enabled})
    assert resp.status_code == 200
    assert resp.json()["data"]["enabled"] is enabled


def test_feature_defaults_to_enabled(client) -> None:
    assert client.get("/api/v1/libraries/feature").json()["data"]["enabled"] is True


def test_disabled_gates_browsing_and_playback_apis(client) -> None:
    library = _create_library(client)
    lid = library["id"]
    # 开关打开时浏览端点可达（空库返回 200）
    assert client.get(f"/api/v1/libraries/{lid}/items").status_code == 200

    _set_enabled(client, False)

    # 库内容浏览面：海报墙/条目详情/图廊/分集（管理员也 404）
    for path in (
        f"/api/v1/libraries/{lid}/items",
        f"/api/v1/libraries/{lid}/item-ids",
        f"/api/v1/libraries/{lid}/item-index",
        f"/api/v1/libraries/{lid}/gallery",
        f"/api/v1/libraries/{lid}/facets",
        f"/api/v1/libraries/{lid}/relax",
        f"/api/v1/libraries/{lid}/items/1",
        f"/api/v1/libraries/{lid}/items/1/episodes",
        "/api/v1/search/library-items",
    ):
        assert client.get(path).status_code == 404, path

    # 播放面：浏览端点与策略端点一并下线
    for path in (
        "/api/v1/playback/up-next",
        "/api/v1/playback/favorites",
        "/api/v1/playback/history",
        "/api/v1/playback/policy",
        "/api/v1/playback/items/1",
    ):
        assert client.get(path).status_code == 404, path

    # 合集与库内影人（纯浏览聚合）
    assert client.get("/api/v1/collections").status_code == 404
    assert client.get("/api/v1/people/12345").status_code == 404


def test_disabled_keeps_management_and_organize_apis(client) -> None:
    """关闭开关不断整理链路：库列表/建库/详情/待识别/回收站照常可用。"""
    _set_enabled(client, False)

    assert client.get("/api/v1/libraries").status_code == 200
    library = _create_library(client)
    assert client.get(f"/api/v1/libraries/{library['id']}").status_code == 200
    assert (
        client.get("/api/v1/libraries/identification/unidentified-files").status_code == 200
    )
    assert client.get("/api/v1/libraries/trashed-files").status_code == 200


def test_session_view_carries_library_flag(client) -> None:
    """/auth/me 下发开关快照，前端据此裁剪入口。"""
    assert client.get("/api/v1/auth/me").json()["data"]["library_enabled"] is True
    _set_enabled(client, False)
    assert client.get("/api/v1/auth/me").json()["data"]["library_enabled"] is False


def test_reenabling_restores_apis(client) -> None:
    library = _create_library(client)
    _set_enabled(client, False)
    assert client.get(f"/api/v1/libraries/{library['id']}/items").status_code == 404
    _set_enabled(client, True)
    assert client.get(f"/api/v1/libraries/{library['id']}/items").status_code == 200


def test_feature_endpoints_reject_anonymous(client) -> None:
    client.cookies.clear()
    assert client.get("/api/v1/libraries/feature").status_code == 401
    assert (
        client.put("/api/v1/libraries/feature", json={"enabled": True}).status_code == 401
    )
