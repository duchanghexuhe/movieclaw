"""「不要杜比视界 Profile 5」的浏览器验收：真实页面 → 接口 → 临时数据库 → 真实匹配 → 详情。

按用户真实的使用顺序走一遍（docs/design/avoid-dv-p5.md）：
1. 设置页「智能选择」：默认关；在「选择说明与最低要求」里勾上，保存后卡片写明
   「不要杜比视界 Profile 5」；
2. 设置页「自定义规则」：规则组的 HDR 一行多一个「DV P5」芯片，点一下排除、再点取消，
   保存后落进规则；
3. 订阅一部片：弹层的结论行写明「不要杜比视界 Profile 5」；站点先后出现 P5（只标 DV）和 P8
   （DV + HDR10）：P5 被跳过且活动里说明原因，P8 被下载。

外部元数据与下载器隔离（tests/e2e/_smart_memory_api_launcher.py），其余都是真实服务。
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from .test_library_manage_browser import REPO, WEB, _chromium_kwargs, _free_port, _wait_http

pw = pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.integration

ADMIN = {"username": "p5-admin", "password": "isolated-test-only"}
SHOTS = Path(os.environ.get("MC_SHOT_DIR", str(REPO / ".tmp-pytest" / "avoid-dv-p5-shots")))
P5_NOTE = "已按「不要杜比视界 Profile 5」跳过"


def _stop(proc: subprocess.Popen) -> None:
    if os.name == "nt":  # next dev 会拉子进程，按树杀
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
        return
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    root = tmp_path_factory.mktemp("avoid-dv-p5-browser")
    api_port, web_port = _free_port(), _free_port()
    api_log = (root / "api.log").open("w")
    web_log = (root / "web.log").open("w")
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite+aiosqlite:///{root / 'data.db'}",
        "SECRET_KEY_FILE": str(root / "secret"),
        "SCHEDULER_ENABLED": "false",
        "SUBSCRIPTION_DISPATCH_DRY_RUN": "true",
        "METADATA_DIR": str(root / "metadata"),
        "LOG_DIR": str(root / "logs"),
        "PYTHONPATH": os.pathsep.join([str(REPO / "src"), str(REPO)]),
    }
    api = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "tests.e2e._smart_memory_api_launcher:app",
         "--host", "127.0.0.1", "--port", str(api_port)],
        cwd=REPO, env=env, stdout=api_log, stderr=subprocess.STDOUT,
    )
    web = subprocess.Popen(
        [shutil.which("pnpm") or "pnpm", "exec", "next", "dev", "--port", str(web_port)],
        cwd=WEB,
        env={**os.environ, "MOVIECLAW_API_PROXY_TARGET": f"http://127.0.0.1:{api_port}",
             "NEXT_DIST_DIR": ".next-avoid-dv-p5-e2e"},
        stdout=web_log, stderr=subprocess.STDOUT,
    )
    try:
        _wait_http(f"http://127.0.0.1:{api_port}/api/v1/auth/bootstrap", 60)
        _wait_http(f"http://127.0.0.1:{web_port}/login", 240)
        yield f"http://127.0.0.1:{web_port}", f"http://127.0.0.1:{api_port}"
    finally:
        for proc in (web, api):
            _stop(proc)
        api_log.close()
        web_log.close()


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, **_chromium_kwargs())
        yield browser
        browser.close()


def shot(page, name):
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.wait_for_timeout(300)
    page.screenshot(path=str(SHOTS / f"{name}.png"))


def test_avoid_dv_p5_from_settings_to_skipped_release(stack, browser):
    base, api = stack
    context = browser.new_context(viewport={"width": 1280, "height": 1000})
    assert context.request.post(f"{base}/api/v1/auth/bootstrap", data=ADMIN).status == 200
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def defaults():
        return context.request.get(f"{base}/api/v1/subscriptions/smart-defaults").json()["data"]

    # ---- 1 智能选择：默认关，勾上后保存 ------------------------------------
    page.goto(f"{base}/settings/subscription")
    smart = page.get_by_role("tabpanel", name="智能选择", exact=True)
    settings = smart.get_by_role("region", name="智能设置", exact=True)
    settings.get_by_text("只需设置一次，电影和剧集都按它来。").wait_for()
    settings.get_by_text("选择说明与最低要求").click()
    switch = settings.get_by_label(re.compile("^不要杜比视界 Profile 5"))
    assert not switch.is_checked(), "默认关：Apple TV 等能原样播 P5 的设备不该被替着拦"
    shot(page, "01-switch-off-by-default")
    switch.check()
    settings.get_by_role("button", name="保存设置").click()
    settings.get_by_text("不要杜比视界 Profile 5", exact=True).wait_for()
    assert defaults()["avoid_dv_p5"] is True
    shot(page, "02-switch-on-summary")

    # ---- 2 自定义规则：HDR 一行的「DV P5」芯片，只在 不限 ↔ 排除 之间切 ----------
    page.get_by_role("tab", name="自定义规则", exact=True).click()
    rules = page.get_by_role("tabpanel", name="自定义规则", exact=True)
    rules.get_by_role("button", name="新建规则组", exact=True).click()
    editor = page.get_by_role("dialog", name="新建规则组", exact=True)
    editor.get_by_placeholder("如：4K 免费、追剧省流", exact=True).fill("不要 DV P5")
    editor.get_by_role("button", name=re.compile("^画质")).click()  # 折叠段里的 HDR 一行
    chip = editor.get_by_role("button", name=re.compile(r"^(✕\s*)?DV P5$"))
    assert "✕" not in chip.inner_text()
    chip.click()
    assert "✕" in chip.inner_text(), "点一下就是排除（没有「只要 P5」这一档）"
    shot(page, "03-rule-chip-excluded")
    chip.click()
    assert "✕" not in chip.inner_text(), "再点取消"
    chip.click()
    editor.get_by_role("button", name="保存", exact=True).click()
    editor.wait_for(state="hidden")
    rows = context.request.get(f"{base}/api/v1/rule-sets").json()["data"]
    saved = next(row for row in rows if row["name"] == "不要 DV P5")
    assert saved["spec"]["hdr_block"] == ["DV P5"]
    assert not saved["spec"].get("hdr_levels"), "P5 只能排除，不会混进白名单"
    # 重新打开：芯片还是排除态
    rules.get_by_role("button", name="不要 DV P5", exact=True).click()
    editor = page.get_by_role("dialog", name="编辑规则组「不要 DV P5」", exact=True)
    editor.get_by_role("button", name=re.compile("^画质")).click()
    assert "✕" in editor.get_by_role("button", name=re.compile(r"DV P5$")).inner_text()
    editor.get_by_role("button", name="取消", exact=True).click()

    # ---- 3 订阅一部片：结论行写明；P5 被跳过、P8 被下载 -----------------------
    page.goto(f"{base}/media/movie/701")
    page.get_by_role("button", name="订阅追踪").first.click()
    card = page.get_by_role("region", name="智能选择")
    card.get_by_text(re.compile("不要杜比视界 Profile 5")).wait_for()
    shot(page, "04-subscribe-dialog-conclusion")
    # 英语片要字幕：弹层按默认答案「要」走，下面的资源都声明了简中字幕
    page.get_by_role("button", name="确认订阅", exact=True).last.click()
    card.wait_for(state="detached")
    rows = context.request.get(f"{base}/api/v1/subscriptions").json()["data"]
    items = rows["items"] if isinstance(rows, dict) else rows
    sub_id = next(s["id"] for s in items if s["media"]["tmdb_id"] == 701)
    detail = context.request.get(f"{base}/api/v1/subscriptions/{sub_id}").json()["data"]
    assert detail["smart_policy"]["avoid_dv_p5"] is True

    p5 = context.request.post(
        f"{api}/__lab/release/movie/701?name=FLUXP5&subs=zh-Hans&hdr=DV&labels=中字,4k,DoVi"
    ).json()
    assert p5["dispatched"] == 0, "只标 DV 的 WEB 资源是 Profile 5，不该下"
    p8 = context.request.post(
        f"{api}/__lab/release/movie/701?name=BYNDRP8&subs=zh-Hans&hdr=DV,HDR10"
        "&labels=4k,hdr10,DoVi"
    ).json()
    assert p8["dispatched"] == 1 and any("BYNDRP8" in t for t in p8["torrents"])

    page.goto(f"{base}/subscriptions/{sub_id}")
    # 拒绝原因记在「排查记录」里（默认折叠）：用户想知道「为什么没下它」时展开就能看到
    page.get_by_role("button", name=re.compile("^排查记录")).click()
    page.get_by_text(re.compile(P5_NOTE)).first.wait_for()
    shot(page, "05-activity-says-why")
    assert not errors, errors
