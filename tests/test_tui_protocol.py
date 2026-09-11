from __future__ import annotations

import asyncio

from protocol.commands import (
    CreatePath,
    DeletePath,
    OpenFile,
    RenamePath,
    RequestAgentTranscript,
    RequestContext,
    RequestMemory,
    StartSession,
)
from protocol.events import (
    ChatHistoryAdded,
    ChatHistoryComplete,
    ContextBreakdown,
    FileContent,
    GitStateUpdated,
    MemoryUpdated,
    PathChanged,
)
from runtime.session import EngineSession
from runtime.store.memory import remember
from tests.fakes import FakeProvider
from tests.test_orchestrator import _AskThenDone, _bind, _queued, _wait_idle


def test_path_commands_and_git(tmp_path):
    async def run():
        session = EngineSession(tmp_path, tmp_path / "session.db")
        await session.start()
        queue = session.subscribe()
        await session.handle(StartSession(workspace=str(tmp_path)))
        while not queue.empty():
            queue.get_nowait()
        await session.handle(CreatePath(path="pkg", is_dir=True))
        await session.handle(CreatePath(path="pkg/a.py", content="x = 1\n"))
        await session.handle(RenamePath(src="pkg/a.py", dest="pkg/b.py"))
        await session.handle(OpenFile(path="pkg/b.py"))
        await session.handle(DeletePath(path="pkg/b.py"))
        events = list(_queued(queue))
        assert any(isinstance(item, PathChanged) and item.action == "mkdir" for item in events)
        contents = [item for item in events if isinstance(item, FileContent)]
        assert contents and contents[0].content == "x = 1\n"
        assert any(isinstance(item, GitStateUpdated) for item in events)

    asyncio.run(run())


def test_request_context_sections(tmp_path):
    async def run():
        session = await _bind(tmp_path, FakeProvider())()
        queue = session.subscribe()
        while not queue.empty():
            queue.get_nowait()
        await session.handle(RequestContext())
        events = [item for item in _queued(queue) if isinstance(item, ContextBreakdown)]
        assert events
        names = [section.name for section in events[0].sections]
        assert names == ["system", "memory", "skills", "tools", "messages"]
        assert events[0].budget > 0
        tools = next(section for section in events[0].sections if section.name == "tools")
        assert "ask" in tools.text or "function" in tools.text

    asyncio.run(run())


def test_memory_updated_on_remember(tmp_path):
    async def run():
        (tmp_path / "a.py").write_text("print(1)\n")
        session = EngineSession(tmp_path, tmp_path / "session.db")
        await session.start()
        queue = session.subscribe()
        await session.handle(StartSession(workspace=str(tmp_path)))
        while not queue.empty():
            queue.get_nowait()
        remember(tmp_path, "engineering", "use the funnel")
        session._emit_memory()
        events = [item for item in _queued(queue) if isinstance(item, MemoryUpdated)]
        assert events
        assert events[0].engineering[0].text == "use the funnel"
        await session.handle(RequestMemory())
        again = [item for item in _queued(queue) if isinstance(item, MemoryUpdated)]
        assert again

    asyncio.run(run())


def test_agent_transcript_replay(tmp_path):
    async def run():
        provider = _AskThenDone()
        session = await _bind(tmp_path, provider)()
        queue = session.subscribe()
        while not queue.empty():
            queue.get_nowait()
        session.start_turn("where is retry?")
        await _wait_idle(session, timeout=8.0)
        orch = session._loop
        agent_id = next(iter(orch._finished_transcripts), None)
        if agent_id is None and orch._children:
            agent_id = next(iter(orch._children))
        assert agent_id
        await session.handle(RequestAgentTranscript(agent_id=agent_id))
        events = _queued(queue)
        added = [
            item
            for item in events
            if isinstance(item, ChatHistoryAdded) and item.agent_id == agent_id
        ]
        complete = [
            item
            for item in events
            if isinstance(item, ChatHistoryComplete) and item.agent_id == agent_id
        ]
        assert complete
        await session.handle(RequestContext(agent_id=agent_id))
        breakdowns = [item for item in _queued(queue) if isinstance(item, ContextBreakdown)]
        assert breakdowns
        assert breakdowns[-1].agent_id == agent_id

    asyncio.run(run())
