from __future__ import annotations

import asyncio
from types import SimpleNamespace

from llm.provider import LLMResult, Usage


class FakeStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.closed = False

    def __aiter__(self):
        self._iter = iter(self._chunks)
        return self

    async def __anext__(self):
        try:
            item = next(self._iter)
        except StopIteration as exc:
            raise StopAsyncIteration from exc
        if isinstance(item, Exception):
            raise item
        return item

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True


class FakeProvider:
    def __init__(self, results=None, hang=None, deltas=None):
        self.model = "fake"
        self.results = list(results or [])
        self.hang = hang
        self.calls = 0
        self.deltas = deltas or []

    async def complete(self, messages, tools=None, *, on_delta=None):
        self.calls += 1
        if self.hang is not None:
            await self.hang.wait()
        if self.deltas and on_delta:
            for channel, text in self.deltas:
                on_delta(channel, text)
        if self.results:
            return self.results.pop(0)
        return LLMResult(text="done", usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15, requests=1))


class FakeApprover:
    def __init__(self, answer="no"):
        self.answer = answer
        self.asked = []

    async def __call__(self, question, kind="text", **kwargs):
        self.asked.append((question, kind))
        return self.answer


def chunk(*, content=None, reasoning=None, tool_calls=None, usage=None, error=None, finish=None, model="fake"):
    delta = SimpleNamespace(
        content=content,
        reasoning=reasoning,
        tool_calls=tool_calls or [],
    )
    choice = SimpleNamespace(delta=delta, finish_reason=finish)
    return SimpleNamespace(
        choices=[choice],
        usage=usage,
        error=error,
        model=model,
    )


def tool_delta(index, *, call_id="", name="", arguments=""):
    return SimpleNamespace(
        index=index,
        id=call_id or None,
        function=SimpleNamespace(name=name or None, arguments=arguments),
    )
