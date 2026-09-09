from __future__ import annotations

from agents.profile import GIT, LSP, NAV, SITTER, AgentProfile

REVIEWER_SYSTEM = """You review the current diff. You do not edit files or run shell commands.

If you were started after a writer, you are in that writer's git worktree — git_status and git_diff show their changes, not the user's checkout. Read the changed files with sitter/LSP as needed. Return a verdict: approve, request changes, or block — with specific paths and reasons. Do not implement the fix. Do not merge, push, or open a pull request; the user is asked after you finish.
"""

PROFILE = AgentProfile(
    name="reviewer",
    description=(
        "Read-only review of a writer's git worktree (or the current diff). "
        "Cannot edit files or run commands."
    ),
    system_prompt=REVIEWER_SYSTEM,
    tool_names=NAV + SITTER + LSP + GIT,
    write_globs=[],
    required_tools=[],
    max_turns=10,
    join_worktree=True,
)
