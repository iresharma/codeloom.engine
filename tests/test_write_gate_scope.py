"""Item 7: the write gate judges this edit's own hunk, and every decision is
journalled against the edit it was about.

Boundaries mocked: the judge (FakeJudge, from conftest). Edits are real writes
through the funnel, and the judgements/edits journals are real sqlite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from runtime.config import EngineConfig
from runtime.judge_decisions import edit_hunk_changes, write_gate_questions
from runtime.store.judgements import all_for_session, ensure_schema, record
from runtime.tools.edits import apply_edit
from runtime.tools.fileid import read_source
from tests.conftest import FakeVerdict, seed
from tools.base import ToolContext

SECRET = "sk-live-9f3a2b1c8d7e6f5a"


def _judged_ctx(ctx: ToolContext, judge, *, mode="advisory") -> ToolContext:
    ctx.judge = judge
    ctx.config = EngineConfig(judge_mode=mode)
    ctx.user_request = "cap redirects at five hops"
    records: list[dict] = []

    def on_judgement(**kwargs):
        records.append(kwargs)

    ctx.on_judgement = on_judgement
    ctx._records = records  # type: ignore[attr-defined]
    return ctx


def _clean_verdict() -> FakeVerdict:
    return FakeVerdict(
        nouls={
            "matches_stated_intent": 0.9,
            "deletes_unrelated_code": 0.0,
            "introduces_hardcoded_secret": 0.0,
            "disables_a_test_or_check": 0.0,
        },
        scores={"scope_creep": 0.0},
    )


def _flagging_verdict() -> FakeVerdict:
    return FakeVerdict(
        nouls={
            "matches_stated_intent": 0.9,
            "deletes_unrelated_code": 0.0,
            "introduces_hardcoded_secret": 0.0,
            "disables_a_test_or_check": 0.9,
        },
        scores={"scope_creep": 0.0},
    )


def _touch(ctx: ToolContext, rel: str) -> None:
    src = read_source(ctx.workspace, rel)
    ctx.files.mark(src.rel, src.raw_sha256)


# --------------------------------------------------------------------------
# edit_hunk_changes


def test_hunk_changes_keeps_only_added_and_removed_lines():
    diff = (
        "--- a/http.py\n"
        "+++ b/http.py\n"
        "@@ -1,4 +1,4 @@\n"
        ' PASSWORD = "hunter2"\n'
        "-    return n + 1\n"
        "+    return n + 2\n"
        " TOKEN = \"ghp_deadbeef\"\n"
    )
    out = edit_hunk_changes(diff)
    assert out == "-    return n + 1\n+    return n + 2"
    assert "hunter2" not in out
    assert "ghp_deadbeef" not in out
    assert "+++ " not in out and "--- " not in out


def test_hunk_changes_of_an_empty_diff_is_empty():
    assert edit_hunk_changes("") == ""
    assert edit_hunk_changes("--- a/x\n+++ b/x\n") == ""


def test_blocking_questions_are_asked_about_changed_lines():
    questions = write_gate_questions()
    secret = questions["introduces_hardcoded_secret"].instructions
    disables = questions["disables_a_test_or_check"].instructions
    assert "changed_lines" in secret
    assert "changed_lines" in disables
    # The intent/scope questions still need the surrounding diff.
    assert "`diff`" in questions["matches_stated_intent"].instructions
    assert "`diff`" in questions["scope_creep"].instructions


# --------------------------------------------------------------------------
# the state the judge actually receives


@pytest.mark.asyncio
async def test_a_later_edit_to_another_file_is_not_judged_on_file_as_secret(
    ctx, judge
):
    """File A gets a fixture credential; a later edit to file B has no string
    literals at all. B's judgement must not see A's secret."""
    _judged_ctx(ctx, judge)
    judge.responses["write_gate"] = _clean_verdict()

    seed(ctx, "tests/fixtures.py", 'USERNAME = "alice"\n')
    await apply_edit(
        ctx,
        "tests/fixtures.py",
        lambda src: src.text + f'PASSWORD = "{SECRET}"\n',
        "str_replace",
    )
    seed(ctx, "http.py", "def hops(n):\n    return n + 1\n")
    await apply_edit(
        ctx, "http.py", lambda src: src.text.replace("n + 1", "n + 2"), "str_replace"
    )

    assert len(judge.calls) == 2
    second = judge.calls[1]["state"]
    assert second["path"] == "http.py"
    assert SECRET not in second["diff"]
    assert SECRET not in second["changed_lines"]
    assert "PASSWORD" not in second["changed_lines"]
    # And nothing string-literal-shaped is in what the secret question reads.
    assert second["changed_lines"] == "-    return n + 1\n+    return n + 2"


@pytest.mark.asyncio
async def test_a_credential_only_in_context_is_not_in_changed_lines(ctx, judge):
    """The sharper case: the *same* file already holds fixture credentials, so
    they land in the unified diff's context lines. The blocking signal must
    not read them."""
    _judged_ctx(ctx, judge)
    judge.responses["write_gate"] = _clean_verdict()

    seed(
        ctx,
        "tests/fixtures.py",
        f'PASSWORD = "{SECRET}"\nTOKEN = "ghp_deadbeef"\nTIMEOUT = 1\n',
    )
    await apply_edit(
        ctx,
        "tests/fixtures.py",
        lambda src: src.text.replace("TIMEOUT = 1", "TIMEOUT = 2"),
        "str_replace",
    )

    state = judge.calls[0]["state"]
    # The credential really is in the diff, as context.
    assert SECRET in state["diff"]
    # But not in the lines this edit wrote.
    assert SECRET not in state["changed_lines"]
    assert "ghp_deadbeef" not in state["changed_lines"]
    assert state["changed_lines"] == "-TIMEOUT = 1\n+TIMEOUT = 2"


@pytest.mark.asyncio
async def test_a_secret_this_edit_really_adds_is_still_in_changed_lines(ctx, judge):
    """The scoping must not blind the gate: an added credential is still
    visible to the signal that can block on it."""
    _judged_ctx(ctx, judge)
    judge.responses["write_gate"] = _clean_verdict()

    seed(ctx, "config.py", "DEBUG = False\n")
    await apply_edit(
        ctx,
        "config.py",
        lambda src: src.text + f'API_KEY = "{SECRET}"\n',
        "str_replace",
    )
    state = judge.calls[0]["state"]
    assert SECRET in state["changed_lines"]


@pytest.mark.asyncio
async def test_the_gate_never_sees_the_cumulative_worktree_diff(ctx, judge):
    """Three edits to three files: each judgement sees exactly one path."""
    _judged_ctx(ctx, judge)
    judge.responses["write_gate"] = _clean_verdict()
    for name in ("a.py", "b.py", "c.py"):
        seed(ctx, name, f"# {name}\nvalue = 1\n")
        await apply_edit(
            ctx, name, lambda src: src.text.replace("value = 1", "value = 2"), "str_replace"
        )
    assert len(judge.calls) == 3
    for name, call in zip(("a.py", "b.py", "c.py"), judge.calls, strict=True):
        state = call["state"]
        assert state["path"] == name
        others = {"a.py", "b.py", "c.py"} - {name}
        for other in others:
            assert other not in state["diff"]


# --------------------------------------------------------------------------
# decisions are journalled with an edit id


@pytest.mark.asyncio
async def test_a_flag_is_recorded_with_the_edit_id_it_judged(ctx, judge):
    _judged_ctx(ctx, judge)
    judge.responses["write_gate"] = _flagging_verdict()

    seed(ctx, "a.py", "value = 1\n")
    out = await apply_edit(
        ctx, "a.py", lambda src: src.text.replace("value = 1", "value = 2"), "str_replace"
    )
    assert "judge flagged" in out
    records = ctx._records
    assert len(records) == 1
    entry = records[0]
    assert entry["tag"] == "write_gate"
    assert entry["outcome"] == "flag"
    assert entry["edit_id"] is not None
    # It points at a real row in the edits journal.
    from runtime.store.edits import recent

    ids = {rec.id for rec in recent(Path(ctx.journal), ctx.session_id, limit=20)}
    assert entry["edit_id"] in ids
    assert entry["signals"]["disables_a_test_or_check"] == 0.9


@pytest.mark.asyncio
async def test_an_allowed_edit_records_nothing(ctx, judge):
    _judged_ctx(ctx, judge)
    judge.responses["write_gate"] = _clean_verdict()
    seed(ctx, "a.py", "value = 1\n")
    await apply_edit(
        ctx, "a.py", lambda src: src.text.replace("value = 1", "value = 2"), "str_replace"
    )
    assert ctx._records == []


@pytest.mark.asyncio
async def test_a_blocked_edit_is_recorded_without_an_edit_id(ctx, judge):
    _judged_ctx(ctx, judge, mode="enforcing")
    ctx.config.judge_mode_write = "enforcing"
    judge.responses["write_gate"] = FakeVerdict(
        nouls={
            "matches_stated_intent": 0.9,
            "introduces_hardcoded_secret": 0.95,
            "deletes_unrelated_code": 0.0,
            "disables_a_test_or_check": 0.0,
        },
        scores={"scope_creep": 0.0},
    )
    seed(ctx, "config.py", "DEBUG = False\n")
    out = await apply_edit(
        ctx,
        "config.py",
        lambda src: src.text + f'API_KEY = "{SECRET}"\n',
        "str_replace",
    )
    assert out.startswith("error: refused")
    # The write really was refused.
    assert SECRET not in (ctx.workspace / "config.py").read_text()
    assert len(ctx._records) == 1
    assert ctx._records[0]["outcome"] == "block"
    assert ctx._records[0]["edit_id"] is None


def test_enforcement_defaults_are_unchanged():
    from runtime.config import CALIBRATED_SITE_MODES
    from runtime.judge_decisions import (
        WRITE_DELETES_UNRELATED_FLAG,
        WRITE_DISABLES_CHECK_FLAG,
        WRITE_MATCHES_INTENT_FLAG,
        WRITE_SCOPE_CREEP_FLAG,
        WRITE_SECRET_BLOCK,
    )

    assert EngineConfig().judge_mode == "advisory"
    assert EngineConfig(judge_mode="enforcing").judge_mode_for("write") == "advisory"
    assert CALIBRATED_SITE_MODES["write"] == "enforcing"
    assert (
        WRITE_SECRET_BLOCK,
        WRITE_DISABLES_CHECK_FLAG,
        WRITE_DELETES_UNRELATED_FLAG,
        WRITE_SCOPE_CREEP_FLAG,
        WRITE_MATCHES_INTENT_FLAG,
    ) == (0.8, 0.5, 0.5, 1.6, 0.3)


# --------------------------------------------------------------------------
# the precision report


def test_judgements_store_round_trips_the_edit_id(tmp_path):
    db = tmp_path / "s.db"
    ensure_schema(db)
    record(
        db,
        session_id="run-1",
        tag="write_gate",
        subject="str_replace:a.py",
        outcome="flag",
        signals={"introduces_hardcoded_secret": 0.92},
        edit_id=41,
    )
    rows = all_for_session(db, "run-1")
    assert len(rows) == 1
    assert rows[0].edit_id == 41


def test_judgements_store_migrates_a_database_without_the_column(tmp_path):
    import sqlite3

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE judgements (
             id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
             tag TEXT NOT NULL, subject TEXT, outcome TEXT, signals TEXT,
             enforced INTEGER, latency_ms INTEGER, agent_id TEXT,
             created_at TEXT NOT NULL)"""
    )
    conn.execute(
        "INSERT INTO judgements (session_id, tag, outcome, created_at) "
        "VALUES ('old', 'write_gate', 'flag', '2026-01-01')"
    )
    conn.commit()
    conn.close()

    rows = all_for_session(db, "old")
    assert len(rows) == 1
    assert rows[0].edit_id is None
    record(db, session_id="old", tag="write_gate", subject="x", outcome="flag", edit_id=7)
    assert [row.edit_id for row in all_for_session(db, "old")] == [None, 7]


