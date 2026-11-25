"""测试替身:不访问网络的假 LLM,可按需产出文本块 / 抛异常,并记录收到的 messages。"""


class StubLLM:
    def __init__(self, *, enabled=True, chunks=(), before=None, after=None, complete_text="", complete_exc=None):
        self.enabled = enabled
        self.chunks, self.before, self.after = chunks, before, after
        self.complete_text, self.complete_exc = complete_text, complete_exc
        self.messages: list[list[dict]] = []

    async def stream(self, messages, **_):
        self.messages.append(messages)
        if self.before:
            raise self.before
        for chunk in self.chunks:
            yield "stub-model", chunk
        if self.after:
            raise self.after

    async def complete(self, messages, **_):
        self.messages.append(messages)
        if self.complete_exc:
            raise self.complete_exc
        return self.complete_text

    async def aclose(self):
        return None
