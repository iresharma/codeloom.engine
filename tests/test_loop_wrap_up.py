"""A loop-control stop no longer throws the agent's findings away.

Three of the last four trial runs had a reviewer stopped for repeating
searches, and each time the finding it had already made (an untested
validation, a stale-cache gap) never reached the orchestrator or the PR. On a
stop the agent now gets one tool-less turn to report what it has.

Boundaries mocked: the LLM provider and the judge (FakeJudge). The loop, the
history, and the result compression are real.
"""

from __future__ import annotations

import asyncio

import pytest

from agents.agent_loop import LOOP_WRAP_UP, AgentLoop
from agents.compactor import compress_for_parent, validate_history
from agents.review_verdict import enforce_verdict
from llm.provider import LLMResult, ToolCall
from runtime.config import EngineConfig
from tests.conftest import FakeJudge, FakeVerdict
from tests.fakes import FakeProvider
from tools.base import Tool
from tools.registry import ToolRegistry

REPORT = """verdict: request changes
=== REQUIREMENTS ===
| New()'s auth validation is tested | hard | not met | auth_test.go:1 (no test) |
=== END REQUIREMENTS ===
Not verified: route wiring."""


class _StuckThenReport(FakeProvider):
    """Calls `ping` until the wrap-up nudge arrives, then behaves per `mode`."""

    def __init__(self, mode: str = "report"):
        super().__init__()
        self.mode = mode
        self.seen: list[tuple[list[dict], object]] = []

    async def complete(self, messages, tools=None, *, on_delta=None, **kwargs):
        self.calls += 1
        self.seen.append((list(messages), tools))
        wrapping = messages[-1].get("role") == "user" and messages[-1].get(
            "content"
        ) == LOOP_WRAP_UP
        if wrapping and self.mode == "report":
            return LLMResult(text=REPORT)
        if wrapping and self.mode == "raise":
            raise RuntimeError("provider down")
        if wrapping and self.mode == "empty":
            return LLMResult(text="")
        return LLMResult(
            text="",
            tool_calls=[ToolCall(id=str(self.calls), name="ping", arguments_json="{}")],
        )


def _ping_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="ping",
            description="ping",
            parameters={"type": "object", "properties": {}},
            fn=lambda **_: "pong",
        )
    )
    return registry


def _stopping_loop(tmp_path, provider):
    judge = FakeJudge()
    judge.responses["loop_control"] = FakeVerdict(
        nouls={"repeating_itself": 0.9, "making_progress": 0.05}
    )
    return AgentLoop(
        llm=provider,
        tools=_ping_registry(),
        workspace=tmp_path,
        config=EngineConfig(judge_mode="enforcing", max_turns=16, loop_control_interval=1),
        judge=judge,
    )


def test_a_stopped_agent_reports_what_it_has(tmp_path):
    provider = _StuckThenReport("report")
    loop = _stopping_loop(tmp_path, provider)
    final = asyncio.run(loop.run("review the diff"))

    assert final == REPORT
    # Still a stop -- the orchestrator's rules for status=stopped are unchanged.
    assert loop._exit_status == "stopped"
    assert loop._stopped_by_judge is True
    assert loop._history[-1] == {"role": "assistant", "content": REPORT}
    assert validate_history(loop._history) == []


def test_the_wrap_up_turn_is_told_to_report_not_investigate(tmp_path):
    provider = _StuckThenReport("report")
    asyncio.run(_stopping_loop(tmp_path, provider).run("review the diff"))
    messages, tools = provider.seen[-1]
    assert messages[-1]["content"] == LOOP_WRAP_UP
    assert "Do not call any tools" in LOOP_WRAP_UP
    assert "did NOT get to verify" in LOOP_WRAP_UP
    # Tools stay declared: providers reject a history with tool calls and no
    # tools. The nudge, not the schema, is what forbids using them.
    assert tools


def test_findings_from_a_stopped_reviewer_reach_the_verdict_parser(tmp_path):
    """The point of the change: what used to vanish is now parsed."""
    provider = _StuckThenReport("report")
    loop = _stopping_loop(tmp_path, provider)
    asyncio.run(loop.run("review the diff"))

    result = asyncio.run(
        compress_for_parent(loop._build_messages(), status="stopped")
    )
    assert "auth_test.go" in result.outcome
    verdict = enforce_verdict(result.outcome)
    assert verdict.verdict == "request_changes"
    assert "validation is tested" in verdict.reason


@pytest.mark.parametrize("mode", ["empty", "raise"])
def test_a_failed_wrap_up_behaves_exactly_like_the_old_stop(tmp_path, mode):
    provider = _StuckThenReport(mode)
    loop = _stopping_loop(tmp_path, provider)
    final = asyncio.run(loop.run("review the diff"))

    assert "repeating" in final
    assert loop._exit_status == "stopped"
    # The nudge is not left dangling in history.
    assert all(m.get("content") != LOOP_WRAP_UP for m in loop._history)
    assert validate_history(loop._history) == []


def test_a_model_that_ignores_the_nudge_and_calls_a_tool_gets_the_old_stop(tmp_path):
    # `_AlwaysPing` semantics: tool calls even when told not to.
    class _Defiant(_StuckThenReport):
        async def complete(self, messages, tools=None, *, on_delta=None, **kwargs):
            self.calls += 1
            return LLMResult(
                text="",
                tool_calls=[ToolCall(id=str(self.calls), name="ping", arguments_json="{}")],
            )

    loop = _stopping_loop(tmp_path, _Defiant())
    final = asyncio.run(loop.run("review the diff"))
    assert "repeating" in final
    assert validate_history(loop._history) == []


def test_no_wrap_up_happens_when_the_judge_does_not_stop_the_agent(tmp_path):
    provider = _StuckThenReport("report")
    judge = FakeJudge()
    judge.responses["loop_control"] = FakeVerdict(
        nouls={"repeating_itself": 0.0, "making_progress": 0.9}
    )
    loop = AgentLoop(
        llm=provider,
        tools=_ping_registry(),
        workspace=tmp_path,
        config=EngineConfig(judge_mode="enforcing", max_turns=3, loop_control_interval=1),
        judge=judge,
    )
    asyncio.run(loop.run("do it"))
    assert all(
        msgs[-1].get("content") != LOOP_WRAP_UP for msgs, _tools in provider.seen
    )