def test_precision_report_counts_flags_per_heuristic_per_run(tmp_path):
    from scripts.judge_precision import report, tripped

    db = tmp_path / "s.db"
    ensure_schema(db)
    rows = [
        ("run-a", {"introduces_hardcoded_secret": 0.92}, 1),
        ("run-a", {"introduces_hardcoded_secret": 0.9}, 2),
        ("run-a", {"disables_a_test_or_check": 0.7}, 3),
        ("run-b", {"scope_creep": 1.8}, 4),
        ("run-b", {"matches_stated_intent": 0.1}, 5),
        # Below every threshold: counted as a decision, attributed to nothing.
        ("run-b", {"introduces_hardcoded_secret": 0.1}, 6),
    ]
    for session, signals, edit_id in rows:
        record(
            db,
            session_id=session,
            tag="write_gate",
            subject="str_replace:a.py",
            outcome="flag",
            signals=signals,
            edit_id=edit_id,
        )
    summary = report(db)
    assert summary["total"] == 6
    assert summary["with_edit_id"] == 6
    assert summary["flags_per_run"]["run-a"] == {
        "introduces_hardcoded_secret": 2,
        "disables_a_test_or_check": 1,
    }
    assert summary["flags_per_run"]["run-b"] == {
        "scope_creep": 1,
        "matches_stated_intent": 1,
    }
    assert summary["no_heuristic_over_threshold"] == 1
    assert tripped({"introduces_hardcoded_secret": 0.92}) == [
        "introduces_hardcoded_secret"
    ]
    assert tripped({"introduces_hardcoded_secret": 0.5}) == []


