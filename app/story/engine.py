"""剧情引擎:把 story.json 加载成对象,并用"状态变量 + 条件分支"驱动剧情。

设计要点
--------
* 剧情骨架(场景、分支、结局)完全由数据定义,是确定性的 —— 这是"剧情收得住"的保证,
  LLM 只负责在骨架之内做表达层的生成,不能改变事实与走向。
* 选择可带 ``requires``(未满足则显示为锁定)与 ``branches``(按状态路由到不同场景/结局)。
* 状态变量很少(整数/布尔),所以 validate 模块可以对整个状态空间做穷举验证。
"""
from __future__ import annotations

import copy
import json
import operator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

Cond = dict[str, Any]

_OPS = {
    ">=": operator.ge,
    "<=": operator.le,
    ">": operator.gt,
    "<": operator.lt,
    "==": operator.eq,
    "!=": operator.ne,
}

DEFAULT_STORY_PATH = Path(__file__).with_name("story.json")


class InvalidChoice(ValueError):
    """选择不存在、已锁定,或当前场景已是结局。"""


# ---------------------------------------------------------------- 条件与效果


def check(cond: Cond | None, variables: dict[str, Any]) -> bool:
    """判断条件是否成立。条件是 ``{变量: 期望值}`` 或 ``{变量: {运算符: 值}}``,所有项需同时成立。"""
    if not cond:
        return True
    for key, expected in cond.items():
        actual = variables.get(key)
        if isinstance(expected, dict):
            for op, value in expected.items():
                if op not in _OPS or not _OPS[op](actual, value):
                    return False
        elif actual != expected:
            return False
    return True


def apply_effects(variables: dict[str, Any], effects: dict[str, Any] | None) -> None:
    """应用 ``set`` / ``add`` 效果(原地修改 variables)。"""
    if not effects:
        return
    for key, value in effects.get("set", {}).items():
        variables[key] = value
    for key, delta in effects.get("add", {}).items():
        variables[key] = variables.get(key, 0) + delta


# ---------------------------------------------------------------- 数据模型


@dataclass(frozen=True)
class Branch:
    to: str
    when: Cond = field(default_factory=dict)


@dataclass(frozen=True)
class Choice:
    id: str
    label: str
    next: str | None = None
    branches: tuple[Branch, ...] = ()
    effects: dict[str, Any] = field(default_factory=dict)
    requires: Cond = field(default_factory=dict)
    lock_hint: str = ""
    memo: str = ""

    def is_available(self, variables: dict[str, Any]) -> bool:
        return check(self.requires, variables)

    def target(self, variables: dict[str, Any]) -> str:
        """按(已应用 effects 之后的)状态决定去向:先看条件分支,再看默认 next。"""
        for branch in self.branches:
            if check(branch.when, variables):
                return branch.to
        if self.next is None:
            raise InvalidChoice(f"choice {self.id!r} has no reachable target")
        return self.next

    def all_targets(self) -> set[str]:
        targets = {b.to for b in self.branches}
        if self.next:
            targets.add(self.next)
        return targets


@dataclass(frozen=True)
class Ending:
    id: str
    name: str
    kind: str  # good / ok / bad
    subtitle: str = ""


@dataclass(frozen=True)
class Node:
    id: str
    title: str
    time: str
    place: str
    image: str
    mood: str
    text: tuple[str, ...]
    variants: tuple[dict[str, Any], ...] = ()
    tail: tuple[str, ...] = ()
    beats: tuple[str, ...] = ()
    choices: tuple[Choice, ...] = ()
    on_enter: tuple[dict[str, Any], ...] = ()
    ending: Ending | None = None
    epilogue: str = ""

    @property
    def is_ending(self) -> bool:
        return self.ending is not None

    def render_text(self, variables: dict[str, Any]) -> list[str]:
        """正文 = 基础段落 + 命中条件的变体段落 + 收尾段落。这是"权威版本",也是 AI 改写的蓝本与降级文本。"""
        paragraphs = list(self.text)
        for variant in self.variants:
            if check(variant.get("if"), variables):
                paragraphs.extend(variant["text"])
        paragraphs.extend(self.tail)
        return paragraphs

    def get_choice(self, choice_id: str) -> Choice | None:
        return next((c for c in self.choices if c.id == choice_id), None)


