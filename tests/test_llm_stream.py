from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from llm.openrouter import _result_from_stream
from llm.provider import LLMError, Usage
from tests.fakes import FakeStream, chunk, tool_delta


def test_text_deltas_concatenate():
    async def run():
        stream = FakeStream(
            [
                chunk(content="hel"),
                chunk(
                    content="lo",
                    finish="stop",
                    usage=SimpleNamespace(
                        prompt_tokens=1,
                        completion_tokens=2,
                        total_tokens=3,
                        cost=0.1,
                        completion_tokens_details=None,
                        prompt_tokens_details=None,
                    ),
                ),
            ]
        )
        seen = []
        result = await _result_from_stream(stream, lambda c, t: seen.append((c, t)), 1)
        assert result.text == "hello"
        assert seen == [("text", "hel"), ("text", "lo")]
        assert stream.closed

    asyncio.run(run())


def test_reasoning_not_in_text():
    async def run():
        stream = FakeStream([chunk(reasoning="think"), chunk(content="out")])
        seen = []
        result = await _result_from_stream(stream, lambda c, t: seen.append((c, t)), 1)
        assert result.text == "out"
        assert ("reasoning", "think") in seen

    asyncio.run(run())


def test_tool_call_fragments():
    async def run():
        stream = FakeStream(
            [
                chunk(tool_calls=[tool_delta(0, call_id="c1", name="search")]),
                chunk(tool_calls=[tool_delta(0, arguments='{"p"')]),
                chunk(tool_calls=[tool_delta(0, arguments=':"x"}')]),
            ]
        )
        result = await _result_from_stream(stream, None, 1)
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].id == "c1"
        assert result.tool_calls[0].arguments() == {"p": "x"}

    asyncio.run(run())


def test_interleaved_tool_calls():
    async def run():
        stream = FakeStream(
            [
                chunk(tool_calls=[tool_delta(1, call_id="b", name="two", arguments="{}")]),
                chunk(tool_calls=[tool_delta(0, call_id="a", name="one", arguments="{}")]),
            ]
        )
        result = await _result_from_stream(stream, None, 1)
        assert [c.name for c in result.tool_calls] == ["one", "two"]

    asyncio.run(run())


def test_chunk_error_raises():
    async def run():
        stream = FakeStream([chunk(error=SimpleNamespace(code=400, message="nope"))])
        with pytest.raises(LLMError) as exc:
            await _result_from_stream(stream, None, 1)
        assert "nope" in str(exc.value)
        assert stream.closed

    asyncio.run(run())


def test_usage_unset_details():
    async def run():
        usage = SimpleNamespace(
            prompt_tokens=3,
            completion_tokens=4,
            total_tokens=7,
            cost=None,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=object()),
            prompt_tokens_details=SimpleNamespace(cached_tokens=None),
        )
        stream = FakeStream([chunk(content="x", usage=usage)])
        result = await _result_from_stream(stream, None, 1)
        assert isinstance(result.usage, Usage)
        assert result.usage.reasoning_tokens == 0
        assert result.usage.cached_tokens == 0

    asyncio.run(run())


def test_idle_timeout_closes():
    class Stall:
        closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(5)
            return chunk(content="x")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.closed = True

    async def run():
        stream = Stall()
        with pytest.raises(TimeoutError):
            await _result_from_stream(stream, None, 0.05)
        assert stream.closed

    asyncio.run(run())


def test_cancelled_closes():
    class Stall:
        closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(10)
            return chunk(content="x")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.closed = True

    async def run():
        stream = Stall()

        async def inner():
            await _result_from_stream(stream, None, 30)

        task = asyncio.create_task(inner())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed

    asyncio.run(run())
