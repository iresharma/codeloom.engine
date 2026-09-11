from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from protocol.commands import (
    ListSessions,
    RequestOrchContext,
    RequestSnapshot,
    Shutdown,
    StartSession,
    SubmitUserMessage,
)
from protocol.events import ErrorOccurred, SessionList, WarningOccurred
from runtime.session import EngineSession
from runtime.store.edits import ensure_schema


@pytest.fixture
def session(tmp_path):
    """Create a test EngineSession."""
    db = tmp_path / "session.db"
    ensure_schema(db)
    sess = EngineSession(tmp_path, db)
    return sess


def test_start_session_wrong_workspace(session):
    """Test StartSession with mismatched workspace."""
    from runtime.commands.lifecycle import start_session

    other_workspace = Path("/other/workspace")
    cmd = StartSession(workspace=str(other_workspace), session_id=None)
    events = []
    session._emit = events.append

    async def run():
        await start_session(session, cmd)

    asyncio.run(run())
    assert len(events) == 1
    assert isinstance(events[0], ErrorOccurred)
    assert "workspace mismatch" in str(events[0])


def test_start_session_new_session(session):
    """Test StartSession creates a new session."""
    from runtime.commands.lifecycle import start_session

    cmd = StartSession(workspace=str(session._workspace), session_id=None)
    events = []
    session._emit = events.append

    async def run():
        await start_session(session, cmd)

    asyncio.run(run())
    assert session._state is not None
    assert session._state.session_id is not None
    # Should emit snapshot and possibly warning
    assert len(events) >= 1


def test_start_session_restore_unknown(session):
    """Test StartSession with unknown session_id."""
    from runtime.commands.lifecycle import start_session

    unknown_id = uuid4().hex
    cmd = StartSession(workspace=str(session._workspace), session_id=unknown_id)
    events = []
    session._emit = events.append

    async def run():
        await start_session(session, cmd)

    asyncio.run(run())
    assert len(events) == 1
    assert isinstance(events[0], ErrorOccurred)
    assert "unknown session" in str(events[0])


def test_list_stored_sessions(session):
    """Test ListSessions command."""
    from runtime.commands.lifecycle import list_stored_sessions, start_session

    # Create and save a session
    cmd1 = StartSession(workspace=str(session._workspace), session_id=None)
    session._emit = lambda x: None

    async def run1():
        await start_session(session, cmd1)

    asyncio.run(run1())
    session._persist()

    # Now list sessions
    events = []
    session._emit = events.append
    cmd2 = ListSessions()
    list_stored_sessions(session, cmd2)

    assert len(events) == 1
    assert isinstance(events[0], SessionList)
    assert isinstance(events[0].sessions, list)


def test_request_snapshot_no_session(session):
    """Test RequestSnapshot with no active session."""
    from runtime.commands.lifecycle import request_snapshot

    session._state = None
    events = []
    session._emit = events.append

    cmd = RequestSnapshot(replay=False)
    request_snapshot(session, cmd)

    assert len(events) == 1
    assert isinstance(events[0], ErrorOccurred)


def test_request_snapshot_with_session(session):
    """Test RequestSnapshot with active session."""
    from runtime.commands.lifecycle import request_snapshot, start_session

    cmd = StartSession(workspace=str(session._workspace), session_id=None)
    session._emit = lambda x: None

    async def run():
        await start_session(session, cmd)

    asyncio.run(run())

    events = []
    session._emit = events.append

    cmd2 = RequestSnapshot(replay=False)
    request_snapshot(session, cmd2)

    # Should emit a snapshot event
    assert len(events) >= 1


def test_request_orch_context_no_session(session):
    """Test RequestOrchContext with no active session."""
    from runtime.commands.lifecycle import request_orch_context

    session._state = None
    events = []
    session._emit = events.append

    cmd = RequestOrchContext()
    request_orch_context(session, cmd)

    assert len(events) == 1
    assert isinstance(events[0], ErrorOccurred)


def test_shutdown_command(session):
    """Test Shutdown command closes the session."""
    from runtime.commands.lifecycle import shutdown, start_session

    cmd = StartSession(workspace=str(session._workspace), session_id=None)
    session._emit = lambda x: None

    async def run():
        await start_session(session, cmd)

    asyncio.run(run())

    # Now shutdown
    async def run_shutdown():
        cmd2 = Shutdown()
        await shutdown(session, cmd2)

    asyncio.run(run_shutdown())
    # Session should be closed
    assert session._state is None


def test_submit_user_message_no_session(session):
    """Test SubmitUserMessage with no active session."""
    from runtime.commands.lifecycle import submit_user_message

    session._state = None
    events = []
    session._emit = events.append

    cmd = SubmitUserMessage(text="hello")
    submit_user_message(session, cmd)

    assert len(events) == 1
    assert isinstance(events[0], ErrorOccurred)
