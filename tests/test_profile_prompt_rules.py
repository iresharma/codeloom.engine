"""Item 4: the mock-at-the-boundary rules are in the prompts that ship.

Asserted against the *rendered* system prompt each profile's agent actually
receives (Subagent appends REPORT_TO_ORCH and the memory/skills sections),
not against the module constant -- so a refactor that stops threading the
profile prompt through fails here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.profile import discover_profiles
from agents.subagent import Subagent
from runtime.config import EngineConfig
from tests.fakes import FakeProvider
from tools.registry import discover_tools

# Phrases, not whole paragraphs: enough to pin the rule, loose enough that
# rewording the surrounding prose does not fail the test for no reason.
BOUNDARY_RULES = (
    "Mock at the boundary",
    "never the output of the function you are testing",
    "at least one test must fail if that branch is deleted",
    "comparison is flipped",
)
REVIEWER_RULES = (
    "would any test fail if the new logic were broken",
    "name the test that covers it",
)


def _rendered(profile_name: str, tmp_path: Path) -> str:
    profiles = discover_profiles()
    profile = profiles.get(profile_name)
    child = Subagent(
        profile,
        llm=FakeProvider(),
        tools=discover_tools().subset(profile.tool_names, profile=profile.name),
        workspace=tmp_path,
        config=EngineConfig(),
    )
    return child._join_system(child._system_parts())


@pytest.mark.parametrize("profile_name", ["coder", "tester"])
@pytest.mark.parametrize("rule", BOUNDARY_RULES)
def test_writer_prompts_carry_the_boundary_rule(profile_name, rule, tmp_path):
    assert rule in _rendered(profile_name, tmp_path)


@pytest.mark.parametrize("rule", REVIEWER_RULES)
def test_reviewer_prompt_carries_the_coverage_checklist(rule, tmp_path):
    assert rule.lower() in _rendered("reviewer", tmp_path).lower()


def test_the_rule_is_identical_in_coder_and_tester(tmp_path):
    from agents.profiles.coder import CODER_SYSTEM
    from agents.profiles.tester import TESTER_SYSTEM

    head = "Mock at the boundary"
    coder = CODER_SYSTEM[CODER_SYSTEM.index(head) :].split("\n\n", 1)[0]
    tester = TESTER_SYSTEM[TESTER_SYSTEM.index(head) :].split("\n\n", 1)[0]
    assert coder == tester
