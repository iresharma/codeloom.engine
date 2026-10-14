from __future__ import annotations

import asyncio

from protocol.commands import StartSession
from protocol.events import ModelCatalogUpdated, WarningOccurred
from protocol.snapshot import ModelRow
from runtime.config import EngineConfig
from runtime.session import EngineSession
from tests.fakes import FakeProvider


def _row(model_id: str, supports_reasoning: bool = True) -> ModelRow:
    return ModelRow(
        id=model_id,
        name=model_id,
        supports_reasoning=supports_reasoning,
        default_effort="medium" if supports_reasoning else None,
        supported_efforts=None,
    )


async def _started_session(tmp_path, provider):
    session = EngineSession(tmp_path, tmp_path / "session.db")
    await session.start()
    session._llm = provider
    session._config = EngineConfig(max_turns=4)
    await session.handle(StartSession(workspace=str(tmp_path)))
    session._loop._llm = provider
    return session


def test_bind_fetches_and_emits_model_catalog(tmp_path):
    async def run():
        provider = FakeProvider(list_models_result=[_row("anthropic/claude-opus-5.5")])
        session = await _started_session(tmp_path, provider)
        queue = session.subscribe()
        task = session._model_catalog_task
        assert task is not None
        await task
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        updates = [e for e in events if isinstance(e, ModelCatalogUpdated)]
        assert updates
        assert updates[-1].models[0].id == "anthropic/claude-opus-5.5"
        assert session._models[0].id == "anthropic/claude-opus-5.5"
        assert session.snapshot().models[0].id == "anthropic/claude-opus-5.5"
        await session.aclose()

    asyncio.run(run())


def test_catalog_fetch_failure_retains_previous_and_warns(tmp_path):
    async def run():
        provider = FakeProvider(list_models_result=[_row("anthropic/claude-opus-5.5")])
        session = await _started_session(tmp_path, provider)
        await session._model_catalog_task
        assert session._models  # seeded by the successful bind-time fetch

        queue = session.subscribe()
        provider.list_models_error = RuntimeError("upstream exploded")
        await session._refresh_model_catalog()

        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        warnings = [e for e in events if isinstance(e, WarningOccurred)]
        assert any("upstream exploded" in w.message for w in warnings)
        # Last good catalog is retained, not cleared.
        assert session._models[0].id == "anthropic/claude-opus-5.5"
        await session.aclose()

    asyncio.run(run())


def test_catalog_fetch_timeout_retains_previous_and_warns(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setattr("runtime.session.MODEL_CATALOG_TIMEOUT_S", 0.05)
        provider = FakeProvider(list_models_result=[_row("anthropic/claude-opus-5.5")])
        session = await _started_session(tmp_path, provider)
        await session._model_catalog_task
        assert session._models

        queue = session.subscribe()
        provider.list_models_hang = asyncio.Event()  # never set -> always times out
        await session._refresh_model_catalog()

        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        warnings = [e for e in events if isinstance(e, WarningOccurred)]
        assert any("model catalog" in w.message for w in warnings)
        assert session._models[0].id == "anthropic/claude-opus-5.5"
        await session.aclose()

    asyncio.run(run())


def test_request_model_catalog_command_refreshes_on_demand(tmp_path):
    async def run():
        from protocol.commands import RequestModelCatalog

        provider = FakeProvider(list_models_result=[])
        session = await _started_session(tmp_path, provider)
        await session._model_catalog_task

        provider.list_models_result = [_row("openai/gpt-5.6-luna")]
        queue = session.subscribe()
        await session.handle(RequestModelCatalog())
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        updates = [e for e in events if isinstance(e, ModelCatalogUpdated)]
        assert updates
        assert updates[-1].models[0].id == "openai/gpt-5.6-luna"
        await session.aclose()

    asyncio.run(run())
