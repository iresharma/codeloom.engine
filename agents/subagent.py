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
        # Orch already runs tools in parallel; children were False only because
        # AgentLoop defaulted that way. gather keeps tool_call order; writers
        # serialize via write_lock.
        kwargs.setdefault("concurrent_tools", True)
        # Freeze system (prompt + memory + skills) on first build so cache
        # prefixes stay identical. remember() during this run is in the tool
        # result, not a rewritten system prompt. Siblings' notes wait for orch.
        kwargs.setdefault("freeze_system", True)
        super().__init__(**kwargs)
        self._profile = profile
        if profile.max_turns:
            self._config.max_turns = profile.max_turns

    async def finish(self, status: str) -> AgentResult:
        async def complete(payload):
            extra = {"model": self._model} if self._model else {}
            return await self._llm.complete(payload, **extra)

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
                status=status,
                summary="",
                outcome=f"compaction error: {exc}",
                files_touched=list(self._files_touched),
            )
