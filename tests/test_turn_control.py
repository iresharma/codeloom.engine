from __future__ import annotations

import asyncio

from llm.provider import LLMResult, ToolCall
from protocol.commands import AbortAgent, StartSession, SubmitUserMessage
from protocol.events import (
    AgentStateChanged,
    ChatMessageAdded,
    ChatMessageDelta,
    ChatMessageStarted,
    ErrorOccurred,
)
from runtime.config import EngineConfig
from runtime.session import EngineSession
from tests.fakes import FakeProvider


def _session(tmp_path, provider):
    session = EngineSession(tmp_path, tmp_path / "session.db")

    async def setup():
        await session.start()
        session._llm = provider
        session._config = EngineConfig(max_turns=4)
        await session.handle(StartSession(workspace=str(tmp_path)))
        session._loop._llm = provider
        return session

    return setup


def test_submit_returns_before_turn_finishes(tmp_path):
    async def run():
        hang = asyncio.Event()
        session = EngineSession(tmp_path, tmp_path / "session.db")
        await session.start()
        queue = session.subscribe()
        session._llm = FakeProvider(hang=hang)
        await session.handle(StartSession(workspace=str(tmp_path)))
        while not queue.empty():
            queue.get_nowait()
        session._loop._llm = session._llm
        session.start_turn("hello")
        assert session._turn_task is not None
        assert not session._turn_task.done()
        hang.set()
        await session._turn_task

    asyncio.run(run())


def test_second_submit_refused(tmp_path):
    async def run():
        hang = asyncio.Event()
        session = EngineSession(tmp_path, tmp_path / "session.db")
        await session.start()
        queue = session.subscribe()
        session._llm = FakeProvider(hang=hang)
        await session.handle(StartSession(workspace=str(tmp_path)))
        session._loop._llm = session._llm
        while not queue.empty():
            queue.get_nowait()
        session.start_turn("one")
        session.start_turn("two")
        errors = []
        while not queue.empty():
            item = queue.get_nowait()
            if isinstance(item, ErrorOccurred):
                errors.append(item.message)
        assert any("busy" in message for message in errors)
        hang.set()
        await session._turn_task

    asyncio.run(run())


def test_abort_mid_complete(tmp_path):
    async def run():
        hang = asyncio.Event()
        session = EngineSession(tmp_path, tmp_path / "session.db")
        await session.start()
        queue = session.subscribe()
        session._llm = FakeProvider(hang=hang)
        await session.handle(StartSession(workspace=str(tmp_path)))
        session._loop._llm = session._llm
        while not queue.empty():
            queue.get_nowait()
        session.start_turn("hello")
        await asyncio.sleep(0)
        task = session._turn_task
        assert task is not None
        assert session.abort_turn()
        await task
        texts = [m.text for m in session._state.messages if m.role == "assistant"]
        assert any("aborted" in text for text in texts)
        states = []
        while not queue.empty():
            item = queue.get_nowait()
            if isinstance(item, AgentStateChanged):
                states.append(item.state)
        assert "aborting" in states
        assert states[-1] == "idle"

    asyncio.run(run())


def test_abort_without_turn(tmp_path):
    async def run():
        session = EngineSession(tmp_path, tmp_path / "session.db")
        await session.start()
        queue = session.subscribe()
        await session.handle(StartSession(workspace=str(tmp_path)))
        while not queue.empty():
            queue.get_nowait()
        await session.handle(AbortAgent())
        messages = []
        while not queue.empty():
            item = queue.get_nowait()
            if isinstance(item, ErrorOccurred):
                messages.append(item.message)
        assert any("no agent turn" in message for message in messages)

    asyncio.run(run())


def test_abort_with_agent_id(tmp_path):
    async def run():
        session = EngineSession(tmp_path, tmp_path / "session.db")
        await session.start()
        queue = session.subscribe()
        await session.handle(StartSession(workspace=str(tmp_path)))
        while not queue.empty():
            queue.get_nowait()
        await session.handle(AbortAgent(agent_id="x"))
        messages = []
        while not queue.empty():
            item = queue.get_nowait()
            if isinstance(item, ErrorOccurred):
                messages.append(item.message)
        assert any("subagents" in message for message in messages)

    asyncio.run(run())


def test_abort_between_tool_calls(tmp_path):
    async def run():
        hang = asyncio.Event()

        class SlowTools(FakeProvider):
            async def complete(self, messages, tools=None, *, on_delta=None):
                if self.calls == 0:
                    self.calls += 1
                    return LLMResult(
                        text="",
                        tool_calls=[
                            ToolCall(id="1", name="list_files", arguments_json="{}")
                        ],
                    )
                await hang.wait()
                return LLMResult(text="done")

        session = EngineSession(tmp_path, tmp_path / "session.db")
        await session.start()
        session._llm = SlowTools()
        await session.handle(StartSession(workspace=str(tmp_path)))
        session._loop._llm = session._llm
        session.start_turn("go")
        await asyncio.sleep(0.05)
        task = session._turn_task
        assert task is not None
        session.abort_turn()
        await task
        history = session._loop._history
        assert not any(item.get("tool_calls") for item in history)

    asyncio.run(run())


def test_added_reuses_stream_id(tmp_path):
    async def run():
        provider = FakeProvider(
            results=[LLMResult(text="hello")],
            deltas=[("text", "hel"), ("text", "lo")],
        )
        session = EngineSession(tmp_path, tmp_path / "session.db")
        await session.start()
        queue = session.subscribe()
        session._llm = provider
        await session.handle(StartSession(workspace=str(tmp_path)))
        session._loop._llm = provider
        while not queue.empty():
            queue.get_nowait()
        session.start_turn("hi")
        await session._turn_task
        started = []
        deltas = []
        added = []
        while not queue.empty():
            item = queue.get_nowait()
            if isinstance(item, ChatMessageStarted):
                started.append(item)
            elif isinstance(item, ChatMessageDelta):
                deltas.append(item)
            elif isinstance(item, ChatMessageAdded) and item.role == "assistant":
                added.append(item)
        assert started and deltas and added
        assert added[-1].id == started[-1].id
        assert all(item.id == added[-1].id for item in deltas)

    asyncio.run(run())


def test_client_skips_already_streamed_added():
    import dummy_client

    dummy_client._STREAM_ID = ""
    dummy_client.format_event(ChatMessageDelta(id="m1", channel="text", text="hi"))
    assert (
        dummy_client.format_event(
            ChatMessageAdded(id="m1", role="assistant", text="hi", ts="t")
        )
        == ""
    )
    assert (
        dummy_client.format_event(
            ChatMessageAdded(id="other", role="assistant", text="bye", ts="t")
        )
        == "assistant: bye"
    )
