"""开发工具:用无头浏览器真实通关两条路线,并为 README 截图。

截的是真实游戏画面(不是 mock):AI 旁白、自由行动的意图解析、被锁住的选项、结局页与结局收集。
需要先启动游戏(`python run.py`)并配置好 Key,否则截不到 AI 旁白。

依赖(仅开发用,见 requirements-dev.txt):playwright。使用系统已安装的 Edge / Chrome,不需要额外下载浏览器内核。

用法(项目根目录):
    python -m tools.take_screenshots                       # 默认 http://127.0.0.1:8000,输出到 docs/screenshots
    python -m tools.take_screenshots --url http://127.0.0.1:8000 --channel chrome
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
STORY = json.loads((ROOT / "app" / "story" / "story.json").read_text(encoding="utf-8"))

# 路线里的 choice id -> 按钮上的文字(用前缀匹配,避开引号等特殊字符)
LABELS: dict[str, str] = {}
for node in STORY["nodes"].values():
    for choice in node.get("choices", []):
        LABELS[choice["id"]] = choice["label"]

ROUTE_TRUTH = ["start", "cut_song", "to_archive", "archive_back", "riverside", "riverside_go", "play_tape"]
ROUTE_SWITCH = ["start", "cut_song", "to_bureau", "bureau_back", "mountain", "mountain_go", "report_evac"]

FREE_ACT_TEXT = "我朝陈姐点点头,先放首歌,等会儿私下给他回电话"
SETTLE_MS = 1800  # 选项淡入、背景切换、打字机收尾的缓冲
VIEWPORT = {"width": 1280, "height": 900}  # 够高:水闸站 4 个选项 + 输入框 + 场景标题("AI 旁白"标记)都不必滚动


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def scene_title(page: Page) -> str:
    return page.locator("#sceneTitle").inner_text()


def wait_choices(page: Page, timeout: int = 70_000) -> None:
    page.wait_for_selector("#choices .choice", state="visible", timeout=timeout)


def skip_narration(page: Page) -> None:
    """不需要截图的场景:直接跳过打字机(同时放弃本次 AI 生成,省时间)。"""
    if page.locator("#choices .choice").count() == 0:
        page.locator("#btnSkip").click()
    wait_choices(page, 15_000)


def wait_title_change(page: Page, before: str, timeout: int = 20_000) -> None:
    page.wait_for_function(
        "(old) => document.getElementById('sceneTitle').textContent !== old", arg=before, timeout=timeout
    )


def click_choice(page: Page, choice_id: str) -> None:
    """点击选项并等到新场景出现(场景标题变化 = showScene 已经清空旧选项)。"""
    prefix = LABELS[choice_id][:14]
    before = scene_title(page)
    page.locator("#choices .choice", has_text=prefix).first.click()
    wait_title_change(page, before)


def shot(page: Page, out: Path, name: str, *, full_height: bool = False) -> None:
    if full_height:  # 结局页较长:把视口拉高一次性截全
        height = page.evaluate("document.querySelector('.ending-card').getBoundingClientRect().height")
        page.set_viewport_size({"width": VIEWPORT["width"], "height": max(VIEWPORT["height"], int(height) + 90)})
        page.wait_for_timeout(600)
    page.mouse.move(2, 2)  # 把鼠标移出内容区,避免上一次点击的位置让某个选项显示成悬停高亮
    page.wait_for_timeout(400)
    path = out / name
    page.screenshot(path=str(path), type="jpeg", quality=88)
    log(f"已保存 {path.relative_to(ROOT)}  ({path.stat().st_size // 1024} KB)")
    if full_height:
        page.set_viewport_size(VIEWPORT)


def play_until(page: Page, route: list[str], stop_before: str | None) -> None:
    """依次点选路线上的选项,直到遇到 stop_before(不点它)。中间的场景全部跳过动画。"""
    for choice_id in route:
        if choice_id == stop_before:
            return
        skip_narration(page)
        click_choice(page, choice_id)


def open_ending(page: Page, out: Path, name: str) -> None:
    """走完最后一个场景 → 点"落幕" → 等 AI 后日谈写完 → 截图。"""
    log(f"到达结局场景:{scene_title(page)}")
    skip_narration(page)
    page.locator("#choices .choice").first.click()  # “落幕 · 查看这一夜的结局”
    page.wait_for_selector("#ending:not(.hidden)", timeout=10_000)
    page.wait_for_function("document.getElementById('epilogueSource').textContent.length > 0", timeout=70_000)
    source = page.locator("#epilogueSource").inner_text()
    log(f"后日谈来源:{source}")
    page.wait_for_timeout(SETTLE_MS)
    shot(page, out, name, full_height=True)


def main(url: str, out: Path, channel: str) -> int:
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel=channel, headless=True,
                                    args=["--autoplay-policy=no-user-gesture-required"])
        context = browser.new_context(viewport=VIEWPORT, locale="zh-CN")
        page = context.new_page()
        page.on("pageerror", lambda e: log(f"[页面错误] {e}"))

        # 1. 标题页
        page.goto(url)
        page.wait_for_function("!document.getElementById('btnStart').disabled", timeout=15_000)
        badge = page.locator("#aiBadge").inner_text()
        log(f"标题页徽章:{badge}")
        if "离线" in badge:
            log("警告:服务当前是离线模式(没有 Key),截不到 AI 旁白。")
        page.wait_for_timeout(SETTLE_MS)
        shot(page, out, "01-title.jpg")

        # 2. 路线 A(真话结局):序幕 + 工作室(AI 旁白 + 自由行动)
        # 免费模型会被限流,这一幕偶尔会降级成预写旁白;截图要展示 AI 旁白,所以最多重开 3 局
        for attempt in range(1, 4):
            if attempt > 1:
                page.goto(url)
                page.wait_for_function("!document.getElementById('btnStart').disabled", timeout=15_000)
            page.locator("#btnStart").click()
            skip_narration(page)  # 序幕
            click_choice(page, "start")
            wait_choices(page)  # 工作室:等 AI 旁白写完
            title = scene_title(page)
            log(f"工作室场景(第 {attempt} 次):{title}")
            if "AI 旁白" in title:
                break
            log("这一幕被限流降级成了预写旁白,重开一局再试。")
        else:
            log("警告:3 次都没拿到 AI 旁白,02/03 号截图将显示预写版本,建议稍后重跑。")
        page.wait_for_timeout(SETTLE_MS)
        shot(page, out, "02-scene-ai-narration.jpg")

        page.locator("#actInput").fill(FREE_ACT_TEXT)
        before = scene_title(page)
        page.locator("#act button").click()
        page.wait_for_function(
            "() => { const r = document.getElementById('reaction');"
            " return !r.classList.contains('hidden') && r.textContent && !r.textContent.includes('正在理解'); }",
            timeout=30_000,
        )
        page.wait_for_timeout(400)
        log(f"导演回应:{page.locator('#reaction').inner_text()}")
        shot(page, out, "03-free-action.jpg")
        wait_title_change(page, before)

        # 路线 A 剩余部分(已经通过自由行动选了 cut_song)
        play_until(page, ROUTE_TRUTH[2:], stop_before=None)
        open_ending(page, out, "05-ending-truth.jpg")

        # 3. 路线 B(开闸结局):在水闸站拍"被锁住的选项"
        before = scene_title(page)
        page.locator("#btnAgain").click()
        wait_title_change(page, before)  # 新一局的序幕出现
        play_until(page, ROUTE_SWITCH, stop_before="report_evac")
        wait_choices(page)  # 水闸站:等 AI 旁白写完
        title = scene_title(page)
        log(f"水闸站场景:{title}")
        if "AI 旁白" not in title:
            log("警告:水闸站没有拿到 AI 旁白,建议稍后重跑。")
        page.wait_for_timeout(SETTLE_MS)
        shot(page, out, "04-locked-choice.jpg")
        click_choice(page, "report_evac")
        open_ending(page, out, "06-ending-switch-gallery.jpg")

        browser.close()
    log("全部完成。")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "screenshots")
    parser.add_argument("--channel", default="msedge", help="msedge / chrome")
    args = parser.parse_args()
    sys.exit(main(args.url, args.out, args.channel))
