from __future__ import annotations

from runtime.tools.shell import _hard_deny
from runtime.tools.server import server_logs as server_logs_impl
from runtime.tools.server import start_server as start_server_impl
from runtime.tools.server import stop_server as stop_server_impl
from tools.base import ToolContext, tool
from tools.shell import _judged_decision


@tool(
    description=(
        "Start a long-running server in this worktree and wait until a TCP "
        "port on 127.0.0.1 accepts connections. One server per worktree; a "
        "new start replaces the previous one. Sets HOST=127.0.0.1 and PORT. "
        "If the framework ignores PORT, pass the port in the command. "
        "Then browser_open or http_request that URL. Asks for approval."
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command that starts the server.",
            },
            "port": {
                "type": "integer",
                "description": (
                    "Listen port. Omit to pick a free port (exported as PORT)."
                ),
            },
            "cwd": {
                "type": "string",
                "description": "Optional workspace-relative working directory.",
            },
        },
        "required": ["command"],
    },
)
async def start_server(
    ctx: ToolContext, command: str, port: int = 0, cwd: str = ""
) -> str:
    config = ctx.config
    approval = getattr(config, "exec_approval", "auto") if config else "auto"
    file_limit = getattr(config, "exec_file_limit_mb", 2048) if config else 2048
    stripped = command.strip()
    _hard_deny(stripped)
    from runtime.tools.toolchain import deny_agent_command

    deny_agent_command(ctx.workspace, stripped)

    reason_suffix = ""
    if approval == "judged":
        approval, reason_suffix = await _judged_decision(ctx, config, stripped, cwd)

    async def approve(question: str, kind: str) -> str:
        if ctx.ask_user is None:
            return "no"
        if reason_suffix:
            question = f"{question}\n(judge flagged: {reason_suffix})"
        return await ctx.ask_user(question, kind=kind)

    def on_output(stream: str, text: str) -> None:
        if ctx.on_output is not None:
            ctx.on_output("", stream, text)

    if approval == "blocked":
        return (
            "error: refused — command judged unsafe "
            f"({reason_suffix or 'no reason given'})"
        )

    return await start_server_impl(
        ctx.workspace,
        command,
        port=port,
        cwd=cwd,
        approve=approve,
        approval=approval,
        file_limit_mb=file_limit,
        on_proc=ctx.on_proc,
        on_output=on_output,
    )


@tool(
    description="Stop the long-running server for this worktree.",
    parameters={"type": "object", "properties": {}},
)
async def stop_server(ctx: ToolContext) -> str:
    return await stop_server_impl(ctx.workspace)


@tool(
    description="Recent stdout/stderr from this worktree's long-running server.",
    parameters={"type": "object", "properties": {}},
)
async def server_logs(ctx: ToolContext) -> str:
    return await server_logs_impl(ctx.workspace)
