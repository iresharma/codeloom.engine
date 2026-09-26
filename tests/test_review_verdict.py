"""Item 5: the reviewer checks the original task, and the engine derives the
verdict from the table rather than trusting the prose.

Boundaries mocked: the LLM provider only. The brief the reviewer child
actually receives is captured from the provider's `messages`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agents.orchestrator import Orchestrator
from agents.profile import discover_profiles
from agents.review_verdict import (
    APPROVE,
    BLOCK,
    REQUEST_CHANGES,
    build_reviewer_brief,
    enforce_verdict,
    parse_requirements_table,
    stated_verdict,
)
from llm.provider import LLMResult, ToolCall
from runtime.config import EngineConfig
from runtime.tools.git import add_agent_worktree, commit_if_dirty
from tests.fakes import FakeProvider
from tools.registry import discover_tools

TABLE_OK = """verdict: approve
=== REQUIREMENTS ===
| requirement | kind | status | evidence |
|---|---|---|---|
| cap redirects at five hops | hard | met | tools/http.py:41 |
| if easy, sanity-check locally | conditional | skipped (no local runner) | - |
=== END REQUIREMENTS ===
"""

TABLE_UNMET = """verdict: approve — the TODOs are expected per brief item 6
=== REQUIREMENTS ===
| requirement | kind | status | evidence |
|---|---|---|---|
| a write never leaves a stale cached read behind | hard | not met (TODO in two write paths) | cache.py:88 |
| cap redirects at five hops | hard | met | tools/http.py:41 |
=== END REQUIREMENTS ===
"""

TABLE_UNDISPOSED = """verdict: approve
=== REQUIREMENTS ===
| requirement | kind | status | evidence |
| cap redirects at five hops | hard | met | tools/http.py:41 |
| if easy, sanity-check with a local run | conditional | - | - |
=== END REQUIREMENTS ===
"""


# --------------------------------------------------------------------------
# parsing


def test_parses_rows_with_kind_status_and_evidence():
    rows = parse_requirements_table(TABLE_OK)
    assert len(rows) == 2
    assert rows[0].requirement == "cap redirects at five hops"
    assert rows[0].is_hard and rows[0].status == "met"
    assert rows[0].evidence == "tools/http.py:41"
    assert rows[1].is_conditional and rows[1].status == "skipped"
    assert rows[1].reason == "no local runner"
    assert not rows[1].undisposed


def test_skips_the_header_and_separator_rows():
    rows = parse_requirements_table(TABLE_UNMET)
    assert [row.status for row in rows] == ["not met", "met"]


def test_parses_a_table_without_outer_pipes_or_markers():
    text = "requirement | kind | status | evidence\ncap redirects | hard | met | http.py:4"
    rows = parse_requirements_table(text)
    assert len(rows) == 1
    assert rows[0].requirement == "cap redirects"


def test_a_three_column_row_is_accepted_with_no_evidence():
    rows = parse_requirements_table("| cap redirects | hard | met |")
    assert len(rows) == 1
    assert rows[0].evidence == ""


def test_an_unrecognized_kind_is_treated_as_hard():
    rows = parse_requirements_table("| cap redirects | ??? | not met |")
    assert rows[0].is_hard


def test_an_empty_disposition_is_undisposed():
    rows = parse_requirements_table(TABLE_UNDISPOSED)
    assert rows[1].undisposed


def test_skipped_without_a_reason_counts_as_undisposed():
    rows = parse_requirements_table("| sanity-check locally | conditional | skipped |")
    assert rows[0].status == "skipped"
    assert rows[0].undisposed


# --------------------------------------------------------------------------
# stated verdict


@pytest.mark.parametrize(
    "text,expected",
    [
        ("verdict: approve", APPROVE),
        ("verdict: request changes", REQUEST_CHANGES),
        ("verdict: changes requested", REQUEST_CHANGES),
        ("verdict: block", BLOCK),
        ("LGTM overall", APPROVE),
        ("Nothing conclusive here", ""),
    ],
)
def test_stated_verdict(text, expected):
    assert stated_verdict(text) == expected


def test_block_wins_over_approve_in_mixed_prose():
    assert stated_verdict("mostly looks good but I block on the cache path") == BLOCK


# --------------------------------------------------------------------------
# enforcement


def test_a_clean_table_keeps_the_approve():
    verdict = enforce_verdict(TABLE_OK)
    assert verdict.verdict == APPROVE
    assert verdict.overridden is False


def test_an_unmet_hard_requirement_forces_request_changes_over_an_approve():
    verdict = enforce_verdict(TABLE_UNMET)
    assert verdict.verdict == REQUEST_CHANGES
    assert verdict.stated == APPROVE
    assert verdict.overridden is True
    assert "stale cached read" in verdict.reason


def test_a_documented_todo_does_not_excuse_an_unmet_requirement():
    # The exact excuse from the trial: "expected per brief item 6".
    assert "brief item 6" in TABLE_UNMET
    assert enforce_verdict(TABLE_UNMET).verdict == REQUEST_CHANGES


def test_a_conditional_with_no_disposition_forces_request_changes():
    verdict = enforce_verdict(TABLE_UNDISPOSED)
    assert verdict.verdict == REQUEST_CHANGES
    assert "no disposition" in verdict.reason


def test_a_hard_requirement_marked_skipped_forces_request_changes():
    text = (
        "verdict: approve\n=== REQUIREMENTS ===\n"
        "| cap redirects | hard | skipped (ran out of turns) | - |\n"
        "=== END REQUIREMENTS ==="
    )
    verdict = enforce_verdict(text)
    assert verdict.verdict == REQUEST_CHANGES
    assert "skipped rather than met" in verdict.reason


def test_no_table_at_all_forces_request_changes():
    verdict = enforce_verdict("verdict: approve\nLooks fine to me.")
    assert verdict.verdict == REQUEST_CHANGES
    assert "no requirements table" in verdict.reason


def test_block_is_never_downgraded():
    text = "verdict: block\n=== REQUIREMENTS ===\n| x | hard | met | a.py:1 |\n"
    assert enforce_verdict(text).verdict == BLOCK


def test_request_changes_is_preserved_without_an_override_note():
    text = (
        "verdict: request changes\n=== REQUIREMENTS ===\n"
        "| cap redirects | hard | met | http.py:4 |\n=== END REQUIREMENTS ==="
    )
    verdict = enforce_verdict(text)
    assert verdict.verdict == REQUEST_CHANGES
    assert verdict.overridden is False


# --------------------------------------------------------------------------
# the brief


def test_brief_keeps_the_original_task_verbatim_and_labels_the_paraphrase():
    task = (
        "Add a write-through cache. A write must never leave a stale cached "
        "read behind. If easy, sanity-check with a local run."
    )
    brief = "Add caching to cache.py. TODOs are fine for the rarer write paths."
    out = build_reviewer_brief(task, brief, "=== HARNESS VERIFY ===\nverdict: PASSED")
    assert task in out
    assert "ORIGINAL USER TASK" in out
    assert "ORCHESTRATOR'S INTERPRETATION" in out
    # The original comes first, so a long brief cannot bury it.
    assert out.index(task) < out.index(brief)
    assert "=== REQUIREMENTS ===" in out
    assert "verdict: PASSED" in out


def test_brief_survives_an_interpretation_that_dropped_the_requirement():
    task = "A write must never leave a stale cached read behind."
    brief = "Add caching to cache.py."
    out = build_reviewer_brief(task, brief)
    assert "stale cached read" in out


# --------------------------------------------------------------------------
# end to end through the orchestrator


def _init_git(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, capture_output=True)
    for key, value in (("user.email", "t@t.t"), ("user.name", "t")):
        subprocess.run(
            ["git", "config", key, value], cwd=path, check=True, capture_output=True
        )
    (path / "README").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"], cwd=path, check=True, capture_output=True
    )


class _Reviewer(FakeProvider):
    def __init__(self, brief_from_orch: str, review_text: str):
        super().__init__()
        self._brief = brief_from_orch
        self._review = review_text
        self.reviewer_briefs: list[str] = []
        self._spawned = False

    async def complete(self, messages, tools=None, *, on_delta=None, **kwargs):
        self.calls += 1
        names = {
            (item.get("function") or item).get("name")
            for item in (tools or [])
            if isinstance(item, dict)
        }
        if "reviewer" in names:
            if not self._spawned:
                self._spawned = True
                import json

                return LLMResult(
                    text="",
                    tool_calls=[
                        ToolCall(
                            id="c1",
                            name="reviewer",
                            arguments_json=json.dumps({"task": self._brief}),
                        )
                    ],
                )
            return LLMResult(text="reviewer started")
        if "run_verify" in names:
            self.reviewer_briefs.append(
                next(
                    (m["content"] for m in reversed(messages) if m.get("role") == "user"),
                    "",
                )
            )
            return LLMResult(text=self._review)
        return LLMResult(text="ok")


async def _wait_children(orch: Orchestrator, timeout: float = 20.0) -> None:
    import asyncio

    for _ in range(int(timeout / 0.02)):
        if not any(not task.done() for task in orch._child_tasks.values()):
            return
        await asyncio.sleep(0.02)
    raise AssertionError("children did not finish")


def _orch_with_tree(repo: Path, provider, agent_id: str):
    orch = Orchestrator(
        provider,
        all_tools=discover_tools(),
        profiles=discover_profiles(),
        workspace=repo,
        config=EngineConfig(verify_command="printf '== 3 passed in 0.1s ==\\n'"),
    )
    dest, branch, err = add_agent_worktree(repo, agent_id, "coder")
    assert not err, err
    work = Path(dest)
    (work / "cache.py").write_text("def put(k, v):\n    pass\n")
    commit_if_dirty(work, "work")
    orch._remember_worktree(agent_id, work, branch, "coder", "b1")
    return orch, work


@pytest.mark.asyncio
async def test_reviewer_brief_keeps_a_requirement_the_orch_brief_dropped(tmp_path):
    repo = tmp_path / "repo"
    _init_git(repo)
    user_task = (
        "Add a write-through cache in cache.py. A write must never leave a "
        "stale cached read behind."
    )
    orch_brief = "Review cache.py. TODOs on the rarer write paths are expected."
    provider = _Reviewer(orch_brief, TABLE_OK)
    orch, _work = _orch_with_tree(repo, provider, "r1")

    await orch.run(user_task)
    await _wait_children(orch)

    assert provider.reviewer_briefs, "the reviewer never ran"
    brief = provider.reviewer_briefs[0]
    assert "stale cached read behind" in brief
    assert "ORIGINAL USER TASK" in brief
    assert orch_brief in brief
    assert "ORCHESTRATOR'S INTERPRETATION" in brief


@pytest.mark.asyncio
async def test_a_not_met_row_forces_request_changes_in_the_orch_report(tmp_path):
    repo = tmp_path / "repo"
    _init_git(repo)
    provider = _Reviewer("Review cache.py.", TABLE_UNMET)
    orch, _work = _orch_with_tree(repo, provider, "r2")
    reports: list[tuple[str, str]] = []
    orch._on_agent_result = lambda aid, profile, text: reports.append((profile, text))

    await orch.run("A write must never leave a stale cached read behind.")
    await _wait_children(orch)

    review = [text for profile, text in reports if profile == "reviewer"]
    assert review, "no reviewer report"
    assert "review_verdict: request_changes" in review[0]
    assert "verdict forced to request_changes" in review[0]
    assert "stale cached read" in review[0]


@pytest.mark.asyncio
async def test_a_clean_table_leaves_the_approve_alone(tmp_path):
    repo = tmp_path / "repo"
    _init_git(repo)
    provider = _Reviewer("Review http.py.", TABLE_OK)
    orch, _work = _orch_with_tree(repo, provider, "r3")
    reports: list[tuple[str, str]] = []
    orch._on_agent_result = lambda aid, profile, text: reports.append((profile, text))

    await orch.run("Cap redirects at five hops.")
    await _wait_children(orch)

    review = [text for profile, text in reports if profile == "reviewer"]
    assert review
    assert "review_verdict: approve" in review[0]
    assert "verdict forced" not in review[0]
