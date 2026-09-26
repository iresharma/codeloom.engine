"""The rules added to the system prompts after the trial runs are still there.

Each rule answers something a run actually did; the comment on each says what.
These assert on the *rendered* prompt each agent receives (the subagent
appends its own sections; the orchestrator is built for real), not on module
constants, so a refactor that stops threading a prompt through fails here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.orchestrator import Orchestrator
from agents.profile import discover_profiles
from agents.subagent import Subagent
from runtime.config import EngineConfig
from tests.fakes import FakeProvider
from tools.registry import discover_tools


def _sub(name: str, tmp_path: Path) -> str:
    profile = discover_profiles().get(name)
    child = Subagent(
        profile,
        llm=FakeProvider(),
        tools=discover_tools().subset(profile.tool_names, profile=profile.name),
        workspace=tmp_path,
        config=EngineConfig(),
    )
    return child._join_system(child._system_parts())


def _orch(tmp_path: Path) -> str:
    orch = Orchestrator(
        FakeProvider(),
        all_tools=discover_tools(),
        profiles=discover_profiles(),
        workspace=tmp_path,
        config=EngineConfig(),
    )
    return orch._join_system(orch._system_parts())


ORCHESTRATOR = [
    # requirements were softened into "TODOs are fine" in the brief
    "Requirements (verbatim from the user)",
    "do not write \"TODOs are fine\"",
    # the survey dismissed the agent that calls the collector
    "the consumers are part of the task",
    # a forced request_changes was shipped anyway, twice
    "review_verdict: request_changes",
    "the verdict is binding",
    "Never brief a reviewer with the outcome you expect",
    # "no verdict" was reported for a stopped reviewer that had a report
    "A reviewer that returns status=stopped still leaves a report",
    # a failure that predated the change pulled a coder into an RPC refactor
    "not the change's",
    "do not widen the task to make a build pass",
    # "a partial fix was already applied" when no edits existed
    "unless a child's report or describe_worktrees shows it",
    # a stray ask agent with the task "placeholder"
    "Never spawn an agent with a placeholder task",
    # the run ended on a menu of options and it became the PR text
    "Never end a message with a menu of options",
    "could be pasted into a pull request as is",
]
ASK = [
    # `search '.'` used as a directory lister, in three runs
    'A pattern such as "." or "\\.go:" matches every line',
    # the ingest client was seen and dismissed as unrelated
    "find its consumers before you report",
    "That includes other programs in the same repository",
]
CODER = [
    # unrelated go vet failures led to a repo-wide refactor
    "it predates your change",
    "Do not refactor unrelated code",
    # the forwarder was never updated to send the new token
    "Search the whole repo for its consumers",
    # committed changeme credentials; non-optional secretKeyRefs
    'Never commit working placeholder credentials',
    "mark new secret or config references optional",
    # blocking time.sleep inside an async tool
    "Do not add a blocking call",
    # `| head` masked vet's exit code, and `2>&1 > file` read as clean
    "Never pipe such a command through head",
    "`| head`, `2>&1 > file`",
    # an 87-second `find /`
    "never search from / or through a module or package cache",
]
REVIEWER = [
    # a reviewer stopped by the loop judge for repeating one search three times
    "Never run the same search twice",
    "report what you have",
    # "ingest path still works" marked met on unit-test evidence alone
    "Check integration, not only the diff",
    "sends what the server now demands",
    # committed placeholder credentials, non-optional secret references
    "no committed placeholder credentials that actually authenticate",
]


@pytest.mark.parametrize("phrase", ORCHESTRATOR)
def test_orchestrator_prompt(phrase, tmp_path):
    assert phrase in _orch(tmp_path)


@pytest.mark.parametrize("phrase", ASK)
def test_ask_prompt(phrase, tmp_path):
    assert phrase in _sub("ask", tmp_path)


@pytest.mark.parametrize("phrase", CODER)
def test_coder_prompt(phrase, tmp_path):
    assert phrase in _sub("coder", tmp_path)


@pytest.mark.parametrize("phrase", REVIEWER)
def test_reviewer_prompt(phrase, tmp_path):
    assert phrase in _sub("reviewer", tmp_path)


def test_the_orchestrator_tool_set_is_unchanged_by_the_prompt_work(tmp_path):
    """Prompt-only change: no profile gained or lost a tool."""
    profiles = discover_profiles()
    assert "run_command" not in profiles.get("reviewer").tool_names
    assert "run_verify" in profiles.get("reviewer").tool_names
    assert "run_command" in profiles.get("coder").tool_names
