from __future__ import annotations

import asyncio

from agents.agent_loop import AgentLoop
from agents.compactor import AgentResult, compress_for_parent
from agents.profile import (
    REPORT_TO_ORCH,
    AgentProfile,
    is_code_path,
    is_test_path,
    repo_has_tests,
)

CLOSING_REPORT = (
    "Then send your full closing report again: paths changed, reasoning:, "
    "test_plan:, and the checks you ran."
)
FINISH_MISSING_TOOLS = "Not finished yet: call {tools} first. " + CLOSING_REPORT
FINISH_NEEDS_TEST = (
    "Not finished yet: this repo has tests, and you changed source ({paths}) "
    "without adding or updating one. Add or extend a test next to the existing "
    "ones that covers this change, run it, and fix what fails. " + CLOSING_REPORT
)


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
        self._repo_has_tests: bool | None = None
        if profile.max_turns:
            self._config.max_turns = profile.max_turns

    async def _finish_nudge(self) -> str | None:
        missing = [
            name for name in self._profile.required_tools if name not in self._tools_called
        ]
        if missing:
            return FINISH_MISSING_TOOLS.format(tools=", ".join(missing))
        if self._profile.requires_tests:
            source = await self._untested_source()
            if source:
                return FINISH_NEEDS_TEST.format(paths=", ".join(source[:5]))
        return None

    async def _untested_source(self) -> list[str]:
        touched = list(self._files_touched)
        if not touched or any(is_test_path(path) for path in touched):
            return []
        source = [path for path in touched if is_code_path(path)]
        if not source:
            return []
        if self._repo_has_tests is None:
            self._repo_has_tests = await asyncio.to_thread(
                repo_has_tests, self._ctx.workspace
            )
        return source if self._repo_has_tests else []

    async def finish(self, status: str) -> AgentResult:
        async def complete(payload):
            return await self._llm_complete(payload)

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
