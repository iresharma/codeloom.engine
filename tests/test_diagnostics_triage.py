"""Phase 6b (docs/impl-plans/jev-exp-1.md): diagnostics triage after a write.

The post-write LSP diagnostics resync in runtime/tools/edits.py is real
ground truth (the LSP told us the truth); the judge only decides what of
that truth is worth surfacing right now.
"""

from __future__ import annotations

import asyncio

from runtime.config import EngineConfig
from runtime.tools.edits import apply_edit
from tests.conftest import FakeJudge, FakeVerdict, seed


class FakeLspWithDiagnostics:
    def __init__(self, diags):
        self._diags = diags

    def cached_diagnostics(self, path):
        return []

    def diagnostics_after_change(self, rel_path, new_text, timeout=5.0):
        return self._diags


def _diag(message: str, line: int = 0, severity: int = 1) -> dict:
    return {
        "range": {"start": {"line": line, "character": 0}},
        "message": message,
        "severity": severity,
        "source": "pyright",
    }


def _edit(ctx):
    async def run():
        return await apply_edit(
            ctx, "a.py", lambda src: src.text.replace("1", "2"), "str_replace"
        )

    return asyncio.run(run())


def test_none_verdict_shows_every_diagnostic(ctx):
    seed(ctx, "a.py", "print(1)\n")
    ctx.lsp = FakeLspWithDiagnostics([_diag("caused"), _diag("noise")])
    ctx.config = EngineConfig(judge_mode="enforcing")
    ctx.judge = FakeJudge()  # no scripted response -> ask() returns None

    result = _edit(ctx)

    assert "caused" in result
    assert "noise" in result
    assert "suppressed" not in result


def test_disabled_judge_shows_every_diagnostic(ctx):
    seed(ctx, "a.py", "print(1)\n")
    ctx.lsp = FakeLspWithDiagnostics([_diag("caused"), _diag("noise")])
    ctx.config = EngineConfig(judge_mode="enforcing")
    ctx.judge = None

    result = _edit(ctx)

    assert "caused" in result
    assert "noise" in result


def test_enforcing_surfaces_caused_and_suppresses_unrelated_low_severity(ctx):
    seed(ctx, "a.py", "print(1)\n")
    ctx.lsp = FakeLspWithDiagnostics(
        [_diag("caused by this edit"), _diag("pre-existing noise")]
    )
    ctx.config = EngineConfig(judge_mode="enforcing")
    judge = FakeJudge()
    judge.responses["diagnostics_triage"] = FakeVerdict(
        nouls={
            "1:caused_by_this_edit": 0.9,
            "1:actionable_now": 0.9,
            "2:caused_by_this_edit": 0.05,
            "2:actionable_now": 0.1,
        },
        scores={"1:severity": 1.0, "2:severity": 0.0},
    )
    ctx.judge = judge
    judgements: list[dict] = []
    ctx.on_judgement = lambda **kw: judgements.append(kw)

    result = _edit(ctx)

    assert "caused by this edit" in result
    assert "pre-existing noise" not in result
    assert "1 more suppressed" in result
    assert judgements
    assert judgements[0]["outcome"] == "suppressed 1/2"
    assert judgements[0]["enforced"] is True


def test_enforcing_surfaces_preexisting_but_severe_and_actionable(ctx):
    seed(ctx, "a.py", "print(1)\n")
    ctx.lsp = FakeLspWithDiagnostics([_diag("blocks correctness elsewhere")])
    ctx.config = EngineConfig(judge_mode="enforcing")
    judge = FakeJudge()
    judge.responses["diagnostics_triage"] = FakeVerdict(
        nouls={"1:caused_by_this_edit": 0.05, "1:actionable_now": 0.9},
        scores={"1:severity": 2.0},
    )
    ctx.judge = judge

    result = _edit(ctx)

    assert "blocks correctness elsewhere" in result


def test_enforcing_suppresses_preexisting_cosmetic_diagnostic(ctx):
    seed(ctx, "a.py", "print(1)\n")
    ctx.lsp = FakeLspWithDiagnostics([_diag("cosmetic pre-existing nit")])
    ctx.config = EngineConfig(judge_mode="enforcing")
    judge = FakeJudge()
    judge.responses["diagnostics_triage"] = FakeVerdict(
        nouls={"1:caused_by_this_edit": 0.05, "1:actionable_now": 0.9},
        scores={"1:severity": 0.0},  # cosmetic
    )
    ctx.judge = judge

    result = _edit(ctx)

    assert "cosmetic pre-existing nit" not in result
    assert "no new diagnostics" in result


def test_advisory_mode_logs_but_shows_every_diagnostic(ctx):
    seed(ctx, "a.py", "print(1)\n")
    ctx.lsp = FakeLspWithDiagnostics(
        [_diag("caused by this edit"), _diag("pre-existing noise")]
    )
    ctx.config = EngineConfig(judge_mode="advisory")
    judge = FakeJudge()
    judge.responses["diagnostics_triage"] = FakeVerdict(
        nouls={
            "1:caused_by_this_edit": 0.9,
            "1:actionable_now": 0.9,
            "2:caused_by_this_edit": 0.05,
            "2:actionable_now": 0.1,
        },
        scores={"1:severity": 1.0, "2:severity": 0.0},
    )
    ctx.judge = judge
    judgements: list[dict] = []
    ctx.on_judgement = lambda **kw: judgements.append(kw)

    result = _edit(ctx)

    assert "caused by this edit" in result
    assert "pre-existing noise" in result  # advisory never hides anything
    assert judgements
    assert judgements[0]["enforced"] is False
