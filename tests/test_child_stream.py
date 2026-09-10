from __future__ import annotations

import asyncio

from agents.hooks import AgentHooks
from agents.profiles.ask import PROFILE
from agents.subagent import Subagent
from dummy_client import format_event, route_event
from llm.provider import LLMResult, ToolCall
from protocol.events import (
    AgentFinished,
    AgentStarted,
    ChatMessageAdded,
    ChatMessageDelta,
    ChatMessageStarted,
)
from runtime.session import EngineSession
from tests.fakes import FakeProvider
from tests.test_orchestrator import (
    _AskHangThenDone,
    _AskThenDone,
    _bind,
    _is_orch,
    _queued,
    _wait_idle,
)
from tools.registry import ToolRegistry


def _session(tmp_path):
    session = EngineSession(tmp_path, tmp_path / "session.db")
    queue = session.subscribe()
    return session, queue


def _drain(queue):
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


def test_child_text_delta_emits_started_then_delta(tmp_path):
    async def run():
        session, queue = _session(tmp_path)
        session._on_delta("m1", "text", "hel", agent_id="a1")
        session._on_delta("m1", "text", "lo", agent_id="a1")
        events = _drain(queue)
        started = [item for item in events if isinstance(item, ChatMessageStarted)]
        deltas = [item for item in events if isinstance(item, ChatMessageDelta)]
        assert len(started) == 1
        assert started[0].id == "m1"
        assert started[0].role == "assistant"
        assert started[0].agent_id == "a1"
        assert [item.text for item in deltas] == ["hel", "lo"]
        assert all(item.agent_id == "a1" and item.id == "m1" for item in deltas)

    asyncio.run(run())


def test_orch_delta_has_empty_agent_id(tmp_path):
    async def run():
        session, queue = _session(tmp_path)
        session._on_delta("m1", "text", "hi")
        events = _drain(queue)
        started = [item for item in events if isinstance(item, ChatMessageStarted)]
        deltas = [item for item in events if isinstance(item, ChatMessageDelta)]
        assert started[0].agent_id == ""
        assert deltas[0].agent_id == ""

    asyncio.run(run())


def test_reasoning_delta_does_not_start_chat(tmp_path):
    async def run():
        session, queue = _session(tmp_path)
        session._on_delta("m1", "reasoning", "plan", agent_id="a1")
        events = _drain(queue)
        assert not any(isinstance(item, ChatMessageStarted) for item in events)
        deltas = [item for item in events if isinstance(item, ChatMessageDelta)]
        assert deltas == [
            ChatMessageDelta(id="m1", channel="reasoning", text="plan", agent_id="a1")
        ]

    asyncio.run(run())


def test_child_message_is_emitted_not_persisted(tmp_path):
    async def run():
        session, queue = _session(tmp_path)
        session._state.session_id = "s1"
        session._on_child_message("m1", "retry is in server.py", agent_id="a1")
        events = _drain(queue)
        added = [item for item in events if isinstance(item, ChatMessageAdded)]
        assert len(added) == 1
        assert added[0].id == "m1"
        assert added[0].role == "assistant"
        assert added[0].text == "retry is in server.py"
        assert added[0].agent_id == "a1"
        assert session._state.messages == []

    asyncio.run(run())


def test_child_message_skips_empty_and_orch(tmp_path):
    async def run():
        session, queue = _session(tmp_path)
        session._on_child_message("m1", "   ", agent_id="a1")
        session._on_child_message("m2", "hello", agent_id="")
        session._on_child_message("m3", "", agent_id="a1")
        assert _drain(queue) == []

    asyncio.run(run())


def test_child_stream_does_not_steal_orch_id(tmp_path):
    session = EngineSession(tmp_path, tmp_path / "session.db")
    session._on_message_start("orch-msg")
    session._on_message_start("child-msg", agent_id="a1")
    assert session._stream_id == "orch-msg"


def test_child_hooks_wire_delta_and_message(tmp_path):
    async def run():
        session, queue = _session(tmp_path)
        orch = session._hooks_for("")
        child = session._hooks_for("agent-1")
        assert orch.on_delta is not None
        assert orch.on_message is None
        assert child.on_delta is not None
        assert child.on_message_start is not None
        assert child.on_message is not None
        child.on_message_start("mid")
        child.on_delta("mid", "text", "hi")
        child.on_message("mid", "hi")
        events = _drain(queue)
        assert any(
            isinstance(item, ChatMessageStarted) and item.agent_id == "agent-1"
            for item in events
        )
        assert any(
            isinstance(item, ChatMessageDelta)
            and item.agent_id == "agent-1"
            and item.text == "hi"
            for item in events
        )
        assert any(
            isinstance(item, ChatMessageAdded)
            and item.agent_id == "agent-1"
            and item.text == "hi"
            for item in events
        )

    asyncio.run(run())


def test_subagent_loop_streams_then_emits_message(tmp_path):
    async def run():
        seen = []
        provider = FakeProvider(
            results=[LLMResult(text="child reply")],
            deltas=[("reasoning", "think"), ("text", "child "), ("text", "reply")],
        )
        child = Subagent(
            PROFILE,
            llm=provider,
            tools=ToolRegistry(),
            workspace=tmp_path,
            hooks=AgentHooks(
                on_delta=lambda mid, channel, text: seen.append(
                    ("delta", mid, channel, text)
                ),
                on_message=lambda mid, text: seen.append(("message", mid, text)),
            ),
            config=None,
        )
        text = await child.run("where is retry?")
        assert text == "child reply"
        deltas = [item for item in seen if item[0] == "delta"]
        messages = [item for item in seen if item[0] == "message"]
        assert [item[2:] for item in deltas] == [
            ("reasoning", "think"),
            ("text", "child "),
            ("text", "reply"),
        ]
        assert len(messages) == 1
        assert messages[0][2] == "child reply"
        assert messages[0][1] == deltas[-1][1]

    asyncio.run(run())


