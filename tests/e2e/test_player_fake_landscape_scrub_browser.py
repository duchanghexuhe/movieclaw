"""iPhone Safari 点「横屏」之后拖进度条——浏览器端到端。

iPhone Safari 没有元素级全屏，播放器的横屏键走的是 CSS 伪横屏（整个容器 rotate(90deg)）。
转过来之后沿进度条拖是**物理竖直方向**，而页面滚动 / 橡皮筋 / 下拉刷新要抢的恰好是这个方向：
被抢走就是 pointercancel，圆点一动不动（2026-10-21 真机反馈，docs/design/player-feel.md §20）。

真后端（uvicorn 子进程）+ 真前端（``pnpm dev``）+ 带 H.264 的 Chrome 真放一段 ffmpeg 现做的
mp4（直出）。页面按手机尺寸开、把元素级全屏按掉（走的就是 iPhone 那条路），手指一律用 CDP
派**真触摸事件**——``page.mouse`` 没有平移手势，这一类它永远验不到。

1. 点横屏键进入伪横屏；沿条从圆点拖到 80%：画面落到 80% 处，没有 pointercancel，进度条上的
   touchstart / touchmove 都被 preventDefault（系统手势抢不走）；
2. 把 touch-action 强行改回 manipulation（模拟 iOS 上 none 被事件区域挖洞 / 没生效）：同样的
   拖动照样成功——修前是一次 pointermove 之后 pointercancel，画面纹丝不动；
3. 拖到一半手指被系统收走（touchcancel）：进度条回到真实位置、不提交落点，并往服务端日志
   报一条 ``scrub-interrupted``（iPhone 上没有控制台，下次再出事就靠它看现场）；
4. 回归：竖屏触屏拖进度条、轻点进度条跳转仍然可用；伪横屏里轻点画面仍能开关控制层。

标 integration：要 pnpm（apps/web 已 install）、ffmpeg 与带 H.264 的 Chrome，CI 不跑。
本地：``E2E_CHROMIUM="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
pytest -m integration tests/e2e/test_player_fake_landscape_scrub_browser.py``。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

playwright = pytest.importorskip("playwright.sync_api")

REPO = Path(__file__).resolve().parents[2]
WEB = REPO / "apps" / "web"
ADMIN = {"username": "admin", "password": "e2e-passw0rd"}
_CHROMIUM_CANDIDATES = (
    os.environ.get("E2E_CHROMIUM"),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/opt/pw-browsers/chromium",
)
_DURATION_S = 400
#: iPhone 竖屏的 CSS 像素尺寸
_PHONE = {"width": 390, "height": 844}
PNPM = shutil.which("pnpm")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(PNPM is None, reason="需要 pnpm 启动前端 dev server"),
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="需要 ffmpeg 生成测试视频"),
    pytest.mark.skipif(
        not (WEB / "node_modules").is_dir(), reason="apps/web 未安装依赖（pnpm install）"
    ),
]

#: iPhone Safari 没有元素级全屏；桌面 Chrome 有，必须按掉才走得到伪横屏这条路
_NO_ELEMENT_FULLSCREEN = """() => {
  const reject = function () { return Promise.reject(new Error('no element fullscreen')); };
  const def = (name, value) =>
    Object.defineProperty(Element.prototype, name, { value, configurable: true });
  def('requestFullscreen', reject);
  def('webkitRequestFullscreen', undefined);
}"""

#: 进度条上的指针 / 触摸事件录像：冒泡到 window 时才记，此时进度条自己的监听早已跑完，
#: 读到的 defaultPrevented 就是页面最终的态度
_RECORDER = """() => {
  window.__bar = [];
  const rec = (event) => {
    const target = event.target;
    if (!(target && target.classList && target.classList.contains('player-scrub'))) return;
    window.__bar.push({
      type: event.type, prevented: event.defaultPrevented, cancelable: event.cancelable,
    });
  };
  for (const type of [
    'pointerdown', 'pointermove', 'pointerup', 'pointercancel',
    'touchstart', 'touchmove', 'touchend', 'touchcancel',
  ]) window.addEventListener(type, rec, { passive: true });
}"""

#: 模拟「touch-action 在 iOS 上没生效」：进度条与所有祖先都改回允许平移
_DEFEAT_TOUCH_ACTION = ".player-root, .player-root * { touch-action: manipulation !important }"

_BAR_GEOMETRY = """() => {
  const input = document.querySelector('.player-scrub').getBoundingClientRect();
  const thumb = document.querySelector('[data-player-thumb]').getBoundingClientRect();
  return {
    left: input.left, top: input.top, width: input.width, height: input.height,
    thumbX: thumb.left + thumb.width / 2, thumbY: thumb.top + thumb.height / 2,
  };
}"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_http(url: str, timeout: float) -> None:
    deadline = time.time() + timeout
    last: Exception | None = None
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=3).status_code < 500:
                return
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(1)
    raise RuntimeError(f"服务未就绪：{url}（{last}）")


