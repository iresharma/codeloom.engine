"""Coverage tests for runtime/commands/files.py"""
from __future__ import annotations

import asyncio

import pytest

from protocol.commands import CloseFile, CreatePath, DeletePath, OpenFile, RenamePath, UndoLastEdit
from protocol.events import ErrorOccurred, FileClosed, PathChanged
from runtime.commands.files import close_file, create_path, delete_path_cmd, open_file, rename_path_cmd, undo_last_edit
from runtime.session import EngineSession
from runtime.store.edits import ensure_schema


@pytest.fixture
def session(tmp_path):
    db = tmp_path / "session.db"
    ensure_schema(db)
    sess = EngineSession(tmp_path, db)
    return sess


def test_open_file_no_session(session):
    cmd = OpenFile(path="test.py")
    events = []
    session._emit = events.append
    open_file(session, cmd)
    assert len(events) == 1
    assert isinstance(events[0], ErrorOccurred)


def test_close_file_no_session(session):
    cmd = CloseFile(path="test.py")
    events = []
    session._emit = events.append
    close_file(session, cmd)
    assert len(events) == 1
    assert isinstance(events[0], ErrorOccurred)


def test_close_file_not_open(session):
    asyncio.run(session.start())
    session._state.session_id = "test"
    cmd = CloseFile(path="missing.py")
    events = []
    session._emit = events.append
    close_file(session, cmd)
    assert len(events) == 1
    assert isinstance(events[0], ErrorOccurred)


def test_undo_last_edit_no_session(session):
    cmd = UndoLastEdit()
    events = []
    session._emit = events.append
    
    async def run():
        await undo_last_edit(session, cmd)
    
    asyncio.run(run())
    assert len(events) == 1
    assert isinstance(events[0], ErrorOccurred)


def test_create_path_no_session(session):
    cmd = CreatePath(path="new.txt", is_dir=False, content="")
    events = []
    session._emit = events.append
    
    async def run():
        await create_path(session, cmd)
    
    asyncio.run(run())
    assert len(events) == 1
    assert isinstance(events[0], ErrorOccurred)


def test_create_path_empty_path(session):
    asyncio.run(session.start())
    session._state.session_id = "test"
    cmd = CreatePath(path="", is_dir=False, content="")
    events = []
    session._emit = events.append
    
    async def run():
        await create_path(session, cmd)
    
    asyncio.run(run())
    assert len(events) == 1
    assert isinstance(events[0], ErrorOccurred)


def test_rename_path_no_session(session):
    cmd = RenamePath(src="old.py", dest="new.py")
    events = []
    session._emit = events.append
    
    async def run():
        await rename_path_cmd(session, cmd)
    
    asyncio.run(run())
    assert len(events) == 1
    assert isinstance(events[0], ErrorOccurred)


def test_rename_path_empty_src_dest(session):
    asyncio.run(session.start())
    session._state.session_id = "test"
    cmd = RenamePath(src="", dest="")
    events = []
    session._emit = events.append
    
    async def run():
        await rename_path_cmd(session, cmd)
    
    asyncio.run(run())
    assert len(events) == 1
    assert isinstance(events[0], ErrorOccurred)


def test_delete_path_no_session(session):
    cmd = DeletePath(path="file.py")
    events = []
    session._emit = events.append
    
    async def run():
        await delete_path_cmd(session, cmd)
    
    asyncio.run(run())
    assert len(events) == 1
    assert isinstance(events[0], ErrorOccurred)


def test_delete_path_empty_path(session):
    asyncio.run(session.start())
    session._state.session_id = "test"
    cmd = DeletePath(path="  ")
    events = []
    session._emit = events.append
    
    async def run():
        await delete_path_cmd(session, cmd)
    
    asyncio.run(run())
    assert len(events) == 1
    assert isinstance(events[0], ErrorOccurred)
