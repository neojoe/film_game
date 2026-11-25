"""开发工具:对所有场景各生成一次 AI 旁白,统计守卫通过率 / 降级原因 / 延迟。

用法(项目根目录,需要 ZHIPU_API_KEY):
    python -m tools.eval_narration            # 每个场景 1 次
    python -m tools.eval_narration --runs 3   # 每个场景 3 次(更稳的统计,但更慢、更耗额度)

用途:调提示词或守卫规则后,用它快速量化"AI 旁白被采纳的比例",而不是凭感觉。
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from collections import Counter, deque

from app.config import load_settings
from app.llm.client import LLMClient
from app.llm.director import Director
from app.story import GameState, Story


def first_state_per_node(story: Story) -> dict[str, GameState]:
    """BFS:遍历所有 (场景, 变量) 状态,每个场景只保留"最早到达它时"的状态(即最短路径)。

    必须按完整状态去重而不是只按场景去重,否则同一场景下需要不同状态才能到达的结局会被漏掉。
    """
    start = story.new_game()
    found = {start.node_id: start}
    seen = {(start.node_id, tuple(sorted(start.variables.items())))}
    queue = deque([start])
    while queue:
        state = queue.popleft()
        for view in story.choice_views(state):
            if view.locked:
                continue
            nxt = state.clone()
            story.choose(nxt, view.id)
            key = (nxt.node_id, tuple(sorted(nxt.variables.items())))
            if key in seen:
                continue
            seen.add(key)
            found.setdefault(nxt.node_id, nxt)
            queue.append(nxt)
    return found


async def run_once(director: Director, state: GameState, epilogue: bool) -> tuple[str, str, float | None, float]:
    started = time.monotonic()
    first = None
    source = reason = ""
    stream = director.epilogue(state) if epilogue else director.narrate(state)
    async for event in stream:
        if event["type"] == "delta" and first is None:
            first = time.monotonic() - started
        elif event["type"] == "done":
            source, reason = event["source"], event["reason"]
    return source, reason, first, time.monotonic() - started


async def main(runs: int) -> int:
    settings = load_settings()
    if not settings.ai_enabled:
        print("未配置 ZHIPU_API_KEY,无法评估。", file=sys.stderr)
        return 1
    story = Story.load()
    llm = LLMClient(settings)
    director = Director(story, llm, settings)

    reasons: Counter[str] = Counter()
    firsts: list[float] = []
    totals: list[float] = []
    ai_ok = attempts = 0
    print(f"{'场景':<12}{'结果':<10}{'原因':<18}{'首字(s)':<9}{'总计(s)'}")
    for node_id, state in first_state_per_node(story).items():
        for _ in range(runs):
            source, reason, first, total = await run_once(director, state, epilogue=False)
            attempts += 1
            ai_ok += source == "ai"
            reasons[reason or "ok"] += 1
            totals.append(total)
            if first:
                firsts.append(first)
            print(f"{node_id:<12}{source:<10}{reason or '-':<18}{(f'{first:.1f}' if first else '-'):<9}{total:.1f}")
    await llm.aclose()

    print("\n== 汇总 ==")
    print(f"AI 旁白被采纳: {ai_ok}/{attempts} = {ai_ok / attempts:.0%}")
    print(f"降级原因分布: {dict(reasons)}")
    if firsts:
        print(f"首字延迟 中位数 {statistics.median(firsts):.1f}s  最大 {max(firsts):.1f}s")
    print(f"整段生成   中位数 {statistics.median(totals):.1f}s  最大 {max(totals):.1f}s")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=1)
    raise SystemExit(asyncio.run(main(parser.parse_args().runs)))
