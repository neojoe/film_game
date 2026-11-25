"""开发工具:用真实模型预览某个场景的 AI 旁白,并展示守卫(校验)结果。

用法(项目根目录):
    python -m tools.preview_narration                 # 预览 handoff 场景(档案室路线)
    python -m tools.preview_narration station         # 预览指定场景(自动选择一条能到达该场景的路线)
    python -m tools.preview_narration end_truth --epilogue
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time

from app.config import load_settings
from app.llm.client import LLMClient
from app.llm.director import Director, validate_narration
from app.story import Story

# 到达各场景的一条代表性路线(含所有前置选择)
ROUTES: dict[str, list[str]] = {
    "studio": ["start"],
    "callback": ["start", "cut_song"],
    "live": ["start", "go_live"],
    "archive": ["start", "cut_song", "to_archive"],
    "soothe": ["start", "go_live", "to_soothe"],
    "handoff": ["start", "cut_song", "to_archive", "archive_back"],
    "riverside": ["start", "cut_song", "to_archive", "archive_back", "riverside"],
    "station": ["start", "cut_song", "to_archive", "archive_back", "riverside", "riverside_go"],
    "end_truth": ["start", "cut_song", "to_archive", "archive_back", "riverside", "riverside_go", "play_tape"],
    "end_gate": ["start", "cut_song", "to_bureau", "bureau_back", "mountain", "mountain_go", "grab_key"],
}


async def main(node_id: str, epilogue: bool) -> int:
    settings = load_settings()
    if not settings.ai_enabled:
        print("未配置 ZHIPU_API_KEY,无法预览。", file=sys.stderr)
        return 1
    story = Story.load()
    state = story.new_game()
    for choice in ROUTES[node_id]:
        story.choose(state, choice)

    llm = LLMClient(settings)
    director = Director(story, llm, settings)
    node = story.current(state)
    authored = node.render_text(state.variables)
    print(f"== 场景: {node.title} ({node.id}) 路线变量: {state.variables}\n")

    started = time.monotonic()
    first = None
    text = []
    source = reason = ""
    stream = director.epilogue(state) if epilogue else director.narrate(state)
    async for event in stream:
        if event["type"] == "delta":
            first = first or time.monotonic() - started
            text.append(event["text"])
        elif event["type"] == "replace":
            print(f"[降级为预写文本] reason={event['reason']}")
            text = ["\n\n".join(event["paragraphs"])]
        else:
            source, reason = event["source"], event["reason"]
    await llm.aclose()

    output = "".join(text)
    print(output)
    print(f"\n-- source={source} reason={reason or '-'} 首字 {first and round(first, 1)}s 总计 {time.monotonic() - started:.1f}s")
    if not epilogue and source == "ai":
        print(f"-- 守卫校验: {validate_narration(output, authored) or 'OK'}  字数 {len(output)} / 原稿 {len(''.join(authored))}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("node", nargs="?", default="handoff", choices=sorted(ROUTES))
    parser.add_argument("--epilogue", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.node, args.epilogue)))
