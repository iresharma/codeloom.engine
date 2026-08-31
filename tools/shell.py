from __future__ import annotations

from tools.base import ToolContext, tool
from runtime.tools.shell import (
    DEFAULT_TIMEOUT,
    format_command_result,
    run_command as run_command_impl,
)


@tool(
    description=(
        "Run a shell command in the workspace. No TTY, default 120s timeout. "
        "A non-zero exit code is information — read the output. Some commands "
        "ask the user for approval."
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to run.",
            },
            "cwd": {
                "type": "string",
                "description": "Optional workspace-relative working directory.",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default 120, max 600).",
            },
        },
        "required": ["command"],
    },
)
async def run_command(
    ctx: ToolContext,
    command: str,
    cwd: str = "",
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    config = ctx.config
    approval = getattr(config, "exec_approval", "auto") if config else "auto"
    file_limit = getattr(config, "exec_file_limit_mb", 2048) if config else 2048
    if config is not None:
        timeout = min(timeout, getattr(config, "exec_timeout_s", timeout) or timeout)

    async def approve(question: str, kind: str) -> str:
        if ctx.ask_user is None:
            return "no"
        return await ctx.ask_user(question, kind=kind)

    def on_output(stream: str, text: str) -> None:
        if ctx.on_output is not None:
            ctx.on_output("", stream, text)

    result = await run_command_impl(
        ctx.workspace,
        command,
        cwd=cwd,
        timeout=timeout,
        on_output=on_output,
        approve=approve,
        approval=approval,
        file_limit_mb=file_limit,
        on_proc=ctx.on_proc,
    )
    return format_command_result(result)