def _library_root(path: Path) -> str:
    """媒体库根路径必须以 / 开头；Windows 上去掉盘符（API 与测试片在同一个盘）。"""
    posix = path.resolve().as_posix()
    return posix if posix.startswith("/") else "/" + posix.split(":", 1)[1].lstrip("/")


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    """拉起后端 + 前端，建好库、扫进一部测试片；模块结束时收掉子进程。"""
    root = tmp_path_factory.mktemp("fake-landscape-e2e")
    media = root / "media"
    media.mkdir()
    tmdb_log = root / "tmdb-requests.log"
    tmdb_log.touch()
    api_port, web_port = _free_port(), _free_port()
    data = root / "data"
    data.mkdir()
    env = {
        **os.environ,
        "APP_ENV": "local",
        "APP_RELOAD": "false",
        "APP_PORT": str(api_port),
        "DATABASE_URL": f"sqlite+aiosqlite:///{(data / 'app.db').as_posix()}",
        "METADATA_DIR": str(data / "metadata"),
        "LOG_DIR": str(data / "logs"),
        "SECRET_KEY_FILE": str(data / ".secret_key"),
        "E2E_TMDB_LOG": str(tmdb_log),
    }
    api_log = (root / "api.log").open("w")
    api = subprocess.Popen(  # noqa: S603
        [sys.executable, str(Path(__file__).with_name("_api_launcher.py"))],
        env=env,
        stdout=api_log,
        stderr=subprocess.STDOUT,
        cwd=str(root),
    )
    web_log = (root / "web.log").open("w")
    web = subprocess.Popen(  # noqa: S603
        [PNPM, "dev", "-p", str(web_port)],
        env={
            **os.environ,
            "NEXT_DIST_DIR": ".next-e2e",
            "MOVIECLAW_API_PROXY_TARGET": f"http://127.0.0.1:{api_port}",
        },
        stdout=web_log,
        stderr=subprocess.STDOUT,
        cwd=str(WEB),
    )
    try:
        base = f"http://127.0.0.1:{web_port}"
        _wait_http(f"http://127.0.0.1:{api_port}/api/v1/auth/bootstrap", 90)
        _wait_http(f"{base}/login", 180)
        _make_movie(media / "某电影 (2020).mp4")
        yield {"base": base, "item_id": _seed_library(base, media)}
    finally:
        for proc in (web, api):
            _stop(proc)
        for proc in (web, api):
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
        api_log.close()
        web_log.close()


def _stop(proc: subprocess.Popen) -> None:
    """收掉子进程。Windows 上 terminate() 只杀 pnpm 这一层，next dev 会留在端口上，要连树杀。"""
    if sys.platform == "win32":
        subprocess.run(  # noqa: S603
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],  # noqa: S607
            capture_output=True,
            check=False,
        )
    else:
        proc.terminate()


def _make_movie(path: Path) -> None:
    """低码率的真视频：h264 + aac 的 mp4，浏览器直出，两秒一个关键帧好拖动。"""
    subprocess.run(  # noqa: S603
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate=24:duration={_DURATION_S}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={_DURATION_S}",
            "-c:v", "libx264", "-preset", "ultrafast", "-g", "48", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "48k", "-movflags", "+faststart",
            str(path),
        ],
        check=True,
    )  # fmt: skip


