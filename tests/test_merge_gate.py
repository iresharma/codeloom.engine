"""Phase 8, scoped to the merge gate (docs/impl-plans/jev-exp-1.md):
scoring a subagent's result before it re-enters the parent's context.

Exercised directly against Orchestrator._apply_merge_gate on a session
bound the same way tests/test_orchestrator.py's other tests bind one --
full dispatch/selection/ordering is explicitly out of scope, see the
module docstring in runtime/judge_decisions.py's Phase 8 section.
"""

from __future__ import annotations

import asyncio

from agents.compactor import AgentResult
from tests.conftest import FakeJudge, FakeVerdict
from tests.fakes import FakeProvider
from tests.test_orchestrator import _bind


async def _orch(tmp_path, judge=None, **config_kw):
    session = await _bind(tmp_path, FakeProvider(), **config_kw)()
    if judge is not None:
        session._loop._ctx.judge = judge
    judgements: list[dict] = []
    session._loop._ctx.on_judgement = lambda **kw: judgements.append(kw)
    session._loop.judgements = judgements
    return session._loop


def _result(**overrides) -> AgentResult:
    defaults = {
        "status": "ok",
        "summary": "found retry logic in server.py:42",
        "outcome": "found retry logic in server.py:42, uses exponential backoff",
        "files_touched": ["server.py"],
    }
    defaults.update(overrides)
    return AgentResult(**defaults)


def test_none_verdict_returns_full_text_unchanged(tmp_path):
    async def run():
        orch = await _orch(tmp_path, judge=FakeJudge(), judge_mode="enforcing")
        result = _result()
        text = await orch._apply_merge_gate("ask", "where is retry?", result)
        assert text == result.as_text()

    asyncio.run(run())


def test_disabled_judge_returns_full_text_unchanged(tmp_path):
    async def run():
        orch = await _orch(tmp_path, judge=None)
        result = _result()
        text = await orch._apply_merge_gate("ask", "where is retry?", result)
        assert text == result.as_text()

    asyncio.run(run())


def test_low_worth_parent_context_shrinks_to_one_line(tmp_path):
    async def run():
        judge = FakeJudge()
        judge.responses["merge_gate"] = FakeVerdict(
            nouls={"accomplished_its_brief": 0.9}, scores={"worth_parent_context": 0.0}
        )
        orch = await _orch(tmp_path, judge=judge, judge_mode="enforcing")
        result = _result()

        text = await orch._apply_merge_gate("ask", "where is retry?", result)

        assert text != result.as_text()
        assert result.summary in text
        assert "files_touched" not in text  # trimmed out at one-line level

    asyncio.run(run())


def test_medium_worth_parent_context_gives_short_summary(tmp_path):
    async def run():
        judge = FakeJudge()
        judge.responses["merge_gate"] = FakeVerdict(
            nouls={"accomplished_its_brief": 0.9}, scores={"worth_parent_context": 1.0}
        )
        orch = await _orch(tmp_path, judge=judge, judge_mode="enforcing")
        result = _result()

        text = await orch._apply_merge_gate("ask", "where is retry?", result)

        assert result.summary in text
        assert result.outcome in text

    asyncio.run(run())


def test_high_worth_parent_context_keeps_full_result(tmp_path):
    async def run():
        judge = FakeJudge()
        judge.responses["merge_gate"] = FakeVerdict(
            nouls={"accomplished_its_brief": 0.9}, scores={"worth_parent_context": 2.0}
        )
        orch = await _orch(tmp_path, judge=judge, judge_mode="enforcing")
        result = _result()

        text = await orch._apply_merge_gate("ask", "where is retry?", result)

        assert text == result.as_text()

    asyncio.run(run())


def test_contradiction_forces_full_admission_even_at_low_worth(tmp_path):
    async def run():
        judge = FakeJudge()
        judge.responses["merge_gate"] = FakeVerdict(
            nouls={"accomplished_its_brief": 0.9, "contradicts_siblings": 0.9},
            scores={"worth_parent_context": 0.0},  # would otherwise shrink to one line
        )
        orch = await _orch(tmp_path, judge=judge, judge_mode="enforcing")
        result = _result()

        text = await orch._apply_merge_gate("ask", "where is retry?", result)

        assert text == result.as_text()  # contradiction wins -> full text kept
        assert orch.judgements[0]["outcome"] == "contradicts_siblings"

    asyncio.run(run())


def test_advisory_mode_logs_but_never_shrinks_the_result(tmp_path):
    async def run():
        judge = FakeJudge()
        judge.responses["merge_gate"] = FakeVerdict(
            nouls={"accomplished_its_brief": 0.1}, scores={"worth_parent_context": 0.0}
        )
        orch = await _orch(tmp_path, judge=judge, judge_mode="advisory")
        result = _result()

        text = await orch._apply_merge_gate("ask", "where is retry?", result)

        assert text == result.as_text()
        assert orch.judgements
        assert orch.judgements[0]["enforced"] is False


def test_sibling_summaries_are_passed_to_the_judge_call(tmp_path):
    async def run():
        judge = FakeJudge()
        judge.responses["merge_gate"] = FakeVerdict(scores={"worth_parent_context": 2.0})
        orch = await _orch(tmp_path, judge=judge, judge_mode="enforcing")
        orch._recent_results = [("coder", "renamed foo to bar across 3 files")]
        result = _result()

        await orch._apply_merge_gate("reviewer", "review the rename", result)

        call = judge.calls[0]
        assert "renamed foo to bar across 3 files" in call["state"]["sibling_summaries"]

    asyncio.run(run())
