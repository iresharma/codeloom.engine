from __future__ import annotations

import asyncio

from tools.base import BaseTool, ToolContext, ToolResult
from tools.paths import resolve_workspace_path


class RunCommand(BaseTool):
    name = "run_command"
    description = "Run a shell command in the workspace. Captures stdout and stderr. No TTY."
    parameters = {
        "type": "object",
        "properties": {
            "cmd": {"type": "string"},
            "cwd": {"type": "string", "description": "Optional directory relative to the workspace"},
            "timeout": {"type": "number", "default": 60},
        },
        "required": ["cmd"],
    }
    allowed_roles = {"orchestrator", "subagent"}

    async def execute(self, ctx: ToolContext, **kwargs) -> ToolResult:
        command = str(kwargs["cmd"])
        cwd = ctx.workspace
        if kwargs.get("cwd"):
            cwd = resolve_workspace_path(ctx.workspace, str(kwargs["cwd"]))
        timeout = float(kwargs.get("timeout") or 60)
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            return ToolResult(ok=False, content=f"command timed out after {timeout}s")
        text = ""
        if stdout:
            text += stdout.decode("utf-8", errors="replace")
        if stderr:
            text += ("\n" if text else "") + stderr.decode("utf-8", errors="replace")
        return ToolResult(
            ok=(process.returncode or 0) == 0,
            content=text.strip() or f"exit {process.returncode}",
            data={"code": process.returncode, "cwd": str(cwd)},
        )


def executor_tools() -> list[BaseTool]:
    return [RunCommand()]
