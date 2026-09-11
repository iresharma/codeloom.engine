"""Comprehensive coverage for runtime/commands/files.py"""
from __future__ import annotations

import asyncio
from pathlib import Path

from protocol.commands import (
    CloseFile,
    CreatePath,
    DeletePath,
    OpenFile,
    RenamePath,
    StartSession,
    UndoLastEdit,
)
from protocol.events import (
    ErrorOccurred,
    FileClosed,
    FileContent,
    PathChanged,
)
from runtime.session import EngineSession
from runtime.store.edits import ensure_schema


async def _started(tmp_path):
    """Fixture: create started session."""
    db = tmp_path / "session.db"
    ensure_schema(db)
    session = EngineSession(tmp_path, db)
    await session.start()
    queue = session.subscribe()
    await session.handle(StartSession(workspace=str(tmp_path)))
    while not queue.empty():
        queue.get_nowait()
    return session, queue


def _drain(queue):
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


def test_open_file_success(tmp_path):
    """OpenFile reads and tracks file."""
    (tmp_path / "test.py").write_text("x = 1\n")
    
    async def run():
        session, queue = await _started(tmp_path)
        await session.handle(OpenFile(path="test.py"))
        events = _drain(queue)
        await session.aclose()
        return events
    
    events = asyncio.run(run())
    assert any(isinstance(e, FileContent) and e.path == "test.py" for e in events)


def test_open_file_missing(tmp_path):
    """OpenFile errors on missing file."""
    async def run():
        session, queue = await _started(tmp_path)
        await session.handle(OpenFile(path="missing.py"))
        events = _drain(queue)
        await session.aclose()
        return events
    
    events = asyncio.run(run())
    assert any(isinstance(e, ErrorOccurred) and "not found" in e.message for e in events)


def test_open_file_escape_attempt(tmp_path):
    """OpenFile rejects escape outside workspace."""
    async def run():
        session, queue = await _started(tmp_path)
        await session.handle(OpenFile(path="../secret.txt"))
        events = _drain(queue)
        await session.aclose()
        return events
    
    events = asyncio.run(run())
    assert any(isinstance(e, ErrorOccurred) and "outside" in e.message for e in events)


def test_open_file_duplicate(tmp_path):
    """OpenFile twice on same file is idempotent."""
    (tmp_path / "a.py").write_text("x = 1\n")
    
    async def run():
        session, queue = await _started(tmp_path)
        await session.handle(OpenFile(path="a.py"))
        _drain(queue)
        await session.handle(OpenFile(path="a.py"))
        _drain(queue)
        open_files = session.snapshot().open_files
        await session.aclose()
        return open_files

    files = asyncio.run(run())
    assert files.count("a.py") == 1


def test_open_file_no_session(tmp_path):
    """OpenFile requires active session."""
    async def run():
        session = EngineSession(tmp_path, tmp_path / "session.db")
        await session.start()
        queue = session.subscribe()
        await session.handle(OpenFile(path="test.py"))
        events = _drain(queue)
        await session.aclose()
        return events
    
    events = asyncio.run(run())
    assert any(isinstance(e, ErrorOccurred) for e in events)


def test_close_file_success(tmp_path):
    """CloseFile removes tracking."""
    (tmp_path / "a.py").write_text("x = 1\n")
    
    async def run():
        session, queue = await _started(tmp_path)
        await session.handle(OpenFile(path="a.py"))
        _drain(queue)
        await session.handle(CloseFile(path="a.py"))
        events = _drain(queue)
        open_files = session.snapshot().open_files
        await session.aclose()
        return events, open_files
    
    events, files = asyncio.run(run())
    assert any(isinstance(e, FileClosed) and e.path == "a.py" for e in events)
    assert "a.py" not in files


def test_close_file_not_open(tmp_path):
    """CloseFile errors if file not open."""
    async def run():
        session, queue = await _started(tmp_path)
        await session.handle(CloseFile(path="notopen.py"))
        events = _drain(queue)
        await session.aclose()
        return events
    
    events = asyncio.run(run())
    assert any(isinstance(e, ErrorOccurred) and "not open" in e.message for e in events)


def test_close_file_escape_attempt(tmp_path):
    """CloseFile rejects escape."""
    async def run():
        session, queue = await _started(tmp_path)
        await session.handle(CloseFile(path="../secret.txt"))
        events = _drain(queue)
        await session.aclose()
        return events
    
    events = asyncio.run(run())
    assert any(isinstance(e, ErrorOccurred) and "outside" in e.message for e in events)


def test_close_file_no_session(tmp_path):
    """CloseFile requires active session."""
    async def run():
        session = EngineSession(tmp_path, tmp_path / "session.db")
        await session.start()
        queue = session.subscribe()
        await session.handle(CloseFile(path="test.py"))
        events = _drain(queue)
        await session.aclose()
        return events
    
    events = asyncio.run(run())
    assert any(isinstance(e, ErrorOccurred) for e in events)


def test_create_path_file(tmp_path):
    """CreatePath creates a new file."""
    async def run():
        session, queue = await _started(tmp_path)
        await session.handle(CreatePath(path="new.py", is_dir=False, content="x = 1\n"))
        events = _drain(queue)
        await session.aclose()
        return events, (tmp_path / "new.py").exists()
    
    events, exists = asyncio.run(run())
    assert exists


