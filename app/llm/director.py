"""叙事导演(Director):把 LLM 安全地接进剧情流程。

分工原则:**剧情骨架由数据决定,LLM 只负责表达层**。具体体现在三件事上:

* ``narrate``   旁白流式改写:带事实锚点与玩家记忆,生成后做质量校验,不合格则替换回预写文本;
* ``interpret`` 自由行动解析:把玩家的自由输入映射到骨架里已有的选项(JSON),模型不能创造新分支;
* ``epilogue``  后日谈:结局确定之后再自由发挥,回扣玩家做过的具体选择。

任何一步 LLM 不可用/超时/输出不合格,都会降级为预写内容,游戏不会卡死或断链。

事件协议(narrate / epilogue 产出,由 API 层包装成 SSE):
    {"type": "delta",   "text": "..."}                         增量文本(打字机效果)
    {"type": "replace", "paragraphs": [...], "reason": "..."}  用预写文本整体替换(降级)
    {"type": "done",    "source": "ai" | "authored", "model": "...", "reason": "..."}
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, AsyncIterator, Callable

from ..config import Settings
from ..story.engine import GameState, Story
from . import prompts
from .client import LLMClient, LLMError, LLMStreamError

log = logging.getLogger(__name__)

Event = dict[str, Any]

# 关键名词组(组内任意一个出现即视为保留了该事实);AI 改写必须保留原稿里出现过的大部分
KEY_GROUPS: tuple[tuple[str, ...], ...] = (
    ("老周",),
    ("陈岚", "陈姐"),
    ("小满",),
    ("磁带", "录音带"),
    ("铃铛",),
    ("水闸",),
    ("江滨坊",),
    ("江滨路",),
    ("互助热线", "热线"),
    ("白板",),
    ("冲锋舟",),
)
# 剧透词:原稿里没出现过的话,AI 也不许自己提前抖出来
SPOILER_TERMS = ("掐断", "掐的", "咔哒", "尚未经证实")
KEY_KEEP_RATIO = 0.6


def _compact(text: str) -> str:
    return "".join(text.split())


def validate_narration(text: str, authored: list[str]) -> str | None:
    """校验 AI 旁白。返回 None 表示合格,否则返回不合格原因。"""
    base = "".join(authored)
    n, base_n = len(_compact(text)), len(_compact(base))
    if n < base_n * 0.6:
        return "too_short"
    if n > base_n * 2.0:
        return "too_long"
    if re.search(r"(^|\n)\s*(#|[-*•]\s|\d+[.、)])", text) or "**" in text or "```" in text:
        return "markdown"
    for term in SPOILER_TERMS:
        if term in text and term not in base:
            return f"spoiler:{term}"
    groups = [g for g in KEY_GROUPS if any(w in base for w in g)]
    kept = [g for g in groups if any(w in text for w in g)]
    if groups and len(kept) / len(groups) < KEY_KEEP_RATIO:
        return "missing_facts"
    return None


def validate_epilogue(text: str) -> str | None:
    n = len(_compact(text))
    if n < 50:
        return "too_short"
    if n > 450:
        return "too_long"
    if "**" in text or "```" in text:
        return "markdown"
    return None


def extract_json(raw: str) -> dict | None:
    """从模型输出里尽量稳健地取出 JSON 对象(容忍 ```json 围栏与前后多余文字)。"""
    raw = raw.strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def sanitize_player_text(text: str) -> str:
    """玩家自由输入属于不可信数据:去控制字符/尖括号(防止闭合提示词里的标签),限制长度。"""
    text = re.sub(r"[\x00-\x1f\x7f<>]", " ", text or "")
    return " ".join(text.split())[: prompts.MAX_PLAYER_TEXT]


class Director:
    def __init__(self, story: Story, llm: LLMClient, settings: Settings,
                 clock: Callable[[], float] = time.monotonic):
        self._story = story
        self._llm = llm
        self._settings = settings
        self._clock = clock

    # ------------------------------------------------------------------ 旁白
    async def narrate(self, state: GameState) -> AsyncIterator[Event]:
        node = self._story.current(state)
        authored = node.render_text(state.variables)
        if not self._llm.enabled:
            for event in _authored_events(authored, "ai_disabled"):
                yield event
            return
        messages = prompts.narration_messages(self._story, node, state, authored)
        async for event in self._generate(
            messages, authored, lambda t: validate_narration(t, authored), max_tokens=900
        ):
            yield event

    # ------------------------------------------------------------------ 后日谈
    async def epilogue(self, state: GameState) -> AsyncIterator[Event]:
        node = self._story.current(state)
        if not node.is_ending:
            raise ValueError("epilogue is only available at an ending")
        authored = [node.epilogue] if node.epilogue else []
        if not self._llm.enabled:
            for event in _authored_events(authored, "ai_disabled"):
                yield event
            return
        messages = prompts.epilogue_messages(self._story, node, state)
        async for event in self._generate(messages, authored, validate_epilogue, max_tokens=500):
            yield event

    # ------------------------------------------------------------------ 自由行动
    async def interpret(self, state: GameState, text: str) -> dict[str, Any]:
        """把玩家自由输入映射成骨架里的选项。永远返回 dict,不抛异常。"""
        node = self._story.current(state)
        clean = sanitize_player_text(text)
        views = self._story.choice_views(state)
        available = [v for v in views if not v.locked]

        def result(choice_id: str | None, reply: str, source: str, reason: str = "") -> dict[str, Any]:
            return {"matched": choice_id is not None, "choice_id": choice_id,
                    "reply": reply, "source": source, "reason": reason}

        if node.is_ending or not available:
            return result(None, "故事已经走到尽头。", "offline", "no_choices")
        if not clean:
            return result(None, "你张了张嘴,什么也没说出口。", "offline", "empty")
        if not self._llm.enabled:
            return result(None, "(AI 导演未启用,请从下方选项中选择。)", "offline", "ai_disabled")

        messages = prompts.intent_messages(self._story, node, state, views, clean)
        try:
            raw = await self._llm.complete(messages, max_tokens=200, temperature=0.3, json_mode=True)
        except LLMError as exc:
            log.warning("intent routing failed: %s", exc)
            return result(None, "(AI 导演暂时离线,请从下方选项中选择。)", "offline", "llm_unavailable")

        data = extract_json(raw) or {}
        choice_id = data.get("choice_id")
        valid_ids = {v.id for v in available}
        if choice_id not in valid_ids:  # 含 null / 幻觉出来的 id / 被锁定的选项
            choice_id = None
        reply = str(data.get("reply") or "").strip()[:80]
        if not reply:
            reply = "你照着心里的想法做了。" if choice_id else "你想了想,眼下并不能这样做。"
        return result(choice_id, reply, "ai")

    # ------------------------------------------------------------------ 通用:流式生成 + 守卫
    async def _generate(
        self,
        messages: list[dict],
        authored: list[str],
        validator: Callable[[str], str | None],
        *,
        max_tokens: int,
    ) -> AsyncIterator[Event]:
        started = self._clock()
        buffer: list[str] = []
        model_used = ""
        try:
            async for model, chunk in self._llm.stream(messages, max_tokens=max_tokens):
                model_used = model
                buffer.append(chunk)
                yield {"type": "delta", "text": chunk}
                if self._clock() - started > self._settings.narration_deadline:
                    raise LLMStreamError("generation deadline exceeded")
        except LLMStreamError as exc:
            log.warning("stream interrupted, falling back to authored text: %s", exc)
            for event in _authored_events(authored, "interrupted"):
                yield event
            return
        except LLMError as exc:
            log.warning("llm unavailable, falling back to authored text: %s", exc)
            for event in _authored_events(authored, "llm_unavailable"):
                yield event
            return

        problem = validator("".join(buffer))
        if problem:
            log.warning("AI output rejected (%s), falling back to authored text", problem)
            for event in _authored_events(authored, problem):
                yield event
            return
        yield {"type": "done", "source": "ai", "model": model_used, "reason": ""}


def _authored_events(paragraphs: list[str], reason: str) -> list[Event]:
    return [
        {"type": "replace", "paragraphs": paragraphs, "reason": reason},
        {"type": "done", "source": "authored", "model": "", "reason": reason},
    ]
