from __future__ import annotations

import json

from tools.base import BaseTool, ToolContext, ToolResult


class AskUser(BaseTool):
    name = "ask_user"
    description = "Ask the human a question and wait for their reply."
    parameters = {
        "type": "object",
        "properties": {
            "question": {"type": "string"},
        },
        "required": ["question"],
    }
    allowed_roles = {"orchestrator"}

    async def execute(self, ctx: ToolContext, **kwargs) -> ToolResult:
        if ctx.orchestrator is None:
            return ToolResult(ok=False, content="orchestrator is not available")
        answer = await ctx.orchestrator.request_user_input(str(kwargs["question"]))
        return ToolResult(ok=True, content=answer)


class SpawnSubagent(BaseTool):
    name = "spawn_subagent"
    description = (
        "Start a subagent with a named profile (ask, linter, editor, reviewer) "
        "and a task prompt. Returns the new agent id."
    )
    parameters = {
        "type": "object",
        "properties": {
            "profile_name": {"type": "string"},
            "task": {"type": "string"},
        },
        "required": ["profile_name", "task"],
    }
    allowed_roles = {"orchestrator"}

    async def execute(self, ctx: ToolContext, **kwargs) -> ToolResult:
        if ctx.orchestrator is None:
            return ToolResult(ok=False, content="orchestrator is not available")
        agent_id = await ctx.orchestrator.spawn_subagent(
            str(kwargs["profile_name"]),
            str(kwargs["task"]),
        )
        return ToolResult(ok=True, content=f"started {agent_id}", data={"agent_id": agent_id})


class ListProfiles(BaseTool):
    name = "list_profiles"
    description = "List named subagent profiles the orchestrator can spawn."
    parameters = {"type": "object", "properties": {}}
    allowed_roles = {"orchestrator"}

    async def execute(self, ctx: ToolContext, **kwargs) -> ToolResult:
        del kwargs
        if ctx.orchestrator is None:
            return ToolResult(ok=False, content="orchestrator is not available")
        rows = [
            {"name": profile.name, "description": profile.description}
            for profile in ctx.orchestrator.profiles.list()
        ]
        return ToolResult(ok=True, content=json.dumps(rows, indent=2), data={"profiles": rows})


class WriteContext(BaseTool):
    name = "write_context"
    description = "Append a note to the orchestrator's long-term context file."
    parameters = {
        "type": "object",
        "properties": {
            "note": {"type": "string"},
        },
        "required": ["note"],
    }
    allowed_roles = {"orchestrator"}

    async def execute(self, ctx: ToolContext, **kwargs) -> ToolResult:
        if ctx.orchestrator is None:
            return ToolResult(ok=False, content="orchestrator is not available")
        ctx.orchestrator.write_context(str(kwargs["note"]))
        return ToolResult(ok=True, content="context updated")


class ListAgents(BaseTool):
    name = "list_agents"
    description = "List agents currently tracked by the session."
    parameters = {"type": "object", "properties": {}}
    allowed_roles = {"orchestrator"}

    async def execute(self, ctx: ToolContext, **kwargs) -> ToolResult:
        del kwargs
        if ctx.session is None:
            return ToolResult(ok=False, content="session is not available")
        rows = [agent.to_json() for agent in ctx.session.state.agents.values()]
        return ToolResult(ok=True, content=json.dumps(rows, indent=2), data={"agents": rows})


class AbortAgent(BaseTool):
    name = "abort_agent"
    description = "Abort one subagent, or the whole run if agent_id is omitted."
    parameters = {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string"},
        },
    }
    allowed_roles = {"orchestrator"}

    async def execute(self, ctx: ToolContext, **kwargs) -> ToolResult:
        if ctx.session is None:
            return ToolResult(ok=False, content="session is not available")
        await ctx.session.abort(kwargs.get("agent_id"))
        return ToolResult(ok=True, content="abort requested")


def admin_tools() -> list[BaseTool]:
    return [AskUser(), SpawnSubagent(), ListProfiles(), WriteContext(), ListAgents(), AbortAgent()]
