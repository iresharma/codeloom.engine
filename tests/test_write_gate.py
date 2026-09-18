"""Phase 7 (docs/impl-plans/jev-exp-1.md): the semantic write gate.

runtime/tools/edits.py's _judge_write_gate / _judge_workspace_write_gate run
entirely before `_apply_sync` -- invariant 5's concurrency guarantee is
exercised separately in tests/test_concurrency.py (the actively-yielding
SlowJudge test); this file covers classification and rollout behaviour.
"""

from __future__ import annotations

import asyncio

from runtime.config import EngineConfig
from runtime.tools.edits import TextEdit, apply_edit, apply_workspace_edit
from tests.conftest import FakeJudge, FakeVerdict, seed


def _edit(ctx, old="print(1)", new="print(2)"):
    async def run():
        return await apply_edit(
            ctx, "a.py", lambda src, o=old, n=new: src.text.replace(o, n), "str_replace"
        )

    return asyncio.run(run())


def _judged_ctx(ctx, judge, *, judge_mode="enforcing"):
    # judge_mode_write set explicitly: the write gate never inherits a
    # blanket ENGINE_JUDGE=enforcing on its own (see judge_mode_for), so
    # tests that want it enforcing say so directly, same as a real deploy
    # would need ENGINE_JUDGE_WRITE=enforcing.
    ctx.config = EngineConfig(judge_mode=judge_mode, judge_mode_write=judge_mode)
    ctx.judge = judge
    ctx.judgements = []
    ctx.on_judgement = lambda **kw: ctx.judgements.append(kw)
    ctx.user_request = "fix the print statement"
    return ctx


def _clean_verdict(**overrides) -> FakeVerdict:
    """A verdict with every signal set to a value that should never flag or
    block on its own -- callers override just the signal they're testing."""
    nouls = {
        "matches_stated_intent": 0.9,
        "deletes_unrelated_code": 0.0,
        "introduces_hardcoded_secret": 0.0,
        "disables_a_test_or_check": 0.0,
    }
    nouls.update(overrides.pop("nouls", {}))
    scores = {"scope_creep": 0.0}
    scores.update(overrides.pop("scores", {}))
    return FakeVerdict(nouls=nouls, scores=scores, **overrides)


def test_none_verdict_reproduces_behavior_unchanged(ctx):
    seed(ctx, "a.py", "print(1)\n")
    judge = FakeJudge()  # no scripted response -> ask() returns None
    _judged_ctx(ctx, judge)

    result = _edit(ctx)

    assert result.startswith("ok:")
    assert judge.calls  # the gate was actually consulted
    assert ctx.judgements == []
    assert (ctx.workspace / "a.py").read_text() == "print(2)\n"


def test_disabled_judge_reproduces_behavior_unchanged(ctx):
    seed(ctx, "a.py", "print(1)\n")
    ctx.judge = None
    ctx.config = EngineConfig(judge_mode="enforcing")

    result = _edit(ctx)

    assert result.startswith("ok:")
    assert (ctx.workspace / "a.py").read_text() == "print(2)\n"


def test_blocks_on_hardcoded_secret_when_enforcing(ctx):
    seed(ctx, "a.py", "print(1)\n")
    judge = FakeJudge()
    judge.responses["write_gate"] = FakeVerdict(nouls={"introduces_hardcoded_secret": 0.9})
    _judged_ctx(ctx, judge)

    result = _edit(ctx)

    assert result.startswith("error: refused")
    assert "secret" in result.lower() or "credential" in result.lower()
    assert (ctx.workspace / "a.py").read_text() == "print(1)\n"  # never written
    assert ctx.judgements[0]["outcome"] == "block"
    assert ctx.judgements[0]["enforced"] is True


def test_secret_at_threshold_does_not_block():
    from runtime.judge_decisions import classify_write

    verdict = FakeVerdict(nouls={"introduces_hardcoded_secret": 0.8})
    decision, _ = classify_write(verdict)
    assert decision != "block"


def test_flags_disabled_check_but_still_commits(ctx):
    seed(ctx, "a.py", "print(1)\n")
    judge = FakeJudge()
    judge.responses["write_gate"] = FakeVerdict(nouls={"disables_a_test_or_check": 0.9})
    _judged_ctx(ctx, judge)

    result = _edit(ctx)

    assert result.startswith("ok:")  # flagged, not blocked
    assert (ctx.workspace / "a.py").read_text() == "print(2)\n"  # committed anyway
    assert "flagged" in result.lower()
    assert ctx.judgements[0]["outcome"] == "flag"
    assert ctx.judgements[0]["enforced"] is True


def test_other_three_signals_never_block_even_when_extreme(ctx):
    """Stage 3 ("consider blocking on the others") is deliberately not
    implemented -- only introduces_hardcoded_secret can ever block."""
    seed(ctx, "a.py", "print(1)\n")
    judge = FakeJudge()
    judge.responses["write_gate"] = FakeVerdict(
        nouls={
            "disables_a_test_or_check": 0.99,
            "deletes_unrelated_code": 0.99,
            "matches_stated_intent": 0.0,
        },
        scores={"scope_creep": 2.0},
    )
    _judged_ctx(ctx, judge)

    result = _edit(ctx)

    assert result.startswith("ok:")
    assert (ctx.workspace / "a.py").read_text() == "print(2)\n"


