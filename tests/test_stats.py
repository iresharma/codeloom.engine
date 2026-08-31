from __future__ import annotations

import asyncio

from llm.provider import LLMResult, Usage
from protocol.commands import StartSession
from protocol.events import StatsUpdated
from protocol.snapshot import EngineSnapshot, GitState, Stats
from runtime.session import EngineSession
from runtime.store.sqlite import load, save
from tests.fakes import FakeProvider


def test_usage_add():
    left = Usage(prompt_tokens=1, completion_tokens=2, total_tokens=3, cost=0.1, requests=1)
    right = Usage(prompt_tokens=4, completion_tokens=5, total_tokens=9, cost=0.2, requests=1)
    total = left + right
    assert total.prompt_tokens == 5
    assert total.requests == 2
    assert abs(total.cost - 0.3) < 1e-9


def test_stats_accumulate(tmp_path):
    async def run():
        provider = FakeProvider(
            results=[
                LLMResult(
                    text="a",
                    usage=Usage(prompt_tokens=2, completion_tokens=3, total_tokens=5, requests=1),
                ),
            ]
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
        updates = []
        while not queue.empty():
            item = queue.get_nowait()
            if isinstance(item, StatsUpdated):
                updates.append(item)
        assert len(updates) >= 2
        assert session._state.stats.requests >= 1
        assert session._state.stats.elapsed_s >= 0

    asyncio.run(run())


def test_stats_round_trip_sqlite(tmp_path):
    snap = EngineSnapshot(
        session_id="s1",
        workspace=str(tmp_path),
        messages=[],
        ended=False,
        open_files=[],
        file_tree=[],
        git=GitState.empty(),
        stats=Stats(prompt_tokens=9, cost=0.5, requests=2),
    )
    db = tmp_path / "session.db"
    from runtime.store.sqlite import init

    init(db)
    save(db, snap)
    loaded = load(db, "s1")
    assert loaded.stats.prompt_tokens == 9
    assert loaded.stats.requests == 2


def test_missing_stats_defaults():
    snap = EngineSnapshot.from_json(
        {
            "session_id": "s",
            "workspace": "/tmp",
            "messages": [],
            "ended": False,
            "open_files": [],
            "file_tree": [],
            "git": {},
        }
    )
    assert snap.stats.prompt_tokens == 0
    assert snap.pending_prompt is None
