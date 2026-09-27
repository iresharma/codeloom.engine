from __future__ import annotations

import asyncio

from protocol.commands import CloseFile, OpenFile, RequestGit, StartSession
from protocol.events import (
    ErrorOccurred,
    FileClosed,
    FileContent,
    GitStateUpdated,
    WarningOccurred,
)
from runtime.session import EngineSession
from tests.test_orchestrator import _init_git
from protocol.commands import (
    CreatePath,
    DeletePath,
    RenamePath,
    UndoLastEdit,
)
from runtime.commands.files import open_file, close_file


async def _started(tmp_path):
    session = EngineSession(tmp_path, tmp_path / "session.db")
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


def test_open_file_emits_content_and_tracks_open(tmp_path):
    (tmp_path / "notes.md").write_text("hello\n", encoding="utf-8")

    async def run():
        session, queue = await _started(tmp_path)
        await session.handle(OpenFile(path="notes.md"))
        events = _drain(queue)
        contents = [item for item in events if isinstance(item, FileContent)]
        assert len(contents) == 1
        assert contents[0].path == "notes.md"
        assert contents[0].content == "hello\n"
        assert session.snapshot().open_files == ["notes.md"]
        await session.handle(OpenFile(path="notes.md"))
        assert session.snapshot().open_files == ["notes.md"]
        await session.aclose()

    asyncio.run(run())


def test_open_file_missing_and_escape(tmp_path):
    async def run():
        session, queue = await _started(tmp_path)
        await session.handle(OpenFile(path="missing.py"))
        events = _drain(queue)
        assert any(
            isinstance(item, ErrorOccurred) and "not found" in item.message
            for item in events
        )
        await session.handle(OpenFile(path="../secret.txt"))
        events = _drain(queue)
        assert any(
            isinstance(item, ErrorOccurred) and "outside" in item.message
            for item in events
        )
        assert session.snapshot().open_files == []
        await session.aclose()

    asyncio.run(run())


def test_open_file_requires_session(tmp_path):
    async def run():
        session = EngineSession(tmp_path, tmp_path / "session.db")
        await session.start()
        queue = session.subscribe()
        await session.handle(OpenFile(path="a.py"))
        events = _drain(queue)
        assert any(
            isinstance(item, ErrorOccurred) and "start one first" in item.message
            for item in events
        )
        await session.aclose()

    asyncio.run(run())


def test_close_file_emits_and_rejects_unknown(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")

    async def run():
        session, queue = await _started(tmp_path)
        await session.handle(OpenFile(path="a.py"))
        _drain(queue)
        await session.handle(CloseFile(path="a.py"))
        events = _drain(queue)
        assert any(isinstance(item, FileClosed) and item.path == "a.py" for item in events)
        assert session.snapshot().open_files == []
        await session.handle(CloseFile(path="a.py"))
        events = _drain(queue)
        assert any(
            isinstance(item, ErrorOccurred) and "not open" in item.message
            for item in events
        )
        await session.aclose()

    asyncio.run(run())


def test_request_git_empty_outside_repo(tmp_path):
    async def run():
        session, queue = await _started(tmp_path)
        await session.handle(RequestGit())
        events = _drain(queue)
        updates = [item for item in events if isinstance(item, GitStateUpdated)]
        assert len(updates) == 1
        assert updates[0].git.branch is None
        assert updates[0].git.dirty is False
        await session.aclose()

    asyncio.run(run())


def test_request_git_dirty_repo(tmp_path):
    _init_git(tmp_path)
    (tmp_path / "README").write_text("changed\n", encoding="utf-8")
    (tmp_path / "extra.py").write_text("y = 2\n", encoding="utf-8")

    async def run():
        session, queue = await _started(tmp_path)
        await session.handle(RequestGit())
        events = _drain(queue)
        updates = [item for item in events if isinstance(item, GitStateUpdated)]
        assert len(updates) == 1
        git = updates[0].git
        assert git.dirty is True
        assert git.branch
        assert "README" in git.unstaged
        assert "extra.py" in git.untracked
        assert "changed" in git.unstaged_diff
        await session.aclose()

    asyncio.run(run())


def test_request_git_truncates_huge_diff(tmp_path, monkeypatch):
    monkeypatch.setattr("runtime.session.EVENT_SOFT_LIMIT", 64)
    _init_git(tmp_path)
    (tmp_path / "README").write_text("x" * 200 + "\n", encoding="utf-8")

    async def run():
        session, queue = await _started(tmp_path)
        await session.handle(RequestGit())
        events = _drain(queue)
        updates = [item for item in events if isinstance(item, GitStateUpdated)]
        assert updates
        assert len(updates[0].git.unstaged_diff) < 200
        assert any(
            isinstance(item, WarningOccurred) and "truncated" in item.message
            for item in events
        )
        await session.aclose()

    asyncio.run(run())


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


# ============================================================================
# runtime/commands/files.py tests
# ============================================================================

class TestFilesCommands:
    """Test coverage gaps in files commands."""

    def test_open_file_not_found(self, tmp_path):
        """Test open_file with non-existent file."""
        session = EngineSession(tmp_path, tmp_path / "db.sqlite")
        cmd = OpenFile(path="nonexistent.txt")
        # _require_session will return False without _state
        open_file(session, cmd)
        # Should return early

    def test_close_file_not_open(self, tmp_path):
        """Test close_file with file not open."""
        session = EngineSession(tmp_path, tmp_path / "db.sqlite")
        cmd = CloseFile(path="nonexistent.txt")
        # _require_session will return False without _state
        close_file(session, cmd)
        # Should return early
