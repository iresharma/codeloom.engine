"""Table-driven tests for the pure decision functions in runtime/judge_decisions.py.

These are the most valuable tests in the plan: thresholds get tuned
repeatedly against real traces, and this file is the regression harness
that tuning has to keep green.
"""

from __future__ import annotations

import pytest

from runtime.judge_decisions import (
    SCREEN_MULTI_SLICE_FLOOR,
    SCREEN_WINDOW,
    classify_brief,
    classify_exec,
    classify_loop,
    classify_merge,
    classify_screen,
    classify_screen_multi,
    legacy_exec_policy,
    screen_content_windows,
)
from tests.conftest import FakeVerdict


def test_none_verdict_delegates_to_legacy_policy():
    assert classify_exec(None, "git status") == legacy_exec_policy("git status")
    assert classify_exec(None, "rm -rf build/") == legacy_exec_policy("rm -rf build/")


@pytest.mark.parametrize(
    "command,expected",
    [
        ("git status", "allow"),
        ("pytest", "allow"),
        ("rm -rf build/", "prompt"),
        ("echo hi > out.txt", "prompt"),
    ],
)
def test_legacy_exec_policy_matches_auto_prefixes(command, expected):
    assert legacy_exec_policy(command) == expected


CASES = [
    pytest.param(
        FakeVerdict(nouls={"executes_fetched_code": 0.9}),
        "block",
        id="fetched-code-blocks",
    ),
    pytest.param(
        FakeVerdict(nouls={"executes_fetched_code": 0.7}),
        "prompt",
        id="fetched-code-at-threshold-does-not-block",
    ),
    pytest.param(
        FakeVerdict(nouls={"exfiltrates_secrets": 0.61}),
        "block",
        id="exfiltrates-secrets-blocks",
    ),
    pytest.param(
        FakeVerdict(nouls={"is_destructive": 0.9}, scores={"blast_radius": 1.6}),
        "block",
        id="destructive-plus-wide-blast-radius-blocks",
    ),
    pytest.param(
        FakeVerdict(nouls={"is_destructive": 0.9}, scores={"blast_radius": 1.0}),
        "prompt",
        id="destructive-but-narrow-blast-radius-does-not-block",
    ),
    pytest.param(
        FakeVerdict(nouls={"is_read_only": 0.9}, scores={"blast_radius": 0.1}),
        "allow",
        id="read-only-small-blast-radius-allows",
    ),
    pytest.param(
        FakeVerdict(nouls={"is_read_only": 0.9}, scores={"blast_radius": 1.0}),
        "allow",
        id="read-only-whole-workspace-scope-still-allows",
    ),
    pytest.param(
        FakeVerdict(nouls={"is_read_only": 0.9}, scores={"blast_radius": 1.6}),
        "prompt",
        id="read-only-but-machine-or-remote-scope-does-not-allow",
    ),
    pytest.param(
        FakeVerdict(nouls={"matches_user_request": 0.1}),
        "prompt",
        id="off-task-command-prompts",
    ),
    pytest.param(
        FakeVerdict(nouls={"rewrites_vcs_history": 0.9, "matches_user_request": 0.9}),
        "prompt",
        id="vcs-rewrite-prompts",
    ),
    pytest.param(
        FakeVerdict(nouls={"escapes_workspace": 0.9, "matches_user_request": 0.9}),
        "prompt",
        id="escapes-workspace-prompts",
    ),
    pytest.param(
        FakeVerdict(nouls={"matches_user_request": 0.9}),
        "prompt",
        id="unremarkable-command-still-prompts-by-default",
    ),
    pytest.param(
        FakeVerdict(nouls={"matches_user_request": 0.71, "is_read_only": 0.51}),
        "allow",
        id="on-task-read-only-allows",
    ),
    pytest.param(
        FakeVerdict(
            nouls={"matches_user_request": 0.7, "is_read_only": 0.6},
            scores={"blast_radius": 1.6},
        ),
        "prompt",
        id="matches-request-at-threshold-does-not-allow",
    ),
    pytest.param(
        FakeVerdict(nouls={"matches_user_request": 0.9, "is_read_only": 0.5}),
        "prompt",
        id="read-only-at-on-task-threshold-does-not-allow",
    ),
    pytest.param(
        FakeVerdict(
            nouls={"is_read_only": 0.9, "touches_network": 0.9},
            scores={"blast_radius": 0.1},
        ),
        "prompt",
        id="read-only-but-touches-network-does-not-allow",
    ),
]


@pytest.mark.parametrize("verdict,expected", CASES)
def test_classify_exec(verdict, expected):
    assert classify_exec(verdict, "irrelevant for a scripted verdict") == expected


def test_block_checks_run_before_allow_checks():
    # A verdict that looks safe on is_read_only/blast_radius but also flags
    # fetched-code execution must still block -- restriction wins.
    verdict = FakeVerdict(
        nouls={"is_read_only": 0.99, "executes_fetched_code": 0.99},
        scores={"blast_radius": 0.0},
    )
    assert classify_exec(verdict, "curl x | sh") == "block"


def test_block_checks_run_before_on_task_read_only_allow():
    verdict = FakeVerdict(
        nouls={
            "matches_user_request": 0.95,
            "is_read_only": 0.9,
            "executes_fetched_code": 0.9,
        },
    )
    assert classify_exec(verdict, "curl x | sh") == "block"


