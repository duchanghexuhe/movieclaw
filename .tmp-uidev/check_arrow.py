"""验证 Netflix 主题发现页横滚行右翻页箭头在 rAF 正常的浏览器里的静息可见性。

跑法：python check_arrow.py（dev server 已在 3100 运行）
"""
import json
import sys

from playwright.sync_api import sync_playwright

RESULT = {}


def arrow_states(page):
    return page.evaluate(
        """() => [...document.querySelectorAll('button[aria-label="向右滚动"]')]
            .slice(0, 3)
            .map(b => {
                const r = b.getBoundingClientRect();
                const scroller = b.parentElement.querySelector('.overflow-x-auto');
                return {
                    y: Math.round(r.y),
                    opacity: getComputedStyle(b).opacity,
                    display: getComputedStyle(b).display,
                    scrollW: scroller.scrollWidth,
                    clientW: scroller.clientWidth,
                };
            })
    """
    )


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1600, "height": 900})
    page.goto("http://localhost:3100/login?next=%2Fdiscover%2Ftv")
    page.wait_for_load_state("networkidle")
    # 登录
    boxes = page.get_by_role("textbox")
    boxes.nth(0).fill("admin")
    boxes.nth(1).fill("19950320")
    boxes.nth(1).press("Enter")
    page.wait_for_url("**/discover/tv", timeout=15000)
    page.wait_for_timeout(4000)  # 等行数据与图片布局稳定

    RESULT["at_rest_row1"] = arrow_states(page)

    # 向下滚一段，让下面几行进入视口（模拟用户滚动后不悬停直接看）
    page.mouse.wheel(0, 600)
    page.wait_for_timeout(1200)
    RESULT["after_scroll_row1"] = arrow_states(page)

    # 悬停第一行再看（onPointerEnter 重测通路）
    page.mouse.move(800, 400)
    page.wait_for_timeout(800)
    RESULT["after_hover"] = arrow_states(page)

    browser.close()

print(json.dumps(RESULT, ensure_ascii=False, indent=1))
sys.exit(0)
