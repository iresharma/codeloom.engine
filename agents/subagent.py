from __future__ import annotations

from typing import Any

from agents.agent_loop import AgentLoop, AgentResult
from agents.profile import AgentProfile
from llm.provider import LLMProvider
from tools.registry import ToolRegistry


class Subagent(AgentLoop):
    def __init__(
        self,
        profile: AgentProfile,
        tools: ToolRegistry,
        session: Any,
        llm: LLMProvider,
        *,
        agent_id: str,
        parent_id: str,
    ) -> None:
        super().__init__(
            profile,
            tools,
            session,
            llm,
            agent_id=agent_id,
            role="subagent",
            parent_id=parent_id,
        )

    async def run(self, task: str) -> AgentResult:
        return await super().run(task)

    def finish(self, result: AgentResult) -> AgentResult:
        self.status = result.status
        self.current_tool = None
        if self.session is not None:
            self.session.track_agent(self.row())
        return result
