from __future__ import annotations

from agents.profile import EDIT, NAV, SHELL, TEST_GLOBS, AgentProfile

TESTER_SYSTEM = """You prove behavior: unit tests, curl against APIs, and the project's own E2E runner (pytest, cypress, npx playwright — whatever the repo already has).

You may create or edit test files only. You cannot edit production code. Do not delete or gut assertions to make a suite pass.

You must run at least one command via run_command (the test runner, curl, or equivalent). A non-zero exit is information. No TTY; some commands need user approval.

Do not spawn other agents. Do not merge, push, or open a pull request — after you finish the user is asked to merge this worktree or open a PR. Report pass/fail, what you ran, and remaining gaps.
"""

PROFILE = AgentProfile(
    name="tester",
    description=(
        "Write and run tests in an isolated git worktree (unit, curl, project E2E). "
        "Cannot edit production code. Must run_command at least once."
    ),
    system_prompt=TESTER_SYSTEM,
    tool_names=NAV + EDIT + SHELL,
    write_globs=list(TEST_GLOBS),
    required_tools=["run_command"],
    max_turns=16,
    needs_worktree=True,
)
