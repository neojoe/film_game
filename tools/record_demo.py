"""开发工具:录制演示视频(真实通关,不是动画合成)。

做法:无头浏览器连接真实游戏与真实 GLM,按"标题 → AI 流式旁白 → 自由行动 → 被锁住的选项 → 结局 →
换一条路 → 另一个结局"的顺序通关,并录屏。录制时在页面里临时注入两样东西(只存在于录制会话,不属于游戏):
  - 一个跟随鼠标的指针(无头浏览器录不到系统光标,否则看不出点了哪里);
  - 一条说明字幕(视频没有旁白,用字幕交代每一步在展示什么)。

需要:先 `python run.py` 启动游戏并配置 Key;系统装有 Edge/Chrome 和 ffmpeg;playwright 已装且执行过
`playwright install ffmpeg`(只有 ~1 MB 的录屏组件,不下载浏览器)。

用法(项目根目录):
    python -m tools.record_demo                 # 输出 docs/demo.mp4
    python -m tools.record_demo --url http://127.0.0.1:8000 --out docs/demo.mp4
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import Locator, Page, sync_playwright

from tools.take_screenshots import (
    FREE_ACT_TEXT,
    LABELS,
    ROOT,
    ROUTE_SWITCH,
    ROUTE_TRUTH,
    log,
    scene_title,
    skip_narration,
    wait_choices,
    wait_title_change,
)

# 16:9。够高才能让"水闸站"4 个选项 + 输入框 + 场景标题一屏放下(面板最大高度是视口的 76%)
VIEWPORT = {"width": 1600, "height": 900}

OVERLAY_JS = r"""
(() => {
  const add = () => {
    if (document.getElementById('demoCursor')) return;
    const cur = document.createElement('div');
    cur.id = 'demoCursor';
    cur.style.cssText = 'position:fixed;z-index:99999;width:24px;height:24px;border-radius:50%;'
      + 'border:2px solid #fff;background:rgba(255,255,255,.22);pointer-events:none;left:-60px;top:-60px;'
      + 'transform:translate(-50%,-50%);transition:width .12s,height .12s,background .12s;'
      + 'box-shadow:0 0 10px rgba(0,0,0,.7)';
    document.body.appendChild(cur);
    const cap = document.createElement('div');
    cap.id = 'demoCaption';
    cap.style.cssText = 'position:fixed;z-index:99998;left:50%;top:14px;transform:translateX(-50%);'
      + 'max-width:78%;padding:9px 22px;border-radius:999px;background:rgba(0,0,0,.74);color:#fff;'
      + 'font:600 20px/1.45 "Microsoft YaHei","PingFang SC",sans-serif;letter-spacing:1px;text-align:center;'
      + 'pointer-events:none;opacity:0;transition:opacity .35s;border:1px solid rgba(255,255,255,.2)';
    document.body.appendChild(cap);
    addEventListener('mousemove', (e) => { cur.style.left = e.clientX + 'px'; cur.style.top = e.clientY + 'px'; });
    addEventListener('mousedown', () => { cur.style.width = cur.style.height = '15px'; cur.style.background = 'rgba(255,214,102,.95)'; });
    addEventListener('mouseup', () => { cur.style.width = cur.style.height = '24px'; cur.style.background = 'rgba(255,255,255,.22)'; });
  };
  if (document.body) add(); else addEventListener('DOMContentLoaded', add);
})();
"""


def caption(page: Page, text: str) -> None:
    page.evaluate(
        "t => { const c = document.getElementById('demoCaption');"
        " if (!t) { c.style.opacity = 0; return; } c.textContent = t; c.style.opacity = 1; }",
        text,
    )


def human_click(page: Page, locator: Locator, steps: int = 24) -> None:
    """鼠标先"移"过去、停一下、再点击,让观众看得清楚点了什么。"""
    locator.scroll_into_view_if_needed()
    box = locator.bounding_box()
    x, y = box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.5
    page.mouse.move(x, y, steps=steps)
    page.wait_for_timeout(320)
    page.mouse.click(x, y)


def park_mouse(page: Page) -> None:
    """把指针挪到面板外的空白处,否则上一次点击的位置会让某个选项一直显示成悬停高亮,像被选中。"""
    page.mouse.move(VIEWPORT["width"] - 90, 420, steps=10)


def pick(page: Page, choice_id: str, steps: int = 24) -> None:
    before = scene_title(page)
    button = page.locator("#choices .choice", has_text=LABELS[choice_id][:14]).first
    human_click(page, button, steps)
    park_mouse(page)
    wait_title_change(page, before)


def fast_forward(page: Page, route: list[str]) -> None:
    """不需要细看的场景:跳过打字机,留 0.7 秒让观众瞥见选项,再点下一个。"""
    for choice_id in route:
        skip_narration(page)
        page.wait_for_timeout(700)
        pick(page, choice_id, steps=14)


def finish_ending(page: Page, hold_ms: int) -> None:
    """走到结局场景 → 点"落幕" → 等 AI 后日谈写完 → 停留让观众读。"""
    skip_narration(page)
    page.wait_for_timeout(600)
    human_click(page, page.locator("#choices .choice").first, steps=14)
    page.wait_for_selector("#ending:not(.hidden)", timeout=10_000)
    page.wait_for_function("document.getElementById('epilogueSource').textContent.length > 0", timeout=70_000)
    page.wait_for_timeout(hold_ms)


def record(page: Page, url: str) -> None:
    page.goto(url)
    page.wait_for_function("!document.getElementById('btnStart').disabled", timeout=15_000)

    # ① 标题页
    caption(page, "《听雨 FM · 最后一夜》  用 AIGC 做的互动影游")
    page.wait_for_timeout(3800)
    caption(page, "点「开始游戏」")
    human_click(page, page.locator("#btnStart"))

    # ② 序幕:AI 流式旁白(让观众看一会儿打字机),再演示"跳过"
    caption(page, "AI 实时生成旁白,逐字流式显示")
    page.wait_for_selector("#narration p", timeout=30_000)
    page.wait_for_timeout(1800)  # 序幕很短,要趁打字机还没打完就点"跳过",演示才有意义
    caption(page, "不想等?点「跳过」直接看全文")
    human_click(page, page.locator("#btnSkip"))
    wait_choices(page, 15_000)
    page.wait_for_timeout(1500)
    caption(page, "每个选择都会改变后面的剧情")
    pick(page, "start")

    # ③ 工作室:AI 结合玩家状态改写旁白 + 自由行动
    caption(page, "AI 在预写稿的基础上改写旁白,故事骨架不变")
    wait_choices(page)
    page.wait_for_timeout(2600)
    caption(page, "也可以直接输入想做的事,AI 导演理解你的意图")
    human_click(page, page.locator("#actInput"))
    page.keyboard.type(FREE_ACT_TEXT, delay=75)
    page.wait_for_timeout(500)
    before = scene_title(page)
    human_click(page, page.locator("#act button"))
    page.wait_for_function(
        "() => { const r = document.getElementById('reaction');"
        " return !r.classList.contains('hidden') && r.textContent && !r.textContent.includes('正在理解'); }",
        timeout=30_000,
    )
    caption(page, "AI 把这句话对应到了「切一首歌」")
    try:
        wait_title_change(page, before, timeout=9_000)
    except Exception:  # 极少数情况下没匹配上:退回到手动点选项,保证视频能继续
        pick(page, "cut_song")

    # ④ 路线 B 快进到水闸站
    caption(page, "故事继续……(快进)")
    fast_forward(page, ROUTE_SWITCH[2:-1])

    # ⑤ 水闸站:被锁住的选项
    wait_choices(page)
    caption(page, "条件分支:没拿到十年前的磁带,这个选项被锁住了")
    page.wait_for_timeout(1800)
    human_click(page, page.locator("#choices .choice.locked").first, steps=30)
    page.wait_for_timeout(3600)
    caption(page, "选「江滨坊的人已经在撤了」")
    pick(page, ROUTE_SWITCH[-1])

    # ⑥ 结局一(字幕在进入结局页之前就换好,避免上一句字幕停在结局页上)
    caption(page, "结局页:AI 生成个性化后日谈,并回顾你做过的选择")
    finish_ending(page, hold_ms=8500)

    # ⑦ 换一条路,另一个结局
    caption(page, "同一个故事,换一条路,走向不同的结局")
    page.wait_for_timeout(1800)
    before = scene_title(page)
    human_click(page, page.locator("#btnAgain"))
    wait_title_change(page, before)
    fast_forward(page, ROUTE_TRUTH)
    finish_ending(page, hold_ms=2000)
    caption(page, "好结局「真话」· 结局收集 2 / 4")
    page.wait_for_timeout(6500)
    caption(page, "代码与说明:github.com/neojoe/film_game")
    page.wait_for_timeout(3500)
    caption(page, "")
    page.wait_for_timeout(600)


def convert(src: Path, dst: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        fallback = dst.with_suffix(".webm")
        shutil.copyfile(src, fallback)
        log(f"没找到 ffmpeg,直接保留 webm:{fallback.relative_to(ROOT)}")
        return
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-ss", "0.8", "-i", str(src), "-an",
           "-c:v", "libx264", "-preset", "slow", "-crf", "28", "-pix_fmt", "yuv420p",
           "-movflags", "+faststart", str(dst)]
    subprocess.run(cmd, check=True)


def main(url: str, out: Path, channel: str) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as p:
        browser = p.chromium.launch(channel=channel, headless=True,
                                    args=["--autoplay-policy=no-user-gesture-required"])
        context = browser.new_context(viewport=VIEWPORT, locale="zh-CN",
                                      record_video_dir=tmp, record_video_size=VIEWPORT)
        context.add_init_script(OVERLAY_JS)
        page = context.new_page()
        page.on("pageerror", lambda e: log(f"[页面错误] {e}"))
        log("开始录制……")
        try:
            record(page, url)
        finally:
            video = page.video
            context.close()
            browser.close()
        raw = Path(video.path())
        log(f"录制结束,原始文件 {raw.stat().st_size // 1024} KB,转码中……")
        convert(raw, out)
    if out.exists():
        log(f"已保存 {out.relative_to(ROOT)}  ({out.stat().st_size / 1024 / 1024:.1f} MB)")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "demo.mp4")
    parser.add_argument("--channel", default="msedge", help="msedge / chrome")
    args = parser.parse_args()
    sys.exit(main(args.url, args.out, args.channel))
