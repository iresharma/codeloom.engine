"""Session shortcuts: exact meta phrases (undo, what changed, list edits)
skip the orchestrator. Everything else, including locate-shaped questions,
goes to the orch. No JEV intent_route call.
"""

from __future__ import annotations

import asyncio

from protocol.events import ToolCallStarted
from runtime.config import EngineConfig
from tests.conftest import FakeJudge, FakeVerdict
from tests.fakes import FakeProvider
from tests.test_orchestrator import _bind, _wait_idle


async def _bound(tmp_path, provider=None, **config_kw):
    session = await _bind(tmp_path, provider or FakeProvider(), **config_kw)()
    return session


def test_default_route_is_unchanged_when_judge_disabled(tmp_path):
    async def run():
        session = await _bound(tmp_path)
        session.start_turn("hello there")
        await _wait_idle(session)
        added = [
            item
            for item in session._state.messages
            if item.role == "assistant"
        ]
        assert added  # the normal loop ran and produced a reply

    asyncio.run(run())


def test_meta_undo_dispatches_without_a_judge_call(tmp_path):
    async def run():
        session = await _bound(tmp_path, judge_mode="enforcing")
        judge = FakeJudge()
        judge.responses["intent_route"] = FakeVerdict(
            choices={"intent": "meta", "meta_action": "undo"},
            confidences={"intent": 0.95, "meta_action": 0.9},
        )
        session._judge = judge
        session._config = EngineConfig(judge_mode="enforcing", max_turns=8)

        session.start_turn("undo")
        await _wait_idle(session)

        assert judge.calls == []
        replies = [m for m in session._state.messages if m.role == "assistant"]
        assert replies
        assert "no edits" in replies[-1].text.lower() or "error" in replies[-1].text.lower() or "undone" in replies[-1].text.lower()
        from runtime.store.judgements import recent

        rows = recent(tmp_path / "session.db")
        assert not any(row.tag == "intent_route" for row in rows)

    asyncio.run(run())


def test_non_exact_phrase_goes_to_orchestrator(tmp_path):
    async def run():
        session = await _bound(tmp_path, judge_mode="enforcing")
        judge = FakeJudge()
        judge.responses["intent_route"] = FakeVerdict(
            choices={"intent": "meta", "meta_action": "undo"},
            confidences={"intent": 0.95, "meta_action": 0.9},
        )
        session._judge = judge
        session._config = EngineConfig(judge_mode="enforcing", max_turns=8)

        session.start_turn("undo that")
        await _wait_idle(session)

        assert judge.calls == []
        replies = [m for m in session._state.messages if m.role == "assistant"]
        assert replies
        assert replies[-1].text == "done"

    asyncio.run(run())


def test_locate_question_does_not_call_intent_route(tmp_path):
    async def run():
        session = await _bound(tmp_path, judge_mode="enforcing")
        judge = FakeJudge()
        judge.responses["intent_route"] = FakeVerdict(
            choices={"intent": "locate"},
            confidences={"intent": 0.9},
            nouls={"answers_message": 0.9},
        )
        session._judge = judge
        session._config = EngineConfig(judge_mode="enforcing", max_turns=8)

        session.start_turn("where is the retry logic?")
        await _wait_idle(session)

        assert judge.calls == []
        replies = [m for m in session._state.messages if m.role == "assistant"]
        assert replies
        assert replies[-1].text == "done"

    asyncio.run(run())


def test_is_inbox_report_detects_agent_and_worktree_handoffs():
    from runtime.session import _is_inbox_report

    assert _is_inbox_report("[agent ask ed8e4551 finished]\nsome report text")
    assert _is_inbox_report("  [worktree coder b03318d3 pr]\nopened pull request")
    assert not _is_inbox_report("where is the retry logic?")
    assert not _is_inbox_report("[urgent] please fix this bug")


def test_inbox_report_never_gets_intent_routed(tmp_path):
    async def run():
        session = await _bound(tmp_path, judge_mode="enforcing")
        judge = FakeJudge()
        judge.responses["intent_route"] = FakeVerdict(
            choices={"intent": "locate"},
            confidences={"intent": 0.9},
            nouls={"answers_message": 0.9},
        )
        session._judge = judge
        session._config = EngineConfig(judge_mode="enforcing", max_turns=8)

        queue = session.subscribe()
        session.start_turn(
            "[agent ask ed8e4551 finished]\nwhere is the retry logic?"
        )
        await _wait_idle(session)

        assert judge.calls == []
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        tool_starts = [e for e in events if isinstance(e, ToolCallStarted)]
        assert not any(e.name == "search" for e in tool_starts)

    asyncio.run(run())
