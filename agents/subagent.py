from __future__ import annotations

from agents.agent_loop import AgentLoop
from agents.compactor import AgentResult, compress_for_parent
from agents.profile import REPORT_TO_ORCH, AgentProfile

class Subagent(AgentLoop):
    """Same loop as the orch, no user-facing chat stream, no spawn tools."""

    def __init__(self, profile: AgentProfile, **kwargs):
        kwargs.setdefault("role", "subagent")
        kwargs.setdefault("profile", profile.name)
        base = kwargs.pop("system_prompt", profile.system_prompt)
        kwargs["system_prompt"] = f"{(base or '').rstrip()}\n\n{REPORT_TO_ORCH}"
        kwargs.setdefault("write_globs", profile.write_globs)
        kwargs.setdefault("concurrent_tools", False)
        super().__init__(**kwargs)
        self._profile = profile
        if profile.max_turns:
            self._config.max_turns = profile.max_turns

    async def finish(self, status: str) -> AgentResult:
        async def complete(payload):
            return await self._llm.complete(payload)

        try:
            return await compress_for_parent(
                self._build_messages(),
                complete=complete,
                status=status,
                required_tools=self._profile.required_tools,
                files_touched=list(self._files_touched),
                tools_called=set(self._tools_called),
            )
        except Exception as exc:  # noqa: BLE001
            return AgentResult(
                status="failed",
                summary="",
                outcome=f"error: {exc}",
                files_touched=list(self._files_touched),
            )
