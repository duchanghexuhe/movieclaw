"""backend_health：起步即死后端的熔断登记。"""

from __future__ import annotations

import time

import pytest

from movieclaw_api.services.playback import backend_health


@pytest.fixture(autouse=True)
def _clean():
    backend_health.reset()
    yield
    backend_health.reset()


def test_demoted_backend_is_visible_until_ttl_expires(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(backend_health.time, "monotonic", lambda: clock["now"])

    backend_health.demote("vaapi")
    assert backend_health.is_demoted("vaapi")
    assert backend_health.demoted_backends() == {"vaapi"}

    clock["now"] += backend_health.DEMOTE_SECONDS - 1
    assert backend_health.is_demoted("vaapi")

    clock["now"] += 2
    assert not backend_health.is_demoted("vaapi")
    # 过期条目在下次读取时被清掉
    assert backend_health.demoted_backends() == set()


def test_reset_clears_single_backend_and_all():
    backend_health.demote("vaapi")
    backend_health.demote("qsv")
    backend_health.reset("vaapi")
    assert not backend_health.is_demoted("vaapi")
    assert backend_health.is_demoted("qsv")
    backend_health.reset()
    assert backend_health.demoted_backends() == set()


def test_reset_only_takes_effect_from_demotion_time(monkeypatch):
    """reset 之后旧的 demoted_at 不得复活（用真实时间语义守护实现）。"""
    backend_health.demote("vaapi")
    real_monotonic = time.monotonic
    monkeypatch.setattr(backend_health.time, "monotonic", real_monotonic)
    backend_health.reset("vaapi")
    assert not backend_health.is_demoted("vaapi")
