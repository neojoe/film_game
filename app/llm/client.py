"""智谱 GLM 客户端:流式/非流式调用 + 模型降级链 + 冷却(熔断)。

为什么需要这些
--------------
实测免费模型 glm-4.7-flash 在高峰期会频繁返回 429("访问量过大")。如果每个场景都先等它失败再换模型,
玩家会感觉到明显卡顿。所以这里做了:

* 模型降级链:主模型失败(429/5xx/超时)立即切换备用模型;
* 冷却:失败的模型在一段时间内不再被尝试,后续请求直接走可用模型;
  全部模型冷却时立刻抛出 LLMUnavailable,让上层秒级降级为预写文本,而不是干等超时。
"""
from __future__ import annotations

import json
import logging
import time
from typing import AsyncIterator, Callable

import httpx

from ..config import Settings

log = logging.getLogger(__name__)

# 这些系列默认开启"思考"模式,会显著拉长首 token 时间,叙事场景不需要
_THINKING_PREFIXES = ("glm-4.5", "glm-4.6", "glm-4.7", "glm-5")

TRANSIENT_COOLDOWN = 20.0  # 429 / 5xx
NETWORK_COOLDOWN = 15.0  # 超时 / 连接错误
EMPTY_COOLDOWN = 10.0  # 返回空内容
AUTH_COOLDOWN = 300.0  # 401 / 403:Key 有问题,别反复打


class LLMError(Exception):
    """调用失败的基类。"""


class LLMUnavailable(LLMError):
    """所有候选模型都不可用(或没有配置 Key)。"""


class LLMStreamError(LLMError):
    """流式输出中途失败(此时已有部分文本发给了调用方)。"""


class LLMClient:
    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._settings = settings
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=5.0, read=settings.llm_read_timeout)
        )
        self._clock = clock
        self._cooldown_until: dict[str, float] = {}

    # ------------------------------------------------------------------ 状态
    @property
    def enabled(self) -> bool:
        return self._settings.ai_enabled

    async def aclose(self) -> None:
        await self._http.aclose()

    def _candidates(self) -> list[str]:
        now = self._clock()
        return [m for m in self._settings.llm_models if self._cooldown_until.get(m, 0.0) <= now]

    def _penalize(self, model: str, seconds: float, reason: str) -> None:
        self._cooldown_until[model] = self._clock() + seconds
        log.warning("llm model %s cooled down for %.0fs (%s)", model, seconds, reason)

    # ------------------------------------------------------------------ 请求构造
    def _request(self, model: str, messages: list[dict], *, stream: bool, max_tokens: int,
                 temperature: float, json_mode: bool) -> dict:
        body: dict = {
            "model": model,
            "messages": messages,
            "stream": stream,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if model.startswith(_THINKING_PREFIXES):
            body["thinking"] = {"type": "disabled"}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        return body

    @property
    def _url(self) -> str:
        return f"{self._settings.base_url}/chat/completions"

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._settings.api_key}", "Content-Type": "application/json"}

    def _handle_http_error(self, model: str, status: int, text: str) -> None:
        """返回 = 可以换下一个模型;抛出 LLMError = 不可恢复的请求错误。"""
        if status in (401, 403):
            for m in self._settings.llm_models:
                self._penalize(m, AUTH_COOLDOWN, f"auth {status}")
            raise LLMUnavailable(f"authentication failed ({status})")
        if status == 429 or status >= 500:
            self._penalize(model, TRANSIENT_COOLDOWN, f"HTTP {status}")
            return
        raise LLMError(f"HTTP {status}: {text[:200]}")

    # ------------------------------------------------------------------ 非流式
    async def complete(
        self,
        messages: list[dict],
        *,
        max_tokens: int = 600,
        temperature: float = 0.7,
        json_mode: bool = False,
    ) -> str:
        """一次性返回完整文本。依次尝试可用模型,全部失败抛 LLMUnavailable。"""
        if not self.enabled:
            raise LLMUnavailable("no api key configured")
        candidates = self._candidates()
        if not candidates:
            raise LLMUnavailable("all models cooling down")

        for model in candidates:
            body = self._request(model, messages, stream=False, max_tokens=max_tokens,
                                 temperature=temperature, json_mode=json_mode)
            try:
                resp = await self._http.post(self._url, headers=self._headers, json=body)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                self._penalize(model, NETWORK_COOLDOWN, type(exc).__name__)
                continue
            if resp.status_code != 200:
                self._handle_http_error(model, resp.status_code, resp.text)
                continue
            try:
                content = resp.json()["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError, ValueError):
                content = ""
            if content.strip():
                return content
            self._penalize(model, EMPTY_COOLDOWN, "empty content")
        raise LLMUnavailable("no model produced a response")

    # ------------------------------------------------------------------ 流式
    async def stream(
        self,
        messages: list[dict],
        *,
        max_tokens: int = 700,
        temperature: float = 0.8,
    ) -> AsyncIterator[tuple[str, str]]:
        """逐块产出 ``(model, text)``。

        * 首个文本块之前的失败 → 静默切换下一个模型;
        * 已经产出文本之后失败 → 抛 LLMStreamError(调用方应整体降级);
        * 没有任何模型可用 → 抛 LLMUnavailable。
        """
        if not self.enabled:
            raise LLMUnavailable("no api key configured")
        candidates = self._candidates()
        if not candidates:
            raise LLMUnavailable("all models cooling down")

        yielded = False
        for model in candidates:
            body = self._request(model, messages, stream=True, max_tokens=max_tokens,
                                 temperature=temperature, json_mode=False)
            try:
                async with self._http.stream("POST", self._url, headers=self._headers, json=body) as resp:
                    if resp.status_code != 200:
                        raw = (await resp.aread()).decode("utf-8", "replace")
                        self._handle_http_error(model, resp.status_code, raw)
                        continue
                    async for line in resp.aiter_lines():
                        text = _parse_sse_line(line)
                        if text is None:
                            if line.strip() == "data: [DONE]":
                                break
                            continue
                        yielded = True
                        yield model, text
                if yielded:
                    return
                self._penalize(model, EMPTY_COOLDOWN, "empty stream")
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                self._penalize(model, NETWORK_COOLDOWN, type(exc).__name__)
                if yielded:
                    raise LLMStreamError(f"stream interrupted: {type(exc).__name__}") from exc
        raise LLMUnavailable("no model produced a response")


def _parse_sse_line(line: str) -> str | None:
    """解析一行 SSE。有正文增量则返回文本,否则返回 None(心跳、reasoning、结束标记等)。"""
    line = line.strip()
    if not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        delta = json.loads(payload)["choices"][0]["delta"]
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    return delta.get("content") or None
