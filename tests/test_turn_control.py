from __future__ import annotations

import asyncio
import time

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


def test_second_submit_is_queued(tmp_path):
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
        assert not any("busy" in message for message in errors)
        hang.set()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            turn = session._turn_task
            if (turn is None or turn.done()) and not session._pending_user:
                break
            await asyncio.sleep(0.02)
        users = [m.text for m in session._state.messages if m.role == "user"]
        assert users == ["one", "two"]

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
        assert any("unknown agent" in message for message in messages)

    asyncio.run(run())


def test_abort_between_tool_calls(tmp_path):
    async def run():
        hang = asyncio.Event()

        class SlowTools(FakeProvider):
            async def complete(self, messages, tools=None, *, on_delta=None, **kwargs):
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


def test_tool_only_complete_does_not_start_chat(tmp_path):
    async def run():
        session = EngineSession(tmp_path, tmp_path / "session.db")
        await session.start()
        queue = session.subscribe()
        session._on_message_start("m-tool")
        session._on_delta("m-tool", "reasoning", "planning")
        session._add_message("assistant", "   ")
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        assert not any(isinstance(item, ChatMessageStarted) for item in events)
        assert not any(
            isinstance(item, ChatMessageAdded) and item.role == "assistant"
            for item in events
        )
        session._on_delta("m-tool", "text", "hello")
        session._add_message("assistant", "hello", message_id="m-tool")
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
        assert len(started) == 1
        assert started[0].id == "m-tool"
        assert [item.text for item in deltas] == ["hello"]
        assert added and added[-1].id == "m-tool"

    asyncio.run(run())


def test_child_stream_does_not_steal_orch_id(tmp_path):
    session = EngineSession(tmp_path, tmp_path / "session.db")
    session._on_message_start("orch-msg")
    assert session._stream_id == "orch-msg"
    session._on_message_start("child-msg", agent_id="abc")
    assert session._stream_id == "orch-msg"


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


def test_output_cutoff_continues_briefing(tmp_path):
    from agents.agent_loop import AgentLoop

    provider = FakeProvider(
        results=[
            LLMResult(text="Module | Cov\nserver.py | 0", finish_reason="length"),
            LLMResult(text="\nlanguage.py | 0", finish_reason="stop"),
        ]
    )
    loop = AgentLoop(provider, workspace=tmp_path)

    async def run():
        return await loop.run("survey coverage")

    text = asyncio.run(run())
    assert "server.py" in text
    assert "language.py" in text
    assert provider.calls == 2
