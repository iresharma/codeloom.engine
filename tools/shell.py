from __future__ import annotations

from runtime.judge import Noul, Score
from runtime.judge_decisions import classify_exec, exec_reason, exec_signals
from runtime.tools.shell import (
    DEFAULT_TIMEOUT,
    format_command_result,
)
from runtime.tools.shell import (
    run_command as run_command_impl,
)
from tools.base import ToolContext, tool


def _exec_questions() -> dict:
    # Built lazily (only once the judge is confirmed enabled) so that a
    # missing typesafe-sdk package cannot break importing this module and
    # taking the whole run_command tool down with it.
    return {
        "is_read_only": Noul(
            instructions="Does `command` only inspect state, without writing, deleting, installing, or transmitting?"
        ),
        "is_destructive": Noul(
            instructions="Would `command` delete or irreversibly overwrite existing data?"
        ),
        "escapes_workspace": Noul(
            instructions="Would `command` read or modify files outside `workspace`?"
        ),
        "touches_network": Noul(
            instructions="Does `command` fetch from or transmit to the network?"
        ),
        "executes_fetched_code": Noul(
            instructions="Does `command` pipe downloaded content into a shell or interpreter?"
        ),
        "rewrites_vcs_history": Noul(
            instructions="Would `command` rewrite or force-overwrite version control history?"
        ),
        "exfiltrates_secrets": Noul(
            instructions="Would `command` read credentials or environment secrets and send them somewhere?"
        ),
        "matches_user_request": Noul(
            instructions="Is `command` a plausible step toward `user_request`?"
        ),
        "blast_radius": Score(
            instructions="How far do the effects of `command` reach?",
            criteria=[
                "Single file or directory inside the workspace",
                "The whole workspace or project",
                "The user's machine or remote systems",
            ],
        ),
    }


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

    reason_suffix = ""
    if approval == "judged":
        approval, reason_suffix = await _judged_decision(ctx, config, command.strip())

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
        return f"error: refused — command judged unsafe ({reason_suffix or 'no reason given'})"

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


async def _judged_decision(ctx: ToolContext, config, command: str) -> tuple[str, str]:
    """Resolve ENGINE_EXEC_APPROVAL=judged into a concrete auto|always|never
    (or "blocked") decision, emitting JudgementMade when a verdict was used.

    Returns (approval, reason). `approval` is one of "never" (run without
    asking — judge said allow), "always" (force the approval prompt — judge
    said prompt), "blocked" (refuse outright), or "auto"/"never" from the
    legacy fallback when no verdict was available.
    """
    site_mode = config.judge_mode_for("exec") if config is not None else "off"
    judge = getattr(ctx, "judge", None)
    verdict = None
    if site_mode != "off" and judge is not None and getattr(judge, "enabled", False):
        state = {
            "command": command,
            "workspace": str(ctx.workspace),
            "cwd": "",
            "user_request": ctx.user_request,
        }
        verdict = await judge.ask(state, _exec_questions(), tag="exec_approval")

    decision = classify_exec(verdict, command)
    reason = ""
    if verdict is not None:
        enforced = site_mode == "enforcing"
        reason = exec_reason(verdict)
        effective = decision if enforced else classify_exec(None, command)
        if ctx.on_judgement is not None:
            ctx.on_judgement(
                tag="exec_approval",
                subject=command[:200],
                outcome=decision,
                signals=exec_signals(verdict),
                enforced=enforced,
                latency_ms=verdict.latency_ms,
                agent_id=ctx.agent_id,
            )
        decision = effective

    if decision == "block":
        return "blocked", reason
    if decision == "allow":
        return "never", reason
    return "always", reason
