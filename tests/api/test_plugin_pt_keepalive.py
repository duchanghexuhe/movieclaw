"""PT 站点保活形态的插件包端到端（与 plugins/pt-keepalive 同形态）。

真实链路：上传 → 校验（清单申请 site.list / site.proxy.request，须过 check_compat）→
批准 → 独立进程加载 → 定时任务贡献进 SCHEDULED_TASKS → 插件路由进入操作目录 →
插件事件登记 → 站点代请求操作按授权放行。pt-keepalive 本体的判定逻辑单测在
插件仓库（plugins/pt-keepalive/tests/），这里只验证宿主侧对这种包形态的完整放行。
"""

from __future__ import annotations

import io
import sys
import textwrap
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from movieclaw_api.core.config import get_settings
from movieclaw_api.services import durable_events, plugin_packages, plugin_runtime

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="进程外插件 IPC 建立在 Unix domain socket 上，Windows 宿主暂不支持（AF_UNIX 缺失）",
)

PLUGIN = '''
from fastapi import APIRouter

from movieclaw_api.plugins.keys import PLUGIN_DATA, PLUGIN_ROUTES
from movieclaw_scheduler import SCHEDULED_TASKS
from movieclaw_scheduler.registry import TaskDefinition, TriggerType
from movieclaw_sdk import plugin
from movieclaw_sdk.events import PLUGIN_EVENTS, plugin_event
from movieclaw_sdk.ops import HOST_OPS

from pydantic import BaseModel, ConfigDict


class Report(BaseModel):
    model_config = ConfigDict(frozen=True)

    ok: bool


REPORT = plugin_event("keepalive-e2e.report", Report, title="保活日报")


@plugin(
    "keepalive-e2e",
    title="保活端到端",
    inject=(HOST_OPS, PLUGIN_DATA, PLUGIN_EVENTS, PLUGIN_ROUTES),
    permissions=("site.list", "site.proxy.request"),
)
async def apply(ctx) -> None:
    ops = await ctx.use(HOST_OPS).client(ctx)
    store = ctx.use(PLUGIN_DATA).scoped(ctx)
    await store.set("granted", sorted(ops.operations))
    events = await ctx.use(PLUGIN_EVENTS).register(ctx, REPORT)

    routes = ctx.use(PLUGIN_ROUTES)
    router = APIRouter()

    @router.get("/status", operation_id="plugins.keepalive-e2e.status", summary="状态")
    async def status() -> dict:
        return {"ok": True}

    routes.mount(ctx, router)

    async def _round() -> None:
        await store.set("round_ran", True)

    ctx.contribute(
        SCHEDULED_TASKS,
        "keepalive-e2e:run",
        TaskDefinition(
            key="keepalive-e2e:run",
            title="保活轮",
            handler=_round,
            default_trigger_type=TriggerType.CRON,
            default_cron="17 9 * * *",
        ),
    )
'''

MANIFEST = """
[plugin]
id = "keepalive-e2e"
title = "保活端到端"
version = "1.0.0"
entry = "keepalive_e2e"
runtime = "process"
sdk = "^1.0"

[requires]
"scheduled-tasks" = "^1.0"
"plugin-events" = "^1.0"
"plugin-data" = "^1.0"
"plugin-routes" = "^1.0"

[permissions]
operations = ["site.list", "site.proxy.request"]
network = true

[[events]]
name = "keepalive-e2e.report"
title = "保活日报"
"""


def package() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("movieclaw-plugin.toml", MANIFEST)
        archive.writestr("keepalive_e2e.py", textwrap.dedent(PLUGIN))
    return buffer.getvalue()


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'pkg.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("SITE_CONFIGS_DIR", str(tmp_path / "site-configs"))
    monkeypatch.setenv("MOVIECLAW_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setattr(plugin_runtime, "BACKOFF_MAX", 0.05)
    monkeypatch.setattr(plugin_packages, "GRACE_POLL", 0.1)
    get_settings.cache_clear()
    durable_events.reset_state()
    yield tmp_path
    for name in [m for m in sys.modules if m.startswith("movieclaw_packages")]:
        del sys.modules[name]
    durable_events.reset_state()
    get_settings.cache_clear()


def start():
    from movieclaw_api.api.deps import require_admin, require_login
    from movieclaw_api.app import create_app
    from movieclaw_api.services.auth import Principal

    app = create_app()
    admin = Principal(kind="admin", name="tester")
    app.dependency_overrides[require_admin] = lambda: admin
    app.dependency_overrides[require_login] = lambda: admin
    return app, TestClient(app)


def wait_for(predicate, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError("等待超时")


def test_keepalive_shaped_package_full_chain(data_dir):
    app, client = start()
    with client:
        # 上传：清单申请 site.list / site.proxy.request，check_compat 必须过
        r = client.post(
            "/api/v1/app/plugins/packages",
            files={"file": ("keepalive-e2e.mcplugin", package(), "application/zip")},
        )
        assert r.status_code == 200, r.text
        # 批准（操作授权与清单一致）
        r = client.post(
            "/api/v1/app/plugins/packages/keepalive-e2e/approve",
            json={"version": "1.0.0", "operations": ["site.list", "site.proxy.request"]},
        )
        assert r.status_code == 200, r.text

        # 独立进程加载成功：条目 active，插件数据就位（异步装载用 wait_for 等待）
        listing = client.get("/api/v1/app/plugins").json()["data"]
        entry = {e["id"]: e for e in listing["plugins"]}.get("keepalive-e2e")
        assert entry is not None, listing
        assert entry["state"] == "active", entry

        # 插件拿到的授权：site.list 与 site.proxy.request 都在内
        from movieclaw_api.services.plugin_data import PluginStore
        from movieclaw_db.engine import get_database

        store = PluginStore(get_database(), "keepalive-e2e")

        def granted() -> list[str]:
            return store.get("granted") or []

        wait_for(lambda: bool(client.portal.call(granted)))
        granted_ops = client.portal.call(granted)
        assert "site.list" in granted_ops
        assert "site.proxy.request" in granted_ops

        # 定时任务贡献进注册表（get_task + 任务页可见），并真实跑一轮
        from movieclaw_scheduler.registry import get_task

        task = client.portal.call(lambda: get_task("keepalive-e2e:run"))
        assert task is not None and task.default_cron == "17 9 * * *"
        tasks = {t["key"] for t in client.get("/api/v1/scheduled-tasks").json()["data"]}
        assert "keepalive-e2e:run" in tasks

        def round_ran() -> bool:
            return bool(store.get("round_ran"))

        client.portal.call(task.handler)
        wait_for(lambda: client.portal.call(round_ran))

        # 插件路由挂上：status 端点进入宿主操作目录
        from movieclaw_api.services.host_ops import _operation_index

        assert "plugins.keepalive-e2e.status" in _operation_index()

        # 插件事件登记进事件目录（别的插件/Webhook 能订）
        from movieclaw_api.services import plugin_events

        wait_for(lambda: "keepalive-e2e.report" in plugin_events.catalog())

        # 卸载干净
        r = client.post("/api/v1/app/plugins/packages/keepalive-e2e/uninstall")
        assert r.status_code == 200, r.text
