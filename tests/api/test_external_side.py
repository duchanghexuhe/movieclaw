"""外网判定的口径统一（docs/design/server-addresses.md §5、preload-transcode.md）。

播放侧有两处「这个请求算不算外网」的判定：``_uplink_bps_for``（外网上行降档预算）
和 ``_is_external_request``（外网省流：转码会话固定 AAC 双声道、便携版排首位）。
两者过去都按对端 IP 判，反代 / 隧道 / Docker NAT 把地址抹成私网时会一律认成
「在家」——正是 #947 修掉的那个毛病。现在统一走 ``network_side.request_side``。

这里锁的是**三态语义**：只有明确 ``home`` 才按内网处理，``unknown`` 按外面处理，
与 ``uplink.note`` / ``choose_direct`` 同一取舍。
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from starlette.requests import HTTPConnection

from movieclaw_api.api.routes import playback as playback_route
from movieclaw_api.core.config import get_settings
from movieclaw_api.settings.store import (
    get_setting_store,
    init_setting_store,
    reset_setting_store,
)
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_jellyfin.routes import playback as jellyfin_playback


@pytest_asyncio.fixture
async def settings_db(tmp_path, monkeypatch):
    """只要配置存储的最小库：本文件不需要媒体条目。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'side.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    reset_setting_store()
    init_setting_store(database=get_database())
    yield get_database()
    reset_setting_store()
    await dispose_db()
    get_settings.cache_clear()


def _conn(headers: dict[str, str]) -> HTTPConnection:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return HTTPConnection({"type": "http", "headers": raw})


# 与 tests/api/test_server_addresses.py::test_request_side 同一组入口事实，
# 这里只关心它们翻译成「算不算外网」的结果。
@pytest.mark.parametrize(
    ("headers", "external"),
    [
        # 家里直接用局域网地址：内网
        ({"Host": "192.168.1.10:3300", "X-Forwarded-For": "192.168.1.23"}, False),
        # 外层反代改写 Host、转发链里有公网：外网
        ({"Host": "192.168.1.10:3000", "X-Forwarded-For": "1.2.3.4, 10.10.10.2"}, True),
        # 家里用外网域名（NAT 回环）：按外面，代价是多压一次音轨
        ({"Host": "movie.example.com"}, True),
        # 自家 App 声明，优先级最高：经 VPN 连内网地址其实在外面
        ({"Host": "192.168.1.10:3300", "X-MovieClaw-Route": "vpn"}, True),
        ({"Host": "movie.example.com", "X-MovieClaw-Route": "lan"}, False),
        # 认不准（没有任何入口信息）：按外面处理
        ({}, True),
    ],
)
def test_playback_external_follows_network_side(headers, external):
    assert playback_route._is_external_request(_conn(headers)) is external


def test_jellyfin_external_uses_the_same_rule():
    """Jellyfin 兼容层与网页端同一口径，否则同一台设备两处看到不同版本。"""
    home = _conn({"Host": "192.168.1.10:3300"})
    away = _conn({"Host": "movie.example.com"})
    unknown = _conn({})

    assert jellyfin_playback._is_external(home) is False
    assert jellyfin_playback._is_external(away) is True
    assert jellyfin_playback._is_external(unknown) is True


async def test_uplink_budget_is_none_only_when_home(settings_db):
    """外网降档预算：在家一律 None；认不准时按外面算，配置了上行就给预算。"""
    from movieclaw_api.settings import AppServerSetting

    store = get_setting_store()
    original = await store.get(AppServerSetting)
    await store.set(original.model_copy(update={"uplink_mbps": 30}))

    try:
        assert await playback_route._uplink_bps_for(
            _conn({"Host": "192.168.1.10:3300"})
        ) is None
        assert await playback_route._uplink_bps_for(_conn({})) == 30_000_000
    finally:
        await store.set(original)
