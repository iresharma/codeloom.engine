"""Coverage for agents/orchestrator.py"""
from __future__ import annotations

from pathlib import Path

from agents.orchestrator import (
    Orchestrator,
    ORCH_SYSTEM,
    _PendingSettle,
    _apply_run_status,
)
from agents.compactor import AgentResult
from agents.profile import ProfileRegistry
from tools.registry import ToolRegistry
from tests.fakes import FakeProvider
from llm.provider import LLMResult, Usage


def _make_orchestrator(**kwargs):
    return Orchestrator(
        all_tools=ToolRegistry(),
        profiles=ProfileRegistry(),
        **kwargs,
    )


def test_orch_system_prompt():
    assert "orchestrator" in ORCH_SYSTEM.lower()
    assert "spawn" in ORCH_SYSTEM.lower()


def test_pending_settle_init():
    settle = _PendingSettle(
        agent_id="agent123",
        profile="coder",
        dest=Path("/tmp/work"),
        branch="engine/coder/agent123",
        summary="changes made",
    )
    assert settle.agent_id == "agent123"
    assert settle.profile == "coder"


def test_apply_run_status_ok_to_ok():
    result = AgentResult(status="ok", summary="done", outcome="")
    updated = _apply_run_status(result, "ok", "")
    assert updated.status == "ok"


def test_apply_run_status_ok_does_not_override_incomplete():
    # "incomplete" is set upstream by the compactor, never by run_status
    # itself; _apply_run_status must not clobber it with "ok".
    result = AgentResult(status="incomplete", summary="done", outcome="")
    updated = _apply_run_status(result, "ok", "")
    assert updated.status == "incomplete"


def test_apply_run_status_ok_to_max_turns():
    result = AgentResult(status="ok", summary="done", outcome="")
    updated = _apply_run_status(result, "max_turns", "")
    assert updated.status == "max_turns"


def test_apply_run_status_incomplete_beats_max_turns():
    result = AgentResult(status="incomplete", summary="done", outcome="")
    updated = _apply_run_status(result, "max_turns", "")
    assert updated.status == "incomplete"


def test_apply_run_status_aborted_wins():
    result = AgentResult(status="ok", summary="done", outcome="")
    updated = _apply_run_status(result, "aborted", "")
    assert updated.status == "aborted"


def test_apply_run_status_failed_wins():
    result = AgentResult(status="ok", summary="done", outcome="")
    updated = _apply_run_status(result, "failed", "error: test error")
    assert updated.status == "failed"


def test_apply_run_status_failed_preserves_summary():
    result = AgentResult(status="ok", summary="original summary", outcome="")
    updated = _apply_run_status(result, "failed", "error: failed")
    assert updated.summary == "original summary"


def test_apply_run_status_failed_sets_outcome():
    result = AgentResult(status="ok", summary="done", outcome="")
    updated = _apply_run_status(result, "failed", "error: test error")
    assert updated.outcome == "error: test error"


def test_apply_run_status_stopped():
    result = AgentResult(status="ok", summary="done", outcome="")
    updated = _apply_run_status(result, "stopped", "")
    assert updated.status == "stopped"


def test_apply_run_status_outcome_already_set():
    result = AgentResult(status="ok", summary="done", outcome="existing outcome")
    updated = _apply_run_status(result, "failed", "error: new error")
    assert updated.outcome == "existing outcome"


def test_orchestrator_init(tmp_path):
    provider = FakeProvider()
    orch = _make_orchestrator(llm=provider, workspace=tmp_path)
    assert orch._ctx.workspace == tmp_path


def test_orchestrator_system_prompt(tmp_path):
    provider = FakeProvider()
    orch = _make_orchestrator(llm=provider, workspace=tmp_path)
    assert "orchestrator" in orch._system_prompt.lower() or "spawn" in orch._system_prompt.lower()


def test_orchestrator_max_turns(tmp_path):
    from runtime.config import EngineConfig

    provider = FakeProvider()
    orch = _make_orchestrator(llm=provider, workspace=tmp_path, config=EngineConfig(max_turns=5))
    assert orch._config.max_turns == 5


def test_orchestrator_context_dump(tmp_path):
    provider = FakeProvider()
    orch = _make_orchestrator(llm=provider, workspace=tmp_path)
    dump = orch.context_dump()
    assert isinstance(dump, str)


def test_orchestrator_breakdown_for_unknown(tmp_path):
    provider = FakeProvider()
    orch = _make_orchestrator(llm=provider, workspace=tmp_path)
    breakdown = orch.breakdown_for("unknown_agent")
    assert breakdown is None


def test_orchestrator_transcript_for_unknown(tmp_path):
    provider = FakeProvider()
    orch = _make_orchestrator(llm=provider, workspace=tmp_path)
    transcript = orch.transcript_for("unknown_agent")
    assert transcript is None


def test_orchestrator_with_config(tmp_path):
    from runtime.config import EngineConfig

    provider = FakeProvider()
    config = EngineConfig(max_turns=10)
    orch = _make_orchestrator(llm=provider, workspace=tmp_path, config=config)
    assert orch._config.max_turns == 10


def test_orchestrator_child_tasks(tmp_path):
    provider = FakeProvider()
    orch = _make_orchestrator(llm=provider, workspace=tmp_path)
    assert hasattr(orch, "_child_tasks")
    assert isinstance(orch._child_tasks, dict)


def test_orchestrator_on_agent_result_callback(tmp_path):
    provider = FakeProvider()
    results = []

    def on_agent_result(agent_id, result):
        results.append((agent_id, result))

    orch = _make_orchestrator(llm=provider, workspace=tmp_path, on_agent_result=on_agent_result)
    assert orch._on_agent_result == on_agent_result


def test_orchestrator_on_tool_callback(tmp_path):
    provider = FakeProvider()
    tools_called = []

    def on_tool(name, tool_input, result, status):
        tools_called.append(name)

    orch = _make_orchestrator(llm=provider, workspace=tmp_path, on_tool=on_tool)
    assert orch._on_tool == on_tool


def test_orchestrator_hooks(tmp_path):
    from agents.hooks import AgentHooks

    provider = FakeProvider()
    hooks = AgentHooks()
    orch = _make_orchestrator(llm=provider, workspace=tmp_path, hooks=hooks)
    assert orch._hooks == hooks