def _seed_library(base: str, media: Path) -> int:
    """建超管、建库（建库即扫描），等测试片入库，返回条目 id。"""
    with httpx.Client(base_url=f"{base}/api/v1", timeout=30) as client:
        client.post("/auth/bootstrap", json=ADMIN)
        assert client.post("/auth/login", json=ADMIN).is_success
        created = client.post(
            "/libraries",
            json={"name": "电影", "kind": "movie", "root_paths": [_library_root(media)]},
        )
        assert created.is_success, created.text
        library_id = created.json()["data"]["id"]
        deadline = time.time() + 90
        while not (items := client.get(f"/libraries/{library_id}/items").json()["data"]):
            assert time.time() < deadline, "扫描没把测试视频入库"
            time.sleep(1)
        return items[0]["media_item_id"]


class _Finger:
    """CDP 真触摸的一根手指。"""

    def __init__(self, context, page) -> None:
        self._cdp = context.new_cdp_session(page)

    def _send(self, kind: str, point: tuple[float, float] | None) -> None:
        points = [] if point is None else [{"x": point[0], "y": point[1]}]
        self._cdp.send("Input.dispatchTouchEvent", {"type": kind, "touchPoints": points})

    def down(self, point: tuple[float, float]) -> None:
        self._send("touchStart", point)

    def move(self, point: tuple[float, float]) -> None:
        self._send("touchMove", point)

    def up(self) -> None:
        self._send("touchEnd", None)

    def cancel(self) -> None:
        """系统把手指收走（通知中心下拉、页面手势接管……）。"""
        self._send("touchCancel", None)

    def drag(self, start: tuple[float, float], end: tuple[float, float], steps: int = 12) -> None:
        self.down(start)
        self.glide(start, end, steps)
        time.sleep(0.2)
        self.up()

    def glide(self, start: tuple[float, float], end: tuple[float, float], steps: int) -> None:
        """按下之后匀速滑过去，不抬手。"""
        for i in range(1, steps + 1):
            fraction = i / steps
            self.move(
                (
                    start[0] + (end[0] - start[0]) * fraction,
                    start[1] + (end[1] - start[1]) * fraction,
                )
            )
            time.sleep(0.03)


def _chromium_kwargs() -> dict:
    for candidate in _CHROMIUM_CANDIDATES:
        if candidate and Path(candidate).exists():
            return {"executable_path": candidate}
    pytest.skip("需要带 H.264 的 Chrome：用 E2E_CHROMIUM 指给它")


@pytest.fixture
def phone(stack):
    """手机尺寸 + 触屏的页面（已登录、已装好事件录像机与「无元素级全屏」垫片）。"""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--autoplay-policy=no-user-gesture-required"],
            **_chromium_kwargs(),
        )
        context = browser.new_context(viewport=_PHONE, has_touch=True, is_mobile=True)
        context.add_init_script(f"({_NO_ELEMENT_FULLSCREEN})()")
        context.add_init_script(f"({_RECORDER})()")
        page = context.new_page()
        page.set_default_timeout(30_000)
        page.set_default_navigation_timeout(120_000)
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        resp = page.request.post(f"{stack['base']}/api/v1/auth/login", data=ADMIN)
        assert resp.ok, f"登录失败：{resp.status} {resp.text()}"
        page.goto(f"{stack['base']}/play/{stack['item_id']}", wait_until="domcontentloaded")
        page.wait_for_function(
            "() => { const v = document.querySelector('video');"
            " return v && v.readyState >= 2 && v.currentTime > 0.5; }",
            timeout=120_000,
        )
        yield page, _Finger(context, page)
        assert errors == [], errors
        browser.close()


def _show_controls(page) -> None:
    """轻点画面，直到控制层露出（初始就是露着的，点一下反而收起来）。"""
    for _ in range(3):
        if page.locator('.player-root[data-chrome="visible"]').count():
            return
        box = page.evaluate(
            "() => { const b = document.querySelector('video').getBoundingClientRect();"
            " return { x: b.left + b.width / 4, y: b.top + b.height / 4 }; }"
        )
        page.touchscreen.tap(box["x"], box["y"])
        time.sleep(0.6)
    raise AssertionError("控制层唤不出来")