def test_create_path_dir(tmp_path):
    """CreatePath creates directory."""
    async def run():
        session, queue = await _started(tmp_path)
        await session.handle(CreatePath(path="newdir", is_dir=True))
        events = _drain(queue)
        await session.aclose()
        return events, (tmp_path / "newdir").is_dir()
    
    events, is_dir = asyncio.run(run())
    assert is_dir


def test_create_path_empty_path(tmp_path):
    """CreatePath rejects empty path."""
    async def run():
        session, queue = await _started(tmp_path)
        await session.handle(CreatePath(path="", is_dir=False))
        events = _drain(queue)
        await session.aclose()
        return events
    
    events = asyncio.run(run())
    assert any(isinstance(e, ErrorOccurred) and "required" in e.message for e in events)


def test_create_path_no_session(tmp_path):
    """CreatePath requires active session."""
    async def run():
        session = EngineSession(tmp_path, tmp_path / "session.db")
        await session.start()
        queue = session.subscribe()
        await session.handle(CreatePath(path="new.py", is_dir=False))
        events = _drain(queue)
        await session.aclose()
        return events
    
    events = asyncio.run(run())
    assert any(isinstance(e, ErrorOccurred) for e in events)


def test_rename_path_success(tmp_path):
    """RenamePath renames file."""
    (tmp_path / "old.py").write_text("x = 1\n")
    
    async def run():
        session, queue = await _started(tmp_path)
        await session.handle(RenamePath(src="old.py", dest="new.py"))
        events = _drain(queue)
        await session.aclose()
        return events, (tmp_path / "new.py").exists(), (tmp_path / "old.py").exists()
    
    events, new_exists, old_exists = asyncio.run(run())
    assert new_exists and not old_exists


def test_rename_path_with_open_file(tmp_path):
    """RenamePath updates open_files list."""
    (tmp_path / "old.py").write_text("x = 1\n")
    
    async def run():
        session, queue = await _started(tmp_path)
        await session.handle(OpenFile(path="old.py"))
        _drain(queue)
        await session.handle(RenamePath(src="old.py", dest="new.py"))
        _drain(queue)
        open_files = session.snapshot().open_files
        await session.aclose()
        return open_files
    
    files = asyncio.run(run())
    assert "new.py" in files and "old.py" not in files


def test_rename_path_empty_paths(tmp_path):
    """RenamePath rejects empty src/dest."""
    async def run():
        session, queue = await _started(tmp_path)
        await session.handle(RenamePath(src="", dest="new.py"))
        events = _drain(queue)
        await session.aclose()
        return events
    
    events = asyncio.run(run())
    assert any(isinstance(e, ErrorOccurred) and "required" in e.message for e in events)


def test_rename_path_no_session(tmp_path):
    """RenamePath requires active session."""
    async def run():
        session = EngineSession(tmp_path, tmp_path / "session.db")
        await session.start()
        queue = session.subscribe()
        await session.handle(RenamePath(src="old.py", dest="new.py"))
        events = _drain(queue)
        await session.aclose()
        return events
    
    events = asyncio.run(run())
    assert any(isinstance(e, ErrorOccurred) for e in events)


def test_delete_path_success(tmp_path):
    """DeletePath removes file."""
    (tmp_path / "temp.py").write_text("x = 1\n")
    
    async def run():
        session, queue = await _started(tmp_path)
        await session.handle(DeletePath(path="temp.py"))
        events = _drain(queue)
        await session.aclose()
        return events, (tmp_path / "temp.py").exists()
    
    events, exists = asyncio.run(run())
    assert not exists


def test_delete_path_with_open_file(tmp_path):
    """DeletePath removes from open_files list."""
    (tmp_path / "temp.py").write_text("x = 1\n")
    
    async def run():
        session, queue = await _started(tmp_path)
        await session.handle(OpenFile(path="temp.py"))
        _drain(queue)
        await session.handle(DeletePath(path="temp.py"))
        _drain(queue)
        open_files = session.snapshot().open_files
        await session.aclose()
        return open_files
    
    files = asyncio.run(run())
    assert "temp.py" not in files


def test_delete_path_empty_path(tmp_path):
    """DeletePath rejects empty path."""
    async def run():
        session, queue = await _started(tmp_path)
        await session.handle(DeletePath(path=""))
        events = _drain(queue)
        await session.aclose()
        return events
    
    events = asyncio.run(run())
    assert any(isinstance(e, ErrorOccurred) and "required" in e.message for e in events)


def test_delete_path_no_session(tmp_path):
    """DeletePath requires active session."""
    async def run():
        session = EngineSession(tmp_path, tmp_path / "session.db")
        await session.start()
        queue = session.subscribe()
        await session.handle(DeletePath(path="temp.py"))
        events = _drain(queue)
        await session.aclose()
        return events
    
    events = asyncio.run(run())
    assert any(isinstance(e, ErrorOccurred) for e in events)


def test_undo_last_edit_no_session(tmp_path):
    """UndoLastEdit requires active session."""
    async def run():
        session = EngineSession(tmp_path, tmp_path / "session.db")
        await session.start()
        queue = session.subscribe()
        await session.handle(UndoLastEdit())
        events = _drain(queue)
        await session.aclose()
        return events
    
    events = asyncio.run(run())
    assert any(isinstance(e, ErrorOccurred) for e in events)
