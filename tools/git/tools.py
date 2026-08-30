from __future__ import annotations

from tools.base import BaseTool, ToolContext, ToolResult
from tools.git.runner import run_git


async def _git(ctx: ToolContext, *args: str) -> ToolResult:
    code, stdout, stderr = await run_git(ctx.workspace, *args)
    text = stdout if stdout.strip() else stderr
    if ctx.refresh:
        await ctx.refresh()
    return ToolResult(ok=code == 0, content=text.strip() or "(empty)", data={"code": code})


class GitStatus(BaseTool):
    name = "git_status"
    description = "Show git status --porcelain and the current branch."
    parameters = {"type": "object", "properties": {}}
    allowed_roles = {"orchestrator", "subagent"}

    async def execute(self, ctx: ToolContext, **kwargs) -> ToolResult:
        del kwargs
        code, branch, _ = await run_git(ctx.workspace, "rev-parse", "--abbrev-ref", "HEAD")
        if code != 0:
            return ToolResult(ok=False, content="not a git repository")
        _, status, _ = await run_git(ctx.workspace, "status", "--porcelain")
        return ToolResult(
            ok=True,
            content=f"branch {branch.strip()}\n{status}".strip(),
            data={"branch": branch.strip(), "status": status},
        )


class GitDiff(BaseTool):
    name = "git_diff"
    description = "Show a git diff. Set staged=true for the index."
    parameters = {
        "type": "object",
        "properties": {
            "staged": {"type": "boolean", "default": False},
            "path": {"type": "string"},
        },
    }
    allowed_roles = {"orchestrator", "subagent"}

    async def execute(self, ctx: ToolContext, **kwargs) -> ToolResult:
        args = ["diff"]
        if kwargs.get("staged"):
            args.append("--cached")
        if kwargs.get("path"):
            args.extend(["--", str(kwargs["path"])])
        return await _git(ctx, *args)


class GitLog(BaseTool):
    name = "git_log"
    description = "Show recent git commits."
    parameters = {
        "type": "object",
        "properties": {
            "max_count": {"type": "integer", "default": 15},
        },
    }
    allowed_roles = {"orchestrator", "subagent"}

    async def execute(self, ctx: ToolContext, **kwargs) -> ToolResult:
        count = int(kwargs.get("max_count") or 15)
        return await _git(ctx, "log", f"-{count}", "--oneline", "--decorate")


class GitBranch(BaseTool):
    name = "git_branch"
    description = "List local git branches."
    parameters = {"type": "object", "properties": {}}
    allowed_roles = {"orchestrator", "subagent"}

    async def execute(self, ctx: ToolContext, **kwargs) -> ToolResult:
        del kwargs
        return await _git(ctx, "branch", "--list")


class GitAdd(BaseTool):
    name = "git_add"
    description = "Stage files with git add."
    parameters = {
        "type": "object",
        "properties": {
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Paths to stage. Use ['.'] for all.",
            }
        },
        "required": ["paths"],
    }
    allowed_roles = {"orchestrator", "subagent"}

    async def execute(self, ctx: ToolContext, **kwargs) -> ToolResult:
        paths = [str(item) for item in kwargs.get("paths") or []]
        if not paths:
            return ToolResult(ok=False, content="paths is required")
        return await _git(ctx, "add", "--", *paths)


class GitCommit(BaseTool):
    name = "git_commit"
    description = "Create a git commit with the given message."
    parameters = {
        "type": "object",
        "properties": {
            "message": {"type": "string"},
        },
        "required": ["message"],
    }
    allowed_roles = {"orchestrator", "subagent"}

    async def execute(self, ctx: ToolContext, **kwargs) -> ToolResult:
        message = str(kwargs.get("message") or "").strip()
        if not message:
            return ToolResult(ok=False, content="commit message is required")
        return await _git(ctx, "commit", "-m", message)


def git_tools() -> list[BaseTool]:
    return [GitStatus(), GitDiff(), GitLog(), GitBranch(), GitAdd(), GitCommit()]