def _enter_fake_landscape(page) -> None:
    """点横屏键。没有元素级全屏的 iPhone 走的就是这条：整个容器转 90°。"""
    _show_controls(page)
    page.click('button[aria-label="横屏"]', timeout=5000)
    page.wait_for_function(
        "() => document.querySelector('.player-root').classList.contains('player-fake-landscape')"
    )
    time.sleep(0.8)


def _along(page, *, vertical: bool, ratio: float | None = None) -> dict:
    """进度条在屏幕上的走向：沿条的起点 / 长度、垂直于条的固定坐标，以及圆点所在的位置。

    ``vertical``：伪横屏里条是物理竖直的（沿条 = 沿 y）。``ratio`` 给了就顺带算出该进度处的触点。
    """
    geo = page.evaluate(_BAR_GEOMETRY)
    if vertical:
        info = {
            "fixed": geo["left"] + geo["width"] / 2,
            "start": geo["top"],
            "length": geo["height"],
            "thumb": geo["thumbY"],
        }
    else:
        info = {
            "fixed": geo["top"] + geo["height"] / 2,
            "start": geo["left"],
            "length": geo["width"],
            "thumb": geo["thumbX"],
        }

    def point(along: float) -> tuple[float, float]:
        return (info["fixed"], along) if vertical else (along, info["fixed"])

    info["thumb_point"] = point(info["thumb"])
    if ratio is not None:
        info["target_point"] = point(info["start"] + info["length"] * ratio)
    return info


def _current_time(page) -> float:
    return page.evaluate("() => document.querySelector('video').currentTime")


def _bar_events(page) -> list[dict]:
    return page.evaluate("() => window.__bar")


def _assert_landed(page, ratio: float) -> None:
    """拖完之后画面落在 ratio 处（关键帧两秒一个，抬手后又放了一阵，留一点余量）。"""
    time.sleep(1.5)
    target = _DURATION_S * ratio
    assert abs(_current_time(page) - target) < 8, (
        f"拖到 {ratio:.0%}（{target:.0f}s），画面却在 {_current_time(page):.1f}s"
    )


def test_fake_landscape_drag_claims_the_touch_and_seeks(phone) -> None:
    """伪横屏里从圆点沿条拖到 80%：画面跟过去，系统手势一次都没抢走。"""
    page, finger = phone
    _enter_fake_landscape(page)

    # 进度条与容器的 touch-action 都是 none（容器那条保证里面所有元素的有效值都交集成 none）
    actions = page.evaluate(
        "() => ({ bar: getComputedStyle(document.querySelector('.player-scrub')).touchAction,"
        " root: getComputedStyle(document.querySelector('.player-root')).touchAction })"
    )
    assert actions == {"bar": "none", "root": "none"}, actions

    bar = _along(page, vertical=True, ratio=0.8)
    page.evaluate("() => { window.__bar.length = 0; }")
    finger.drag(bar["thumb_point"], bar["target_point"])
    _assert_landed(page, 0.8)

    events = _bar_events(page)
    kinds = {e["type"] for e in events}
    assert "pointercancel" not in kinds and "touchcancel" not in kinds, events
    assert {"pointerdown", "pointermove", "pointerup"} <= kinds, events
    # 触摸本身被页面认领：起手和每一次可取消的移动都 preventDefault 了
    claimed = [e for e in events if e["type"] in ("touchstart", "touchmove") and e["cancelable"]]
    assert claimed and all(e["prevented"] for e in claimed), claimed
    assert any(e["type"] == "touchstart" for e in claimed), "起手的 touchstart 没被认领"


def test_fake_landscape_drag_survives_touch_action_being_defeated(phone) -> None:
    """touch-action 在 iOS 上被事件区域挖洞 / 没生效时，沿条拖照样不被系统手势收走。"""
    page, finger = phone
    _enter_fake_landscape(page)
    page.add_style_tag(content=_DEFEAT_TOUCH_ACTION)
    assert (
        page.evaluate("() => getComputedStyle(document.querySelector('.player-scrub')).touchAction")
        == "manipulation"
    )

    bar = _along(page, vertical=True, ratio=0.8)
    page.evaluate("() => { window.__bar.length = 0; }")
    finger.drag(bar["thumb_point"], bar["target_point"])
    _assert_landed(page, 0.8)
    assert "pointercancel" not in {e["type"] for e in _bar_events(page)}, _bar_events(page)

    # 再从 80% 处的圆点往回拖到 20%：两个方向都得行
    bar = _along(page, vertical=True, ratio=0.2)
    finger.drag(bar["thumb_point"], bar["target_point"])
    _assert_landed(page, 0.2)


