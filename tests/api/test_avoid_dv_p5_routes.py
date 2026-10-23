"""「不要杜比视界 Profile 5」智能开关：真实 HTTP 接口 → 服务 → 临时数据库。

重点是兼容：旧客户端不认识这个字段，保存别的设置时绝不能顺手把它改回关；
关着的时候落库内容与升级前逐字相同（回退旧版本、存量订阅都不受影响）。
docs/design/avoid-dv-p5.md
"""

# ruff: noqa: F811
from __future__ import annotations

from tests.api.test_smart_memory_routes import (  # noqa: F401
    BASE,
    _data,
    _defaults,
    _preview,
    _subscribe,
    client,
)


def _activities(client, subscription_id):
    return _data(client.get(f"{BASE}/{subscription_id}/activities"))


def test_defaults_carry_the_switch_and_old_clients_cannot_turn_it_off(client):
    assert _data(client.get(f"{BASE}/smart-defaults"))["avoid_dv_p5"] is False
    assert _defaults(client)["avoid_dv_p5"] is False

    on = _defaults(client, avoid_dv_p5=True)
    assert on["avoid_dv_p5"] is True
    # 对话里一并带进了说明：订阅弹层的结论行看得到
    plan = _preview(client, "tmdb:movie:701")
    assert plan["wait_label"].endswith("达标后停止洗版 · 不要杜比视界 Profile 5")

    # 旧客户端保存别的设置：请求体里没有这个字段 → 沿用，不是改回关
    after_old_client = _defaults(client, resolution="1080p")
    assert after_old_client["resolution"] == "1080p"
    assert after_old_client["avoid_dv_p5"] is True

    # 显式传 false 才关
    assert _defaults(client, avoid_dv_p5=False)["avoid_dv_p5"] is False
    assert _preview(client, "tmdb:movie:701")["wait_label"].endswith("达标后停止洗版")


def test_per_kind_legacy_route_keeps_the_switch_too(client):
    _defaults(client, avoid_dv_p5=True)
    for kind in ("movie", "tv"):
        profile = _data(client.get(f"{BASE}/smart-profiles/{kind}"))
        assert profile["preferences"]["avoid_dv_p5"] is True
        # 按类型的旧接口只会带它认识的五个字段
        saved = _data(
            client.put(
                f"{BASE}/smart-profiles/{kind}",
                json={
                    "revision": profile["revision"],
                    "preferences": {
                        "resolution": "2160p",
                        "source": "web-dl",
                        "wait_seconds": 7200,
                        "allow_upgrade": True,
                        "strict_resolution": False,
                    },
                },
            )
        )
        assert saved["preferences"]["avoid_dv_p5"] is True
    # 新写法显式说关，才关
    profile = _data(client.get(f"{BASE}/smart-profiles/movie"))
    saved = _data(
        client.put(
            f"{BASE}/smart-profiles/movie",
            json={
                "revision": profile["revision"],
                "preferences": {**profile["preferences"], "avoid_dv_p5": False},
            },
        )
    )
    assert "avoid_dv_p5" not in saved["preferences"]


def test_switch_is_frozen_into_new_subscriptions_and_followed_by_existing_ones(client):
    _defaults(client)
    plan = _preview(client, "tmdb:movie:705")
    waiting = _subscribe(client, "tmdb:movie:705", plan)
    # 关着：落库的策略里没有这个键，与升级前逐字相同
    assert "avoid_dv_p5" not in waiting["smart_policy"]

    _defaults(client, avoid_dv_p5=True)
    detail = _data(client.get(f"{BASE}/{waiting['id']}"))
    assert detail["smart_policy"]["avoid_dv_p5"] is True  # 在等的订阅立刻跟随
    messages = [a["message"] for a in _activities(client, waiting["id"])]
    assert any("已跟随当前智能设置与记忆" in m and "不要杜比视界 Profile 5" in m for m in messages)

    created = _subscribe(client, "tmdb:tv:804", _preview(client, "tmdb:tv:804"))
    assert created["smart_policy"]["avoid_dv_p5"] is True

    _defaults(client, avoid_dv_p5=False)
    detail = _data(client.get(f"{BASE}/{waiting['id']}"))
    assert "avoid_dv_p5" not in detail["smart_policy"]
