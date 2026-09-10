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

    async def complete(self, messages, tools=None, *, on_delta=None, **kwargs):
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


class FakeMcpSession:
    def __init__(
        self,
        *,
        tools=None,
        resources=None,
        fail_init=None,
        stdout="",
        stderr="",
        dead=False,
        call_error=None,
        call_result=None,
        elicit="",
    ):
        self.tools = list(tools or [{"name": "search", "description": "search"}])
        self.resources = list(resources or [])
        self.fail_init = fail_init
        self.stdout = stdout
        self.stderr = stderr
        self.dead = dead
        self.call_error = call_error
        self.call_result = call_result
        self.elicit = elicit
        self.closed = False
        self.calls = []

    async def initialize(self):
        if self.fail_init is not None:
            if isinstance(self.fail_init, BaseException):
                raise self.fail_init
            raise RuntimeError(str(self.fail_init))

    async def list_tools(self):
        return SimpleNamespace(tools=self.tools)

    async def list_resources(self):
        return SimpleNamespace(resources=self.resources)

    async def read_resource(self, uri):
        if self.dead:
            raise ConnectionError("disconnected")
        return SimpleNamespace(content=[{"type": "text", "text": f"resource {uri}"}])

    async def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments or {}))
        if self.elicit:
            callback = getattr(self, "_engine_elicit", None) or getattr(
                self, "elicitation_callback", None
            )
            if callback is not None:
                await callback(None, SimpleNamespace(message=self.elicit))
        if self.dead:
            raise ConnectionError("disconnected")
        if self.call_error is not None:
            if isinstance(self.call_error, BaseException):
                raise self.call_error
            raise RuntimeError(str(self.call_error))
        if self.call_result is not None:
            return self.call_result
        return SimpleNamespace(content=[{"type": "text", "text": f"ok {name}"}])

    async def aclose(self):
        self.closed = True


def fake_mcp_connect(factory=None, *, per_name=None):
    calls: list[str] = []

    async def connect(cfg):
        calls.append(cfg.name)
        if per_name and cfg.name in per_name:
            item = per_name[cfg.name]
            if isinstance(item, list):
                return item.pop(0)
            if callable(item):
                return item()
            return item
        if factory is not None:
            return factory()
        return FakeMcpSession()

    connect.calls = calls
    return connect
