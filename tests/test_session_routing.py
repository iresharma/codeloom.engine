"""Phase 5 (docs/impl-plans/jev-exp-1.md) end-to-end: EngineSession._maybe_route_turn
wired into a real turn, driven through session.start_turn like a live client would.
"""

from __future__ import annotations

import asyncio
import shutil

import pytest

from protocol.events import ToolCallFinished, ToolCallStarted
from runtime.config import EngineConfig
from tests.conftest import FakeJudge, FakeVerdict
from tests.fakes import FakeProvider
from tests.test_orchestrator import _bind, _wait_idle

# The locate-resolution test below shells out to real ripgrep. See the same
# guard in tests/test_resolver.py.
needs_rg = pytest.mark.skipif(shutil.which("rg") is None, reason="rg (ripgrep) is not installed")


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


def test_meta_undo_dispatches_without_spawning_the_loop(tmp_path):
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

        assert judge.calls  # intent classification actually ran
        replies = [m for m in session._state.messages if m.role == "assistant"]
        assert replies
        assert "no edits" in replies[-1].text.lower() or "error" in replies[-1].text.lower() or "undone" in replies[-1].text.lower()

    asyncio.run(run())


def test_meta_advisory_mode_does_not_change_behavior(tmp_path):
    async def run():
        session = await _bound(tmp_path, judge_mode="advisory")
        judge = FakeJudge()
        judge.responses["intent_route"] = FakeVerdict(
            choices={"intent": "meta", "meta_action": "undo"},
            confidences={"intent": 0.95, "meta_action": 0.9},
        )
        session._judge = judge
        session._config = EngineConfig(judge_mode="advisory", max_turns=8)

        session.start_turn("undo that")
        await _wait_idle(session)

        # Advisory: the judge was consulted, but the normal loop still ran
        # (FakeProvider's default reply is "done", not the deterministic
        # undo text), matching invariant 3's fall-through-safe behaviour.
        replies = [m for m in session._state.messages if m.role == "assistant"]
        assert replies
        assert replies[-1].text == "done"

    asyncio.run(run())


@needs_rg
def test_locate_resolution_seeds_context_and_skips_exploration(tmp_path):
    async def run():
        (tmp_path / "retry.py").write_text(
            "\n".join(f"# line {i}" for i in range(30))
            + "\ndef retry_logic():\n    return backoff()\n"
        )
        (tmp_path / "unrelated.py").write_text("def totally_unrelated():\n    pass\n")
        (tmp_path / "notes.md").write_text(
            "retry backoff retry backoff retry backoff retry\n" * 5
        )
        session = await _bound(tmp_path, judge_mode="enforcing")
        judge = FakeJudge()
        judge.responses["intent_route"] = FakeVerdict(
            choices={"intent": "locate", "meta_action": "other"},
            confidences={"intent": 0.9},
        )
        judge.responses["search_rerank"] = FakeVerdict(
            probabilities={"best_match": {"1": 0.9}}, confidences={"best_match": 0.9}
        )
        session._judge = judge
        session._config = EngineConfig(judge_mode="enforcing", max_turns=8)
        # The second "intent_route"-tagged call in the pipeline is the
        # resolver's answers-check; FakeJudge only keys by tag, so both the
        # intent classification and the answers-check share this response
        # -- give it an is_ambiguous-safe, answers_message-true verdict.
        judge.responses["intent_route"] = FakeVerdict(
            choices={"intent": "locate"},
            confidences={"intent": 0.9},
            nouls={"answers_message": 0.9},
        )

        queue = session.subscribe()
        session.start_turn("where is the retry logic?")
        await _wait_idle(session)
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        tool_starts = [e for e in events if isinstance(e, ToolCallStarted)]
        tool_finishes = [e for e in events if isinstance(e, ToolCallFinished)]
        assert any(e.name == "search" for e in tool_starts)
        assert any(e.name == "search" for e in tool_finishes)
        replies = [m for m in session._state.messages if m.role == "assistant"]
        assert replies

    asyncio.run(run())


def test_is_inbox_report_detects_agent_and_worktree_handoffs():
    from runtime.session import _is_inbox_report

    assert _is_inbox_report("[agent ask ed8e4551 finished]\nsome report text")
    assert _is_inbox_report("  [worktree coder b03318d3 pr]\nopened pull request")
    assert not _is_inbox_report("where is the retry logic?")
    assert not _is_inbox_report("[urgent] please fix this bug")


@needs_rg
def test_inbox_report_never_gets_intent_routed_or_locate_resolved(tmp_path):
    """Regression: an "[agent ... finished]" handoff that a real TypeSafe
    call would classify as "locate" must never reach the resolver -- it
    injects search/read_file tool-call history into the *orchestrator's*
    own context, whose tool schema never included those tools, which
    confuses the model into stalling the turn instead of spawning a coder."""

    async def run():
        (tmp_path / "retry.py").write_text("def retry_logic():\n    return backoff()\n")
        session = await _bound(tmp_path, judge_mode="enforcing")
        judge = FakeJudge()
        judge.responses["intent_route"] = FakeVerdict(
            choices={"intent": "locate"},
            confidences={"intent": 0.9},
            nouls={"answers_message": 0.9},
        )
        judge.responses["search_rerank"] = FakeVerdict(
            probabilities={"best_match": {"1": 0.9}}, confidences={"best_match": 0.9}
        )
        session._judge = judge
        session._config = EngineConfig(judge_mode="enforcing", max_turns=8)

        queue = session.subscribe()
        session.start_turn(
            "[agent ask ed8e4551 finished]\nwhere is the retry logic?"
        )
        await _wait_idle(session)

        assert judge.calls == []  # intent classification itself never ran
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        tool_starts = [e for e in events if isinstance(e, ToolCallStarted)]
        assert not any(e.name == "search" for e in tool_starts)

    asyncio.run(run())