def test_advisory_mode_never_blocks_even_on_secret(ctx):
    seed(ctx, "a.py", "print(1)\n")
    judge = FakeJudge()
    judge.responses["write_gate"] = FakeVerdict(nouls={"introduces_hardcoded_secret": 0.99})
    _judged_ctx(ctx, judge, judge_mode="advisory")

    result = _edit(ctx)

    assert result.startswith("ok:")
    assert (ctx.workspace / "a.py").read_text() == "print(2)\n"
    assert ctx.judgements[0]["outcome"] == "block"
    assert ctx.judgements[0]["enforced"] is False


def test_write_gate_defaults_to_advisory_under_global_enforcing_end_to_end(ctx):
    """The write gate never silently inherits a blanket enforcing setting
    meant for other sites -- config.judge_mode_for("write") caps it at
    advisory unless ENGINE_JUDGE_WRITE is set explicitly."""
    seed(ctx, "a.py", "print(1)\n")
    judge = FakeJudge()
    judge.responses["write_gate"] = FakeVerdict(nouls={"introduces_hardcoded_secret": 0.99})
    ctx.config = EngineConfig(judge_mode="enforcing")  # global enforcing, no override
    ctx.judge = judge
    ctx.on_judgement = lambda **kw: None

    result = _edit(ctx)

    assert result.startswith("ok:")  # capped at advisory -> never blocks


def test_write_gate_explicit_override_does_enforce(ctx):
    seed(ctx, "a.py", "print(1)\n")
    judge = FakeJudge()
    judge.responses["write_gate"] = FakeVerdict(nouls={"introduces_hardcoded_secret": 0.99})
    ctx.config = EngineConfig(judge_mode="enforcing", judge_mode_write="enforcing")
    ctx.judge = judge
    ctx.on_judgement = lambda **kw: None

    result = _edit(ctx)

    assert result.startswith("error: refused")


def test_syntax_gate_still_wins_even_when_judge_would_allow(ctx):
    """Invariant: a high matches_stated_intent (or any judge signal) must
    never let an edit past the syntax gate -- it can only ever add a
    refusal, never relax a deterministic guard."""
    seed(ctx, "a.py", "print(1)\n")
    judge = FakeJudge()
    judge.responses["write_gate"] = FakeVerdict(nouls={"matches_stated_intent": 0.99})
    _judged_ctx(ctx, judge)

    result = _edit(ctx, old="print(1)", new="print(2")  # unbalanced paren -> syntax gate

    assert result.startswith("error:")
    assert (ctx.workspace / "a.py").read_text() == "print(1)\n"


def test_multi_file_batch_gets_one_judgment_not_one_per_file(ctx):
    (ctx.workspace / "a.py").write_text("alpha = 1\n")
    (ctx.workspace / "b.py").write_text("alpha = 1\n")
    edits = [
        ("a.py", [TextEdit(start_line=0, start_char=0, end_line=0, end_char=5, new_text="gamma")]),
        ("b.py", [TextEdit(start_line=0, start_char=0, end_line=0, end_char=5, new_text="gamma")]),
    ]
    judge = FakeJudge()
    judge.responses["write_gate"] = _clean_verdict()
    _judged_ctx(ctx, judge)

    async def run():
        return await apply_workspace_edit(ctx, edits, "rename_symbol")

    result = asyncio.run(run())

    assert result.startswith("ok:")
    write_gate_calls = [c for c in judge.calls if c["tag"] == "write_gate"]
    assert len(write_gate_calls) == 1  # one judgment for the whole batch
    combined_diff = write_gate_calls[0]["state"]["diff"]
    assert "a.py" in combined_diff
    assert "b.py" in combined_diff


def test_multi_file_batch_blocks_on_secret_before_any_file_is_written(ctx):
    (ctx.workspace / "a.py").write_text("alpha = 1\n")
    (ctx.workspace / "b.py").write_text("alpha = 1\n")
    edits = [
        ("a.py", [TextEdit(start_line=0, start_char=0, end_line=0, end_char=5, new_text="gamma")]),
        ("b.py", [TextEdit(start_line=0, start_char=0, end_line=0, end_char=5, new_text="gamma")]),
    ]
    judge = FakeJudge()
    judge.responses["write_gate"] = FakeVerdict(nouls={"introduces_hardcoded_secret": 0.95})
    _judged_ctx(ctx, judge)

    async def run():
        return await apply_workspace_edit(ctx, edits, "rename_symbol")

    result = asyncio.run(run())

    assert result.startswith("error: refused")
    assert (ctx.workspace / "a.py").read_text() == "alpha = 1\n"
    assert (ctx.workspace / "b.py").read_text() == "alpha = 1\n"
