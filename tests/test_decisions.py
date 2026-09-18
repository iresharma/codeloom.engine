"""Table-driven tests for the pure decision functions in runtime/judge_decisions.py.

These are the most valuable tests in the plan: thresholds get tuned
repeatedly against real traces, and this file is the regression harness
that tuning has to keep green.
"""

from __future__ import annotations

import pytest

from runtime.judge_decisions import classify_exec, legacy_exec_policy
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
        FakeVerdict(nouls={"is_read_only": 0.9}, scores={"blast_radius": 0.9}),
        "prompt",
        id="read-only-but-wide-blast-radius-does-not-allow",
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
