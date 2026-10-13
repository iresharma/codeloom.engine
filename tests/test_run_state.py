import asyncio
from types import SimpleNamespace

import pytest

from protocol.commands import RequestRunState
from runtime.commands.run_state import request_run_state


@pytest.mark.asyncio
async def test_completion_waits_for_queued_reports_and_settlement():
    events = []
    pending = asyncio.create_task(asyncio.sleep(10))
    session = SimpleNamespace(
        _loop=SimpleNamespace(_child_tasks={}, _settle_tasks={"writer": pending}),
        _pending_user=[], _inbox=["child report"], _turn_task=None,
        _prompts=SimpleNamespace(pending=lambda: None), _emit=events.append,
    )
    try:
        await request_run_state(session, RequestRunState())
        assert not events[-1].idle
        assert events[-1].live_settlements == 1
        assert events[-1].queued_messages == 1
        session._loop._settle_tasks.clear()
        session._inbox.clear()
        await request_run_state(session, RequestRunState())
        assert events[-1].idle
    finally:
        pending.cancel()


def test_broker_base_url_preserves_default_constructor(monkeypatch):
    from unittest.mock import patch
    from llm.openrouter import OpenRouterLLM

    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)
    with patch("llm.openrouter.OpenRouter") as constructor:
        OpenRouterLLM("test-key", "test-model")
        constructor.assert_called_once_with(api_key="test-key")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "http://broker:8080/v1")
    with patch("llm.openrouter.OpenRouter") as constructor:
        OpenRouterLLM("trial-token", "test-model")
        constructor.assert_called_once_with(api_key="trial-token", server_url="http://broker:8080/v1")


def test_native_retries_can_be_disabled_for_broker(monkeypatch):
    from llm.openrouter import _retry_config
    monkeypatch.setenv('OPENROUTER_DISABLE_RETRIES', '1')
    assert _retry_config().strategy == 'none'
    assert not _retry_config().retry_connection_errors
    monkeypatch.delenv('OPENROUTER_DISABLE_RETRIES')
    assert _retry_config().strategy == 'backoff'


@pytest.mark.asyncio
async def test_budget_refusal_has_a_distinct_error_code(monkeypatch):
    from unittest.mock import AsyncMock
    from llm.openrouter import OpenRouterLLM
    from llm.provider import LLMError
    monkeypatch.setenv('OPENROUTER_BASE_URL', 'http://broker/v1')
    llm=OpenRouterLLM('attempt-token','test-model')
    error=RuntimeError('budget_exhausted')
    error.status_code=402
    llm._client.chat.send_async=AsyncMock(side_effect=error)
    with pytest.raises(LLMError,match='budget_exhausted') as caught:
        await llm.complete([{'role':'user','content':'task'}])
    assert caught.value.code==402


@pytest.mark.asyncio
async def test_broker_never_fetches_out_of_band_billing(monkeypatch):
    from unittest.mock import AsyncMock
    from llm.openrouter import OpenRouterLLM
    from llm.provider import LLMResult, Usage
    monkeypatch.setenv('OPENROUTER_BASE_URL', 'http://broker/v1')
    llm=OpenRouterLLM('attempt-token','test-model')
    llm._client.generations.get_generation_async=AsyncMock()
    result=LLMResult(generation_id='id',usage=Usage(prompt_tokens=1,total_tokens=1,cost=0))
    assert await llm._fill_missing_cost(result) is result
    llm._client.generations.get_generation_async.assert_not_called()
