"""LLM 客户端测试:降级链、冷却熔断、流式中断。全部用 httpx.MockTransport,不访问网络。"""
import asyncio
import json

import httpx
import pytest

from app.config import Settings
from app.llm.client import LLMClient, LLMStreamError, LLMUnavailable


def run(coro):
    return asyncio.run(coro)


def make_settings(**kw) -> Settings:
    return Settings(api_key=kw.pop("api_key", "test-key"), llm_models=("m1", "m2"), **kw)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def sse(*texts: str) -> bytes:
    lines = [f"data: {json.dumps({'choices': [{'delta': {'content': t}}]})}\n\n" for t in texts]
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def chat_ok(text: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


def make_client(handler, clock=None, **settings_kw):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return LLMClient(make_settings(**settings_kw), http=http, clock=clock or FakeClock())


def test_complete_falls_back_to_next_model_on_429():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        calls.append(model)
        return httpx.Response(429, json={"error": {"code": "1305"}}) if model == "m1" else chat_ok("你好")

    client = make_client(handler)
    assert run(client.complete([{"role": "user", "content": "hi"}])) == "你好"
    assert calls == ["m1", "m2"]


def test_failed_model_is_cooled_down_then_recovers():
    calls = []
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        calls.append(model)
        return httpx.Response(429) if model == "m1" else chat_ok("ok")

    client = make_client(handler, clock=clock)
    run(client.complete([]))
    run(client.complete([]))
    assert calls == ["m1", "m2", "m2"]  # 第二次直接跳过冷却中的 m1

    clock.now += 60  # 冷却结束后重新尝试 m1
    run(client.complete([]))
    assert calls[-2:] == ["m1", "m2"]


def test_all_models_failing_fails_fast_afterwards():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429)

    client = make_client(handler)
    with pytest.raises(LLMUnavailable):
        run(client.complete([]))
    n = len(calls)
    with pytest.raises(LLMUnavailable):  # 全部冷却:不再发请求,秒级失败
        run(client.complete([]))
    assert len(calls) == n


def test_no_api_key_is_unavailable_without_network():
    def handler(request):  # pragma: no cover - 不应被调用
        raise AssertionError("network must not be touched")

    client = make_client(handler, api_key="")
    assert client.enabled is False
    with pytest.raises(LLMUnavailable):
        run(client.complete([]))


def test_auth_error_cools_every_model():
    def handler(request):
        return httpx.Response(401, json={"error": "bad key"})

    client = make_client(handler)
    with pytest.raises(LLMUnavailable):
        run(client.complete([]))
    assert client._candidates() == []


def test_thinking_disabled_only_for_reasoning_models():
    bodies = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies[body["model"]] = body
        return httpx.Response(429) if body["model"] == "glm-4.7-flash" else chat_ok("ok")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = Settings(api_key="k", llm_models=("glm-4.7-flash", "glm-4-flash-250414"))
    run(LLMClient(settings, http=http).complete([], json_mode=True))
    assert bodies["glm-4.7-flash"]["thinking"] == {"type": "disabled"}
    assert "thinking" not in bodies["glm-4-flash-250414"]
    assert bodies["glm-4-flash-250414"]["response_format"] == {"type": "json_object"}


async def _collect(agen):
    return [item async for item in agen]


def test_stream_switches_model_before_first_token():
    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        if model == "m1":
            return httpx.Response(429)
        return httpx.Response(200, content=sse("雨", "夜"), headers={"content-type": "text/event-stream"})

    client = make_client(handler)
    chunks = run(_collect(client.stream([])))
    assert chunks == [("m2", "雨"), ("m2", "夜")]


def test_stream_ignores_reasoning_and_heartbeat_lines():
    payload = (
        b": keep-alive\n\n"
        b'data: {"choices":[{"delta":{"reasoning_content":"thinking..."}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"\xe5\xa5\xbd"}}]}\n\n'
        b"data: [DONE]\n\n"
    )
    client = make_client(lambda r: httpx.Response(200, content=payload))
    assert run(_collect(client.stream([]))) == [("m1", "好")]


class _BrokenStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield sse("半")[: -len(b"data: [DONE]\n\n")]
        raise httpx.ReadTimeout("boom")


def test_stream_failure_midway_raises_stream_error():
    client = make_client(lambda r: httpx.Response(200, stream=_BrokenStream()))
    received = []

    async def consume():
        async for _model, chunk in client.stream([]):
            received.append(chunk)

    with pytest.raises(LLMStreamError):
        run(consume())
    assert received == ["半"]