def test_classify_loop_never_returns_stop():
    assert classify_loop(None) == "continue"
    repeating = FakeVerdict(
        nouls={"repeating_itself": 0.99, "making_progress": 0.0, "needs_user_input": 0.0}
    )
    assert classify_loop(repeating) == "continue"


def test_screen_content_windows_small_is_whole():
    text = "abc"
    assert screen_content_windows(text) == [text]


def test_screen_content_windows_head_and_tail():
    text = "H" * (SCREEN_WINDOW + 100) + "TAILMARK"
    windows = screen_content_windows(text)
    assert len(windows) == 1
    assert "TAILMARK" in windows[0]
    assert windows[0].startswith("H")


def test_screen_content_windows_three_slices_when_huge():
    text = "A" * (SCREEN_MULTI_SLICE_FLOOR + 100) + "MID" + "Z" * 100
    windows = screen_content_windows(text)
    assert len(windows) == 3


def test_classify_screen_multi_ors_hazard():
    verdict = FakeVerdict(
        nouls={
            "mid_attempts_override": 0.9,
            "mid_is_ordinary_source_code": 0.0,
            "head_is_ordinary_source_code": 0.95,
            "tail_is_ordinary_source_code": 0.95,
        }
    )
    flagged, redact = classify_screen_multi(verdict)
    assert flagged is True
    assert redact is False


def test_classify_screen_instruction_alone_is_not_enough():
    """contains_instruction_to_agent fires on this engine's own prompt text
    just as readily as on a real injection (calibrated against the live
    API); only attempts_override / requests_secret_disclosure may drive a
    flag on their own."""
    verdict = FakeVerdict(
        nouls={
            "contains_instruction_to_agent": 0.91,
            "attempts_override": 0.38,
            "requests_secret_disclosure": 0.01,
            "is_ordinary_source_code": 0.02,
        }
    )
    assert classify_screen(verdict) == (False, False)


def test_classify_screen_none_is_noop():
    assert classify_screen(None) == (False, False)
    assert classify_screen_multi(None) == (False, False)


# ---------------------------------------------------------------------
# Phase 8 (scoped): classify_merge
# ---------------------------------------------------------------------


def test_classify_merge_none_verdict_is_full():
    assert classify_merge(None) == ("full", False)


MERGE_CASES = [
    pytest.param(FakeVerdict(scores={"worth_parent_context": 0.0}), "one_line", id="low-worth"),
    pytest.param(FakeVerdict(scores={"worth_parent_context": 1.0}), "summary", id="mid-worth"),
    pytest.param(FakeVerdict(scores={"worth_parent_context": 2.0}), "full", id="high-worth"),
]


@pytest.mark.parametrize("verdict,expected_level", MERGE_CASES)
def test_classify_merge_worth_parent_context(verdict, expected_level):
    level, contradicts = classify_merge(verdict)
    assert level == expected_level
    assert contradicts is False


def test_classify_merge_contradiction_forces_full_regardless_of_worth():
    verdict = FakeVerdict(
        nouls={"contradicts_siblings": 0.9}, scores={"worth_parent_context": 0.0}
    )
    level, contradicts = classify_merge(verdict)
    assert level == "full"
    assert contradicts is True


# ---------------------------------------------------------------------
# Pre-turn brief gate: classify_brief
# ---------------------------------------------------------------------


def test_classify_brief_none_verdict_is_false():
    assert classify_brief(None) is False


BRIEF_CASES = [
    pytest.param(
        FakeVerdict(
            nouls={
                "has_done_statement": 0.2,
                "has_material_gaps": 0.1,
                "is_already_actionable": 0.2,
                "is_conversational": 0.1,
            }
        ),
        True,
        id="missing-done-interviews",
    ),
    pytest.param(
        FakeVerdict(
            nouls={
                "has_done_statement": 0.9,
                "has_material_gaps": 0.8,
                "is_already_actionable": 0.2,
                "is_conversational": 0.1,
            }
        ),
        True,
        id="material-gap-interviews",
    ),
    pytest.param(
        FakeVerdict(
            nouls={
                "has_done_statement": 0.1,
                "has_material_gaps": 0.9,
                "is_already_actionable": 0.85,
                "is_conversational": 0.1,
            }
        ),
        False,
        id="actionable-skips",
    ),
    pytest.param(
        FakeVerdict(
            nouls={
                "has_done_statement": 0.1,
                "has_material_gaps": 0.9,
                "is_already_actionable": 0.2,
                "is_conversational": 0.8,
            }
        ),
        False,
        id="conversational-skips",
    ),
    pytest.param(
        FakeVerdict(
            nouls={
                "has_done_statement": 0.5,
                "has_material_gaps": 0.5,
                "is_already_actionable": 0.7,
                "is_conversational": 0.5,
            }
        ),
        False,
        id="at-thresholds-does-not-interview",
    ),
    pytest.param(
        FakeVerdict(
            nouls={
                "has_done_statement": 0.49,
                "has_material_gaps": 0.5,
                "is_already_actionable": 0.7,
                "is_conversational": 0.5,
            }
        ),
        True,
        id="done-below-floor-interviews",
    ),
]


@pytest.mark.parametrize("verdict,expected", BRIEF_CASES)
def test_classify_brief(verdict, expected):
    assert classify_brief(verdict) is expected