@dataclass
class GameState:
    node_id: str
    variables: dict[str, Any]
    history: list[dict[str, str]] = field(default_factory=list)
    step: int = 0

    def clone(self) -> GameState:
        return copy.deepcopy(self)


@dataclass(frozen=True)
class ChoiceView:
    id: str
    label: str
    locked: bool
    lock_hint: str


# ---------------------------------------------------------------- Story


class Story:
    def __init__(self, meta: dict[str, Any], nodes: dict[str, Node]):
        self.meta = meta
        self.nodes = nodes
        self.start: str = meta["start"]
        self.initial_state: dict[str, Any] = meta["initial_state"]

    # ---- 加载
    @classmethod
    def load(cls, path: str | Path = DEFAULT_STORY_PATH) -> Story:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        nodes = {nid: _parse_node(nid, data) for nid, data in raw["nodes"].items()}
        return cls(raw["meta"], nodes)

    # ---- 查询
    @property
    def title(self) -> str:
        return self.meta["title"]

    @property
    def endings(self) -> list[Ending]:
        return [n.ending for n in self.nodes.values() if n.ending]

    def node(self, node_id: str) -> Node:
        return self.nodes[node_id]

    # ---- 状态机
    def new_game(self) -> GameState:
        state = GameState(self.start, copy.deepcopy(self.initial_state))
        self._enter(state, self.start)
        return state

    def current(self, state: GameState) -> Node:
        return self.nodes[state.node_id]

    def choice_views(self, state: GameState) -> list[ChoiceView]:
        node = self.current(state)
        return [
            ChoiceView(c.id, c.label, not c.is_available(state.variables), c.lock_hint)
            for c in node.choices
        ]

    def choose(self, state: GameState, choice_id: str) -> GameState:
        """执行一次选择并原地推进状态。非法选择抛出 InvalidChoice 且不改变状态。"""
        node = self.current(state)
        if node.is_ending:
            raise InvalidChoice("story already ended")
        choice = node.get_choice(choice_id)
        if choice is None:
            raise InvalidChoice(f"unknown choice {choice_id!r} at node {node.id!r}")
        if not choice.is_available(state.variables):
            raise InvalidChoice(f"choice {choice_id!r} is locked")

        apply_effects(state.variables, choice.effects)
        target = choice.target(state.variables)
        state.history.append({"node": node.id, "choice": choice.id, "memo": choice.memo})
        state.step += 1
        self._enter(state, target)
        return state

    def _enter(self, state: GameState, node_id: str) -> None:
        state.node_id = node_id
        for rule in self.nodes[node_id].on_enter:
            if check(rule.get("if"), state.variables):
                apply_effects(state.variables, rule)


def _parse_node(node_id: str, data: dict[str, Any]) -> Node:
    ending = Ending(**data["ending"]) if data.get("ending") else None
    return Node(
        id=node_id,
        title=data["title"],
        time=data["time"],
        place=data["place"],
        image=data["image"],
        mood=data.get("mood", "rain_indoor"),
        text=tuple(data["text"]),
        variants=tuple(data.get("variants", ())),
        tail=tuple(data.get("tail", ())),
        beats=tuple(data.get("beats", ())),
        choices=tuple(_parse_choice(c) for c in data.get("choices", ())),
        on_enter=tuple(data.get("on_enter", ())),
        ending=ending,
        epilogue=data.get("epilogue", ""),
    )


def _parse_choice(data: dict[str, Any]) -> Choice:
    return Choice(
        id=data["id"],
        label=data["label"],
        next=data.get("next"),
        branches=tuple(Branch(to=b["to"], when=b.get("if", {})) for b in data.get("branches", ())),
        effects=data.get("effects", {}),
        requires=data.get("requires", {}),
        lock_hint=data.get("lock_hint", ""),
        memo=data.get("memo", ""),
    )
