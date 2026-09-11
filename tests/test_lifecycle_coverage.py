"""Coverage tests for runtime/commands/lifecycle.py"""
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


def test_start_session_workspace_mismatch(session):
    """Test StartSession with mismatched workspace."""
    from runtime.commands.lifecycle import start_session

    other = Path("/other/workspace")
    cmd = StartSession(workspace=str(other), session_id=None)
    events = []
    session._emit = events.append

    async def run():
        await start_session(session, cmd)

    asyncio.run(run())
    assert len(events) >= 1
    assert any(isinstance(e, ErrorOccurred) for e in events)


def test_start_session_new(session):
    """Test StartSession creates new session."""
    from runtime.commands.lifecycle import start_session

    cmd = StartSession(workspace=str(session._workspace), session_id=None)
    events = []
    session._emit = events.append

    async def run():
        await start_session(session, cmd)

    asyncio.run(run())
    assert session._state is not None


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
    assert any(isinstance(e, ErrorOccurred) for e in events)


def test_list_sessions(session):
    """Test ListSessions command."""
    from runtime.commands.lifecycle import list_stored_sessions, start_session

    cmd1 = StartSession(workspace=str(session._workspace), session_id=None)
    session._emit = lambda x: None

    async def run1():
        await start_session(session, cmd1)

    asyncio.run(run1())
    session._persist()

    events = []
    session._emit = events.append

    async def run2():
        await list_stored_sessions(session, ListSessions())

    asyncio.run(run2())
    assert any(isinstance(e, SessionList) for e in events)


def test_request_snapshot_no_session(session):
    """Test RequestSnapshot when no session started."""
    from runtime.commands.lifecycle import request_snapshot

    events = []
    session._emit = events.append
    request_snapshot(session, RequestSnapshot(replay=False))
    assert any(isinstance(e, ErrorOccurred) for e in events)


def test_request_orch_context_no_session(session):
    """Test RequestOrchContext when no session started."""
    from runtime.commands.lifecycle import request_orch_context

    events = []
    session._emit = events.append
    request_orch_context(session, RequestOrchContext())
    assert any(isinstance(e, ErrorOccurred) for e in events)


def test_shutdown_command(session):
    """Test Shutdown command."""
    from runtime.commands.lifecycle import shutdown

    events = []
    session._emit = events.append

    async def run():
        await shutdown(session, Shutdown())

    asyncio.run(run())
