"""Additional coverage tests for runtime/commands/lifecycle.py"""
from __future__ import annotations

import asyncio

import pytest

from protocol.commands import (
    RequestAgentTranscript,
    RequestContext,
    RequestMemory,
    RequestOrchContext,
    RequestSnapshot,
    StartSession,
    SubmitUserMessage,
)
from protocol.events import ChatHistoryAdded, ChatHistoryComplete, ErrorOccurred
from runtime.commands.lifecycle import (
    request_agent_transcript,
    request_context,
    request_memory,
    request_orch_context,
    request_snapshot,
    submit_user_message,
)
from runtime.session import EngineSession
from runtime.store.edits import ensure_schema


@pytest.fixture
def session(tmp_path):
    """Create a test EngineSession."""
    db = tmp_path / "session.db"
    ensure_schema(db)
    sess = EngineSession(tmp_path, db)
    return sess


def test_submit_user_message_no_session(session):
    """Test SubmitUserMessage when no session exists."""
    cmd = SubmitUserMessage(text="hello")
    events = []
    session._emit = events.append
    
    submit_user_message(session, cmd)
    
    assert len(events) == 1
    assert isinstance(events[0], ErrorOccurred)


def test_submit_user_message_with_pending_settle(session):
    """Test SubmitUserMessage with settle prompt."""
    from types import SimpleNamespace
    asyncio.run(session.start())
    session._state.session_id = "test"
    
    pending = SimpleNamespace(prompt_id="p1", choices=("merge", "pr", "keep", "discard"))
    session._prompts.pending = lambda: pending
    session._prompts.answer = lambda pid, text: None
    
    cmd = SubmitUserMessage(text="merge")
    events = []
    session._emit = events.append
    
    submit_user_message(session, cmd)


def test_request_agent_transcript_empty_agent_id(session):
    """Test RequestAgentTranscript with empty agent_id."""
    asyncio.run(session.start())
    session._state.session_id = "test"
    from types import SimpleNamespace
    session._loop = SimpleNamespace()
    
    cmd = RequestAgentTranscript(agent_id="")
    events = []
    session._emit = events.append
    
    request_agent_transcript(session, cmd)
    
    assert len(events) == 1
    assert isinstance(events[0], ErrorOccurred)


def test_request_agent_transcript_unknown_agent(session):
    """Test RequestAgentTranscript with unknown agent."""
    asyncio.run(session.start())
    session._state.session_id = "test"
    from types import SimpleNamespace
    session._loop = SimpleNamespace(transcript_for=lambda agent_id: None)
    
    cmd = RequestAgentTranscript(agent_id="unknown")
    events = []
    session._emit = events.append
    
    request_agent_transcript(session, cmd)
    
    assert len(events) == 1
    assert isinstance(events[0], ErrorOccurred)


def test_request_agent_transcript_empty_history(session):
    """Test RequestAgentTranscript with empty history."""
    asyncio.run(session.start())
    session._state.session_id = "test"
    from types import SimpleNamespace
    session._loop = SimpleNamespace(transcript_for=lambda agent_id: [])
    
    cmd = RequestAgentTranscript(agent_id="agent1")
    events = []
    session._emit = events.append
    
    request_agent_transcript(session, cmd)
    
    assert len(events) == 1
    assert isinstance(events[0], ChatHistoryComplete)


def test_request_agent_transcript_with_history(session):
    """Test RequestAgentTranscript with transcript history."""
    asyncio.run(session.start())
    session._state.session_id = "test"
    from types import SimpleNamespace
    
    lines = [
        {"role": "user", "text": "q"},
        {"role": "assistant", "text": "a"},
    ]
    session._loop = SimpleNamespace(transcript_for=lambda agent_id: lines)
    
    cmd = RequestAgentTranscript(agent_id="agent1")
    events = []
    session._emit = events.append
    
    request_agent_transcript(session, cmd)
    
    added = [e for e in events if isinstance(e, ChatHistoryAdded)]
    assert len(added) == 2


def test_request_agent_transcript_missing_fields(session):
    """Test RequestAgentTranscript handles missing role/text."""
    asyncio.run(session.start())
    session._state.session_id = "test"
    from types import SimpleNamespace
    
    lines = [{"text": "t"}, {"role": "user"}, {}]
    session._loop = SimpleNamespace(transcript_for=lambda agent_id: lines)
    
    cmd = RequestAgentTranscript(agent_id="agent1")
    events = []
    session._emit = events.append
    
    request_agent_transcript(session, cmd)
    
    added = [e for e in events if isinstance(e, ChatHistoryAdded)]
    assert len(added) == 3


def test_request_context_no_loop(session):
    """Test RequestContext when no loop exists."""
    asyncio.run(session.start())
    session._state.session_id = "test"
    session._loop = None
    
    cmd = RequestContext(agent_id="agent1")
    events = []
    session._emit = events.append
    
    request_context(session, cmd)
    
    assert len(events) == 1
    assert isinstance(events[0], ErrorOccurred)


def test_request_context_unknown_agent(session):
    """Test RequestContext with unknown agent."""
    asyncio.run(session.start())
    session._state.session_id = "test"
    from types import SimpleNamespace
    session._loop = SimpleNamespace(breakdown_for=lambda agent_id: None)
    
    cmd = RequestContext(agent_id="unknown")
    events = []
    session._emit = events.append
    
    request_context(session, cmd)
    
    assert len(events) == 1
    assert isinstance(events[0], ErrorOccurred)


def test_request_context_with_breakdown(session):
    """Test RequestContext returns breakdown."""
    asyncio.run(session.start())
    session._state.session_id = "test"
    from types import SimpleNamespace
    
    section = SimpleNamespace(
        title="Files",
        text="file1.py\nfile2.py",
        tokens_est=10,
        chars=15,
    )
    breakdown = SimpleNamespace(
        agent_id="agent1",
        sections=[section],
    )
    
    session._loop = SimpleNamespace(breakdown_for=lambda agent_id: breakdown)
    
    cmd = RequestContext(agent_id="agent1")
    events = []
    session._emit = events.append
    
    request_context(session, cmd)
    
    assert len(events) == 1


def test_request_memory(session):
    """Test RequestMemory command."""
    asyncio.run(session.start())
    session._state.session_id = "test"
    
    cmd = RequestMemory()
    call_count = [0]
    original = session._emit_memory
    
    def mock():
        call_count[0] += 1
    
    session._emit_memory = mock
    request_memory(session, cmd)
    
    assert call_count[0] == 1


def test_request_orch_context_with_loop(session):
    """Test RequestOrchContext with active loop."""
    asyncio.run(session.start())
    session._state.session_id = "test"
    from types import SimpleNamespace
    
    session._loop = SimpleNamespace(context_dump=lambda: "ctx" * 100)
    
    cmd = RequestOrchContext()
    events = []
    session._emit = events.append
    
    request_orch_context(session, cmd)
    
    assert len(events) == 1


def test_request_snapshot_with_replay(session):
    """Test RequestSnapshot with replay=True."""
    asyncio.run(session.start())
    session._state.session_id = "test"
    
    cmd = RequestSnapshot(replay=True)
    events = []
    session._emit = events.append
    
    request_snapshot(session, cmd)