def test_scrub_taken_over_by_the_system_is_reported_and_not_committed(phone) -> None:
    """拖到一半手指被系统收走：不提交落点、进度条回到真实位置，并往服务端日志报一条。"""
    page, finger = phone
    reports: list[dict] = []

    def on_request(request) -> None:
        if request.method == "POST" and request.url.endswith("/playback/client-log"):
            reports.append(json.loads(request.post_data or "{}"))

    page.on("request", on_request)
    _enter_fake_landscape(page)

    bar = _along(page, vertical=True, ratio=0.8)
    finger.down(bar["thumb_point"])
    halfway = (bar["fixed"], (bar["thumb"] + bar["target_point"][1]) / 2)
    finger.glide(bar["thumb_point"], halfway, 5)
    finger.cancel()
    # 拖动中画面本来就跟着手指（直出档跳转不要钱），收走那一刻画面在哪就是哪
    at_cancel = _current_time(page)

    deadline = time.time() + 5
    while not [r for r in reports if r.get("event") == "scrub-interrupted"]:
        assert time.time() < deadline, f"没有收到 scrub-interrupted：{reports}"
        time.sleep(0.2)
    detail = next(r for r in reports if r["event"] == "scrub-interrupted")["detail"]
    assert detail["fake_landscape"] is True and detail["pointer_type"] == "touch", detail
    assert isinstance(detail["held_ms"], int) and detail["touch_action"] == "none", detail

    # 手势作废：不提交落点（不会往 80% 处跳），圆点退回真实的播放位置
    time.sleep(1.5)
    assert abs(_current_time(page) - at_cancel) < 10, "被收走的拖动不该再提交落点"
    shown = page.evaluate(
        "() => parseFloat(document.querySelector('[data-player-thumb]').style.left)"
    )
    actual = _current_time(page) / _DURATION_S * 100
    assert abs(shown - actual) < 3, f"圆点 {shown:.1f}% 没回到真实位置 {actual:.1f}%"


def test_portrait_touch_drag_and_tap_still_seek(phone) -> None:
    """回归：竖屏里认领触摸之后，沿条拖与轻点条跳转都照常。"""
    page, finger = phone
    _show_controls(page)
    bar = _along(page, vertical=False, ratio=0.7)
    page.evaluate("() => { window.__bar.length = 0; }")
    finger.drag(bar["thumb_point"], bar["target_point"])
    _assert_landed(page, 0.7)
    assert "pointercancel" not in {e["type"] for e in _bar_events(page)}

    # 轻点条上 25% 处：按下抬起之间没有移动，也得跳过去
    tap = _along(page, vertical=False, ratio=0.25)["target_point"]
    finger.down(tap)
    time.sleep(0.05)
    finger.up()
    _assert_landed(page, 0.25)


def test_fake_landscape_tap_on_picture_still_toggles_controls(phone) -> None:
    """回归：容器上写了 touch-action: none，轻点画面开关控制层的 click 仍要来。"""
    page, _finger = phone
    _enter_fake_landscape(page)
    assert page.locator('.player-root[data-chrome="visible"]').count() == 1
    # 画面左侧靠上的空白处（物理上是竖屏的上半、水平居中），避开中央簇与两端的按钮
    page.touchscreen.tap(_PHONE["width"] / 2, 150)
    page.wait_for_selector('.player-root[data-chrome="hidden"]', state="attached", timeout=5000)
    time.sleep(1)  # 两下挨得太近就成了双击（左三分之一 = 退十秒），不是开关
    page.touchscreen.tap(_PHONE["width"] / 2, 150)
    page.wait_for_selector('.player-root[data-chrome="visible"]', state="attached", timeout=5000)
