"""Table-driven tests for the pure decision functions in runtime/judge_decisions.py.

These are the most valuable tests in the plan: thresholds get tuned
repeatedly against real traces, and this file is the regression harness
that tuning has to keep green.
"""

from __future__ import annotations

import pytest

from runtime.judge_decisions import (
    classify_exec,
    classify_intent,
    classify_merge,
    classify_meta_action,
    classify_write,
    legacy_exec_policy,
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


# ---------------------------------------------------------------------
# Phase 5: classify_intent / classify_meta_action
# ---------------------------------------------------------------------


def test_classify_intent_none_verdict_is_default():
    assert classify_intent(None) == "default"


def test_classify_intent_ambiguous_wins_over_everything_else():
    verdict = FakeVerdict(
        nouls={"is_ambiguous": 0.9},
        choices={"intent": "locate"},
        confidences={"intent": 0.99},
    )
    assert classify_intent(verdict) == "ambiguous"


@pytest.mark.parametrize(
    "verdict,expected",
    [
        pytest.param(
            FakeVerdict(choices={"intent": "meta"}, confidences={"intent": 0.9}),
            "meta",
            id="meta-above-confidence-floor",
        ),
        pytest.param(
            FakeVerdict(choices={"intent": "meta"}, confidences={"intent": 0.5}),
            "default",
            id="meta-below-confidence-floor",
        ),
        pytest.param(
            FakeVerdict(choices={"intent": "locate"}, confidences={"intent": 0.85}),
            "locate",
            id="locate-above-confidence-floor",
        ),
        pytest.param(
            FakeVerdict(choices={"intent": "locate"}, confidences={"intent": 0.5}),
            "default",
            id="locate-below-confidence-floor",
        ),
        pytest.param(
            FakeVerdict(
                choices={"intent": "edit"},
                confidences={"intent": 0.9},
                nouls={"is_multi_file": 0.9},
            ),
            "edit_multi_file",
            id="edit-multi-file",
        ),
        pytest.param(
            FakeVerdict(
                choices={"intent": "edit"},
                confidences={"intent": 0.9},
                nouls={"is_multi_file": 0.1},
            ),
            "default",
            id="edit-single-file-is-default",
        ),
        pytest.param(
            FakeVerdict(choices={"intent": "execute"}, confidences={"intent": 0.99}),
            "default",
            id="execute-is-default",
        ),
    ],
)
def test_classify_intent(verdict, expected):
    assert classify_intent(verdict) == expected


def test_classify_meta_action_none_verdict_is_other():
    assert classify_meta_action(None) == "other"


def test_classify_meta_action_below_confidence_is_other():
    verdict = FakeVerdict(choices={"meta_action": "undo"}, confidences={"meta_action": 0.3})
    assert classify_meta_action(verdict) == "other"


def test_classify_meta_action_confident_action():
    verdict = FakeVerdict(choices={"meta_action": "undo"}, confidences={"meta_action": 0.9})
    assert classify_meta_action(verdict) == "undo"


# ---------------------------------------------------------------------
# Phase 7: classify_write -- only introduces_hardcoded_secret ever blocks
# ---------------------------------------------------------------------


def test_classify_write_none_verdict_allows():
    assert classify_write(None) == ("allow", "")


def test_classify_write_secret_blocks():
    verdict = FakeVerdict(nouls={"introduces_hardcoded_secret": 0.81})
    decision, reason = classify_write(verdict)
    assert decision == "block"
    assert reason


def test_classify_write_secret_at_threshold_does_not_block():
    verdict = FakeVerdict(nouls={"introduces_hardcoded_secret": 0.8})
    decision, _ = classify_write(verdict)
    assert decision != "block"


WRITE_CASES = [
    pytest.param(
        FakeVerdict(nouls={"disables_a_test_or_check": 0.9}),
        "flag",
        id="disables-check-flags-not-blocks",
    ),
    pytest.param(
        FakeVerdict(nouls={"deletes_unrelated_code": 0.9}),
        "flag",
        id="deletes-unrelated-flags",
    ),
    pytest.param(
        FakeVerdict(scores={"scope_creep": 2.0}),
        "flag",
        id="scope-creep-flags",
    ),
    pytest.param(
        FakeVerdict(nouls={"matches_stated_intent": 0.1}),
        "flag",
        id="low-intent-match-flags",
    ),
    pytest.param(
        FakeVerdict(
            nouls={
                "disables_a_test_or_check": 0.99,
                "deletes_unrelated_code": 0.99,
                "matches_stated_intent": 0.0,
            },
            scores={"scope_creep": 2.0},
        ),
        "flag",
        id="every-non-secret-signal-extreme-still-only-flags",
    ),
    pytest.param(
        FakeVerdict(nouls={"matches_stated_intent": 0.9}),
        "allow",
        id="clean-diff-allows",
    ),
]


@pytest.mark.parametrize("verdict,expected", WRITE_CASES)
def test_classify_write(verdict, expected):
    decision, _ = classify_write(verdict)
    assert decision == expected


def test_classify_write_secret_check_runs_before_flag_checks():
    verdict = FakeVerdict(
        nouls={"introduces_hardcoded_secret": 0.9, "matches_stated_intent": 0.9}
    )
    decision, reason = classify_write(verdict)
    assert decision == "block"
    assert "secret" in reason or "credential" in reason


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
