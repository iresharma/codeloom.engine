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
