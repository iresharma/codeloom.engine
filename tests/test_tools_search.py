"""Phase 3 (search re-ranking) from docs/impl-plans/jev-exp-1.md, exercised
against tools/search.py's run_command-style wrapper around ripgrep."""

from __future__ import annotations

import asyncio

from runtime.config import EngineConfig
from tests.conftest import FakeJudge, FakeVerdict
import tools.search as tools_search


def _candidates(n: int) -> list[str]:
    return [f"file{i}.py:{i}:line contents {i}" for i in range(1, n + 1)]


def _ctx_with(ctx, judge=None, judge_mode="enforcing", user_request="where is the retry logic?"):
    ctx.config = EngineConfig(judge_mode=judge_mode)
    ctx.judge = judge
    ctx.user_request = user_request
    ctx.judgements = []
    ctx.on_judgement = lambda **kw: ctx.judgements.append(kw)
    return ctx


def test_no_matches(ctx, monkeypatch):
    monkeypatch.setattr(tools_search, "search_candidates", lambda *a, **k: [])
    result = asyncio.run(tools_search.search(ctx, "retry"))
    assert result == "(no matches)"


def test_below_rerank_floor_skips_judge_and_keeps_rg_order(ctx, monkeypatch):
    candidates = _candidates(5)  # below SEARCH_RERANK_FLOOR (10)
    monkeypatch.setattr(tools_search, "search_candidates", lambda *a, **k: candidates)
    judge = FakeJudge()
    _ctx_with(ctx, judge=judge)

    result = asyncio.run(tools_search.search(ctx, "retry"))

    assert result == "\n".join(candidates)
    assert judge.calls == []


def test_no_judge_keeps_rg_order_above_floor(ctx, monkeypatch):
    candidates = _candidates(20)
    monkeypatch.setattr(tools_search, "search_candidates", lambda *a, **k: candidates)
    _ctx_with(ctx, judge=None)

    result = asyncio.run(tools_search.search(ctx, "retry"))

    assert result.startswith(candidates[0])


def test_none_verdict_keeps_rg_order(ctx, monkeypatch):
    candidates = _candidates(20)
    monkeypatch.setattr(tools_search, "search_candidates", lambda *a, **k: candidates)
    judge = FakeJudge()  # no scripted response -> ask() returns None
    _ctx_with(ctx, judge=judge)

    result = asyncio.run(tools_search.search(ctx, "retry"))

    assert result.split("\n")[0] == candidates[0]
    assert judge.calls  # judge was consulted
    assert ctx.judgements == []


def test_enforced_rerank_reorders_and_shrinks_to_top_n(ctx, monkeypatch):
    candidates = _candidates(20)
    monkeypatch.setattr(tools_search, "search_candidates", lambda *a, **k: candidates)
    judge = FakeJudge()
    # Candidate "5" (1-indexed str key) scores highest -> should come first.
    judge.responses["search_rerank"] = FakeVerdict(
        probabilities={"best_match": {"5": 0.9, "1": 0.05, "2": 0.05}},
        confidences={"best_match": 0.9},
    )
    _ctx_with(ctx, judge=judge)

    result = asyncio.run(tools_search.search(ctx, "retry"))

    lines = result.split("\n")
    assert lines[0] == candidates[4]  # the "5" candidate, now ranked first
    assert ctx.judgements[0]["outcome"] == "ranked"
    assert ctx.judgements[0]["enforced"] is True


def test_advisory_rerank_logs_but_keeps_rg_order(ctx, monkeypatch):
    candidates = _candidates(20)
    monkeypatch.setattr(tools_search, "search_candidates", lambda *a, **k: candidates)
    judge = FakeJudge()
    judge.responses["search_rerank"] = FakeVerdict(
        probabilities={"best_match": {"5": 0.9}},
        confidences={"best_match": 0.9},
    )
    _ctx_with(ctx, judge=judge, judge_mode="advisory")

    result = asyncio.run(tools_search.search(ctx, "retry"))

    assert result.split("\n")[0] == candidates[0]  # unchanged order
    assert ctx.judgements[0]["outcome"] == "ranked"
    assert ctx.judgements[0]["enforced"] is False


def test_rerank_never_drops_a_candidate(ctx, monkeypatch):
    candidates = _candidates(20)
    monkeypatch.setattr(tools_search, "search_candidates", lambda *a, **k: candidates)
    judge = FakeJudge()
    judge.responses["search_rerank"] = FakeVerdict(
        probabilities={"best_match": {"1": 0.5}},
        confidences={"best_match": 0.5},
    )
    _ctx_with(ctx, judge=judge)

    result = asyncio.run(tools_search.search(ctx, "retry", max_matches=200))

    # top_n caps the *displayed* set, but the suppressed count still
    # reflects every candidate ripgrep found, not a silently shrunk total.
    assert "more matches" in result
    omitted = int(result.rsplit("(", 1)[1].split(" more")[0])
    assert omitted == len(candidates) - 15
