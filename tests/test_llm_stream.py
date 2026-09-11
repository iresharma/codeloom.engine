from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from llm.openrouter import (
    OpenRouterLLM,
    _cost_from,
    _prefer_usage,
    _result_from_stream,
    _usage_from,
)
from llm.provider import LLMError, LLMResult, Usage
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


def test_cost_falls_back_to_cost_details():
    class Unset:
        def __bool__(self):
            return False

    raw = SimpleNamespace(
        prompt_tokens=10,
        completion_tokens=2,
        total_tokens=12,
        cost=Unset(),
        cost_details=SimpleNamespace(
            upstream_inference_cost=0.0123,
            upstream_inference_prompt_cost=0.01,
            upstream_inference_completions_cost=0.0023,
        ),
        completion_tokens_details=None,
        prompt_tokens_details=None,
    )
    usage = _usage_from(raw)
    assert usage is not None
    assert abs(usage.cost - 0.0123) < 1e-9
    assert abs(_cost_from(SimpleNamespace(cost=Unset(), cost_details=None)) - 0.0) < 1e-9
    split = SimpleNamespace(
        cost=None,
        cost_details=SimpleNamespace(
            upstream_inference_cost=Unset(),
            upstream_inference_prompt_cost=0.008,
            upstream_inference_completions_cost=0.002,
        ),
    )
    assert abs(_cost_from(split) - 0.01) < 1e-9


def test_prefer_usage_keeps_cost_when_later_chunk_omits_it():
    first = Usage(prompt_tokens=1, total_tokens=1, cost=0.2, requests=1)
    second = Usage(prompt_tokens=10, completion_tokens=2, total_tokens=12, cost=0.0, requests=1)
    merged = _prefer_usage(first, second)
    assert merged is not None
    assert merged.total_tokens == 12
    assert abs(merged.cost - 0.2) < 1e-9


def test_stream_keeps_generation_id_and_merges_cost():
    async def run():
        stream = FakeStream(
            [
                chunk(
                    content="x",
                    chunk_id="gen-1",
                    usage=SimpleNamespace(
                        prompt_tokens=2,
                        completion_tokens=0,
                        total_tokens=2,
                        cost=0.05,
                        completion_tokens_details=None,
                        prompt_tokens_details=None,
                    ),
                ),
                chunk(
                    content="y",
                    chunk_id="gen-1",
                    usage=SimpleNamespace(
                        prompt_tokens=2,
                        completion_tokens=3,
                        total_tokens=5,
                        cost=None,
                        completion_tokens_details=None,
                        prompt_tokens_details=None,
                    ),
                ),
            ]
        )
        result = await _result_from_stream(stream, None, 1)
        assert result.generation_id == "gen-1"
        assert result.usage is not None
        assert result.usage.total_tokens == 5
        assert abs(result.usage.cost - 0.05) < 1e-9

    asyncio.run(run())


def test_fill_missing_cost_from_generation():
    async def run():
        llm = OpenRouterLLM(api_key="sk-test", model="fake")

        class Gens:
            async def get_generation_async(self, id):
                assert id == "gen-99"
                return SimpleNamespace(data=SimpleNamespace(total_cost=0.42, usage=0.42))

        llm._client = SimpleNamespace(generations=Gens())
        result = await llm._fill_missing_cost(
            LLMResult(
                usage=Usage(prompt_tokens=10, total_tokens=12, cost=0.0, requests=1),
                generation_id="gen-99",
            )
        )
        assert abs(result.usage.cost - 0.42) < 1e-9

    asyncio.run(run())


def test_idle_timeout_closes():
    class Stall:
        closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            # _result_from_stream wraps this call in asyncio.wait_for(idle_s),
            # so it is cancelled well before this sleep completes. The
            # duration only needs to comfortably outlast idle_s (0.05s)
            # below; kept at 0.2s (not near-zero) so a regression that
            # dropped the wait_for wrapper would still be caught by
            # pytest-timeout/CI rather than passing accidentally fast.
            await asyncio.sleep(0.2)
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
            # The outer task is cancelled after 0.01s (below), well before
            # this sleep can complete; the duration just needs to be long
            # enough that __anext__ is still pending at cancellation time.
            await asyncio.sleep(0.2)
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