def test_precision_report_uses_the_same_thresholds_as_the_gate():
    from runtime.judge_decisions import classify_write
    from scripts.judge_precision import tripped

    verdict = FakeVerdict(
        nouls={
            "matches_stated_intent": 0.9,
            "deletes_unrelated_code": 0.0,
            "introduces_hardcoded_secret": 0.0,
            "disables_a_test_or_check": 0.51,
        },
        scores={"scope_creep": 0.0},
    )
    decision, _ = classify_write(verdict)
    assert decision == "flag"
    assert tripped({"disables_a_test_or_check": 0.51, "matches_stated_intent": 0.9}) == [
        "disables_a_test_or_check"
    ]


def test_precision_report_cli_runs(tmp_path, capsys):
    from scripts.judge_precision import main

    db = tmp_path / "s.db"
    ensure_schema(db)
    record(
        db,
        session_id="run-a",
        tag="write_gate",
        subject="str_replace:a.py",
        outcome="flag",
        signals={"introduces_hardcoded_secret": 0.92},
        edit_id=3,
    )
    assert main(["--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "introduces_hardcoded_secret" in out
    assert "run-a" in out

    assert main(["--db", str(db), "--json"]) == 0
    import json as _json

    payload = _json.loads(capsys.readouterr().out)
    assert payload["flags_per_run"]["run-a"]["introduces_hardcoded_secret"] == 1

    assert main(["--db", str(tmp_path / "missing.db")]) == 1
