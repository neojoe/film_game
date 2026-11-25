"""剧情图校验:保证"剧情收得住"——没有死路、没有悬空引用、每个结局都可达。

因为状态变量取值很少,这里直接对 (场景, 变量取值) 做穷举(BFS),
而不是只看静态图,这样能发现"条件分支永远走不到"或"某状态下所有选择都被锁死"这类软锁问题。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .engine import _OPS, Story

MAX_STATES = 50_000


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    reachable_nodes: set[str] = field(default_factory=set)
    reachable_endings: set[str] = field(default_factory=set)
    states_explored: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors


def _freeze(variables: dict[str, Any]) -> tuple:
    return tuple(sorted(variables.items()))


def _check_cond_shape(cond: Any, known_vars: set[str], where: str, errors: list[str]) -> None:
    if not cond:
        return
    if not isinstance(cond, dict):
        errors.append(f"{where}: condition must be an object")
        return
    for key, expected in cond.items():
        if key not in known_vars:
            errors.append(f"{where}: unknown variable {key!r}")
        if isinstance(expected, dict):
            for op in expected:
                if op not in _OPS:
                    errors.append(f"{where}: unknown operator {op!r}")


def validate_story(story: Story) -> ValidationReport:
    report = ValidationReport()
    errors = report.errors
    known_vars = set(story.initial_state)

    if story.start not in story.nodes:
        errors.append(f"start node {story.start!r} does not exist")
        return report

    # ---- 静态检查
    for node in story.nodes.values():
        if not node.text:
            errors.append(f"{node.id}: empty text")
        if node.is_ending and node.choices:
            errors.append(f"{node.id}: ending node must not have choices")
        if not node.is_ending and not node.choices:
            errors.append(f"{node.id}: non-ending node has no choices (dead end)")

        ids = [c.id for c in node.choices]
        if len(ids) != len(set(ids)):
            errors.append(f"{node.id}: duplicate choice ids")

        for variant in node.variants:
            _check_cond_shape(variant.get("if"), known_vars, f"{node.id}.variant", errors)
        for rule in node.on_enter:
            _check_cond_shape(rule.get("if"), known_vars, f"{node.id}.on_enter", errors)

        for choice in node.choices:
            where = f"{node.id}.{choice.id}"
            if choice.next is None and not choice.branches:
                errors.append(f"{where}: no next/branches")
            if choice.requires and not choice.lock_hint:
                errors.append(f"{where}: locked choice needs a lock_hint")
            _check_cond_shape(choice.requires, known_vars, f"{where}.requires", errors)
            for branch in choice.branches:
                _check_cond_shape(branch.when, known_vars, f"{where}.branch", errors)
            for target in choice.all_targets():
                if target not in story.nodes:
                    errors.append(f"{where}: target {target!r} does not exist")
            # 最后一个 branch 之后必须有兜底,否则可能无去向
            if choice.branches and choice.next is None and choice.branches[-1].when:
                errors.append(f"{where}: last branch needs a default (no 'if') or a 'next'")
            for key in (*choice.effects.get("set", {}), *choice.effects.get("add", {})):
                if key not in known_vars:
                    errors.append(f"{where}: effect on unknown variable {key!r}")

    if errors:
        return report

    # ---- 动态检查:穷举 (场景, 状态)
    start = story.new_game()
    seen = {(start.node_id, _freeze(start.variables))}
    queue = deque([start])
    while queue:
        state = queue.popleft()
        report.states_explored += 1
        if report.states_explored > MAX_STATES:
            errors.append("state space too large (possible unbounded variable)")
            return report

        node = story.current(state)
        report.reachable_nodes.add(node.id)
        if node.ending:
            report.reachable_endings.add(node.ending.id)
            continue

        available = [c for c in node.choices if c.is_available(state.variables)]
        if not available:
            errors.append(
                f"soft-lock: no available choice at {node.id} with state {state.variables}"
            )
            continue
        for choice in available:
            nxt = state.clone()
            story.choose(nxt, choice.id)  # 走真实的引擎路径,确保不抛异常
            key = (nxt.node_id, _freeze(nxt.variables))
            if key not in seen:
                seen.add(key)
                queue.append(nxt)

    for node_id in story.nodes:
        if node_id not in report.reachable_nodes:
            errors.append(f"unreachable node: {node_id}")
    for ending in story.endings:
        if ending.id not in report.reachable_endings:
            errors.append(f"unreachable ending: {ending.id}")
    return report


if __name__ == "__main__":  # pragma: no cover  命令行: python -m app.story.validate
    story = Story.load()
    rep = validate_story(story)
    print(f"states explored: {rep.states_explored}")
    print(f"nodes reachable: {len(rep.reachable_nodes)}/{len(story.nodes)}")
    print(f"endings reachable: {sorted(rep.reachable_endings)}")
    if rep.errors:
        print("ERRORS:")
        for e in rep.errors:
            print(" -", e)
        raise SystemExit(1)
    print("story graph OK")
