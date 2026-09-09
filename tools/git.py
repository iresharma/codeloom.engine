from __future__ import annotations

from tools.base import ToolContext, tool
from runtime.tools.git import read_state


@tool(
    description="Git status: branch, dirty flag, staged/unstaged/untracked paths.",
    parameters={
        "type": "object",
        "properties": {},
    },
)
def git_status(ctx: ToolContext) -> str:
    state = read_state(ctx.workspace, diffs=False)
    if state.branch is None and not state.dirty:
        return "not a git repository"
    lines = [
        f"branch: {state.branch or '(unknown)'}",
        f"dirty: {state.dirty}",
        "staged: " + (", ".join(state.staged) or "(none)"),
        "unstaged: " + (", ".join(state.unstaged) or "(none)"),
        "untracked: " + (", ".join(state.untracked) or "(none)"),
    ]
    return "\n".join(lines)


@tool(
    description="Git diffs. staged=true for index (cached), false for worktree.",
    parameters={
        "type": "object",
        "properties": {
            "staged": {
                "type": "boolean",
                "description": "If true, show staged (cached) diff. Default false (worktree).",
            }
        },
    },
)
def git_diff(ctx: ToolContext, staged: bool = False) -> str:
    state = read_state(ctx.workspace, diffs=True)
    if state.branch is None and not state.dirty:
        return "not a git repository"
    diff = state.staged_diff if staged else state.unstaged_diff
    if not diff.strip():
        return "(no diff)"
    return diff