def test_spawn_streams_child_tokens_to_client(tmp_path):
    async def run():
        session = await _bind(tmp_path, _AskThenDone())()
        queue = session.subscribe()
        while not queue.empty():
            queue.get_nowait()
        session.start_turn("where is retry?")
        await _wait_idle(session)
        events = _queued(queue)
        started = [item for item in events if isinstance(item, AgentStarted)]
        assert started
        agent_id = started[0].agent_id
        child_started = [
            item
            for item in events
            if isinstance(item, ChatMessageStarted) and item.agent_id == agent_id
        ]
        child_deltas = [
            item
            for item in events
            if isinstance(item, ChatMessageDelta) and item.agent_id == agent_id
        ]
        child_added = [
            item
            for item in events
            if isinstance(item, ChatMessageAdded)
            and item.role == "assistant"
            and item.agent_id == agent_id
        ]
        orch_added = [
            item
            for item in events
            if isinstance(item, ChatMessageAdded)
            and item.role == "assistant"
            and not item.agent_id
        ]
        assert child_started
        assert [item.text for item in child_deltas] == ["retry is in server.py"]
        assert child_added[-1].text == "retry is in server.py"
        assert child_added[-1].id == child_started[-1].id
        assert all(item.id == child_added[-1].id for item in child_deltas)
        assert orch_added
        assert all("retry is in server.py" != item.text for item in orch_added)
        assert all(
            "retry is in server.py" != message.text
            for message in session._state.messages
            if message.role == "assistant"
        )

    asyncio.run(run())


def test_parallel_children_tag_their_own_streams(tmp_path):
    async def run():
        hang = asyncio.Event()
        session = await _bind(tmp_path, _TwoStreamingAsks(hang))()
        queue = session.subscribe()
        while not queue.empty():
            queue.get_nowait()
        session.start_turn("look around")
        started = []
        for _ in range(50):
            started.extend(
                item for item in _queued(queue) if isinstance(item, AgentStarted)
            )
            if len(started) >= 2:
                break
            await asyncio.sleep(0.02)
        assert len(started) >= 2
        hang.set()
        await _wait_idle(session)
        events = _queued(queue)
        ids = {item.agent_id for item in started}
        added = [
            item
            for item in events
            if isinstance(item, ChatMessageAdded)
            and item.role == "assistant"
            and item.agent_id
        ]
        child_ids = {item.agent_id for item in added}
        assert ids <= child_ids
        assert len(child_ids) >= 2

    asyncio.run(run())


def test_abort_child_does_not_emit_final_message(tmp_path):
    async def run():
        hang = asyncio.Event()
        session = await _bind(tmp_path, _AskHangThenDone(hang))()
        queue = session.subscribe()
        while not queue.empty():
            queue.get_nowait()
        session.start_turn("ask something")
        agent_id = ""
        for _ in range(50):
            for item in _queued(queue):
                if isinstance(item, AgentStarted):
                    agent_id = item.agent_id
            if agent_id:
                break
            await asyncio.sleep(0.02)
        assert agent_id
        assert session.abort_child(agent_id)
        hang.set()
        await _wait_idle(session)
        events = _queued(queue)
        finished = [item for item in events if isinstance(item, AgentFinished)]
        assert any(item.status == "aborted" for item in finished)
        child_added = [
            item
            for item in events
            if isinstance(item, ChatMessageAdded)
            and item.role == "assistant"
            and item.agent_id == agent_id
        ]
        assert not child_added

    asyncio.run(run())


def test_client_routes_and_formats_child_chat():
    started = ChatMessageStarted(
        id="m1", role="assistant", ts="t", agent_id="aaaaaaaa"
    )
    delta = ChatMessageDelta(
        id="m1", channel="text", text="hi", agent_id="aaaaaaaa"
    )
    added = ChatMessageAdded(
        id="c1", role="assistant", text="child", ts="t", agent_id="aaaaaaaa"
    )
    assert route_event(started) == "agents"
    assert route_event(delta) == "agents"
    assert route_event(added) == "agents"
    assert format_event(started) == "agent aaaaaaaa streaming"
    import dummy_client

    dummy_client._STREAM_ID = ""
    assert format_event(added) == "assistant [aaaaaaaa]: child"


class _TwoStreamingAsks(FakeProvider):
    def __init__(self, hang):
        super().__init__()
        self.hang = hang
        self.orch_spawned = False

    async def complete(self, messages, tools=None, *, on_delta=None):
        if _is_orch(tools):
            if not self.orch_spawned:
                self.orch_spawned = True
                return LLMResult(
                    text="",
                    tool_calls=[
                        ToolCall(id="1", name="ask", arguments_json='{"task":"a"}'),
                        ToolCall(id="2", name="ask", arguments_json='{"task":"b"}'),
                    ],
                )
            return LLMResult(text="spawned both")
        await self.hang.wait()
        if on_delta:
            on_delta("text", "done")
        return LLMResult(text="done")
