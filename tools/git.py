from __future__ import annotations

from tools.base import ToolContext, tool
from runtime.tools.git import git_blame as blame_impl
from runtime.tools.git import git_log as log_impl
from runtime.tools.git import git_range as range_impl
from runtime.tools.git import git_show as show_impl
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


@tool(
    description="Recent commits, one per line (hash + subject). Optional path filter.",
    parameters={
        "type": "object",
        "properties": {
            "max": {
                "type": "integer",
                "description": "Number of commits (default 20, max 50).",
            },
            "path": {
                "type": "string",
                "description": "Optional workspace-relative path to filter history.",
            },
        },
    },
)
def git_log(ctx: ToolContext, max: int = 20, path: str = "") -> str:
    return log_impl(ctx.workspace, max_count=max, path=path)


@tool(
    description="Show one revision: metadata, stat, and a clipped patch.",
    parameters={
        "type": "object",
        "properties": {
            "rev": {
                "type": "string",
                "description": "Commit, tag, or ref to show.",
            }
        },
        "required": ["rev"],
    },
)
def git_show(ctx: ToolContext, rev: str) -> str:
    return show_impl(ctx.workspace, rev)


@tool(
    description="Blame a file. Optional inclusive 1-based line window.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative file path."},
            "start_line": {
                "type": "integer",
                "description": "1-based start line. 0 means the whole file.",
            },
            "end_line": {
                "type": "integer",
                "description": "1-based end line. 0 with start_line means that one line.",
            },
        },
        "required": ["path"],
    },
)
def git_blame(
    ctx: ToolContext, path: str, start_line: int = 0, end_line: int = 0
) -> str:
    return blame_impl(ctx.workspace, path, start_line=start_line, end_line=end_line)


@tool(
    description="Commits and diffstat for base...head (symmetric difference).",
    parameters={
        "type": "object",
        "properties": {
            "base": {"type": "string", "description": "Base ref (e.g. main)."},
            "head": {"type": "string", "description": "Head ref (e.g. HEAD or a branch)."},
        },
        "required": ["base", "head"],
    },
)
def git_range(ctx: ToolContext, base: str, head: str) -> str:
    return range_impl(ctx.workspace, base, head)
