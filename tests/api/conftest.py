"""API 测试的公共夹具。"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_preload_local_executor(request, monkeypatch):
    """预转码的 NAS 本地执行器在测试里默认不接单。

    没有硬件时 NAS 也会用 libx264 软编接单（preload-transcode.md §7：本地是基石），测试环境
    恰好就是「没有硬件」——不关掉它，每个下单的用例都会被后台协程抢先领走任务、对着假片源起
    ffmpeg。专测本地调度的用例标 ``@pytest.mark.preload_local``；只想让本地有余量的用例照旧在
    用例里 monkeypatch ``_local_capacity``。
    """
    if request.node.get_closest_marker("preload_local"):
        return
    from movieclaw_api.services.preload import dispatcher

    monkeypatch.setattr(dispatcher, "_local_capacity", lambda: 0)
    monkeypatch.setattr(dispatcher, "_ensure_watchdog", lambda: None)
