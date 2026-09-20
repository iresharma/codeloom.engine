"""Coverage for agents/orchestrator.py"""
from __future__ import annotations

from pathlib import Path

from agents.orchestrator import (
    Orchestrator,
    ORCH_SYSTEM,
    _PendingSettle,
    _apply_run_status,
)
from agents.profile import discover_profiles
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


def test_orchestrator_plan_mode_init(tmp_path):
    """plan_mode starts as False."""
    provider = FakeProvider()
    orch = _make_orchestrator(llm=provider, workspace=tmp_path)
    assert orch.plan_mode is False


def test_orchestrator_enter_plan_mode_tool_exists(tmp_path):
    """enter_plan_mode tool is registered."""
    provider = FakeProvider()
    orch = _make_orchestrator(llm=provider, workspace=tmp_path)
    assert "enter_plan_mode" in orch._tools.names()


def test_orchestrator_present_plan_tool_exists(tmp_path):
    """present_plan tool is registered."""
    provider = FakeProvider()
    orch = _make_orchestrator(llm=provider, workspace=tmp_path)
    assert "present_plan" in orch._tools.names()


def test_present_plan_not_in_plan_mode_returns_error(tmp_path):
    """present_plan returns error when plan_mode is False (not called yet)."""
    import asyncio
    from tools.base import ToolContext

    provider = FakeProvider()
    orch = _make_orchestrator(llm=provider, workspace=tmp_path)
    
    async def run():
        # Get the present_plan tool
        spec = orch._tools._tools.get("present_plan")
        assert spec is not None
        ctx = ToolContext(workspace=tmp_path)
        result = await spec.execute(ctx, {"plan": "some plan"})
        assert result.startswith("error: not in plan mode")
        # Verify it did NOT call _child_ask_user
        assert orch._child_ask_user is None

    asyncio.run(run())


def test_enter_plan_mode_sets_flag(tmp_path):
    """enter_plan_mode tool sets plan_mode to True."""
    import asyncio
    from tools.base import ToolContext

    provider = FakeProvider()
    orch = _make_orchestrator(llm=provider, workspace=tmp_path)
    
    async def run():
        spec = orch._tools._tools.get("enter_plan_mode")
        assert spec is not None
        ctx = ToolContext(workspace=tmp_path)
        result = await spec.execute(ctx, {})
        assert "plan mode is now on" in result
        assert orch.plan_mode is True

    asyncio.run(run())


def test_present_plan_with_yes_approval_clears_plan_mode(tmp_path):
    """present_plan with yes/y answer clears plan_mode and returns success."""
    import asyncio
    from tools.base import ToolContext

    provider = FakeProvider()
    ask_user_called = []

    async def mock_ask_user(question, kind="confirm"):
        ask_user_called.append((question, kind))
        return "yes"

    orch = _make_orchestrator(
        llm=provider,
        workspace=tmp_path,
        child_ask_user=mock_ask_user,
    )
    
    async def run():
        # Enter plan mode first
        enter_spec = orch._tools._tools.get("enter_plan_mode")
        ctx = ToolContext(workspace=tmp_path)
        await enter_spec.execute(ctx, {})
        assert orch.plan_mode is True
        
        # Now present the plan
        present_spec = orch._tools._tools.get("present_plan")
        result = await present_spec.execute(ctx, {"plan": "test plan"})
        
        assert "plan approved" in result
        assert orch.plan_mode is False
        assert len(ask_user_called) == 1
        assert ask_user_called[0][0].startswith("Proposed plan:")
        assert ask_user_called[0][1] == "confirm"

    asyncio.run(run())


def test_present_plan_with_yes_capital_approval(tmp_path):
    """present_plan with Y (capital) answer also clears plan_mode."""
    import asyncio
    from tools.base import ToolContext

    provider = FakeProvider()

    async def mock_ask_user(question, kind="confirm"):
        return "Y"

    orch = _make_orchestrator(
        llm=provider,
        workspace=tmp_path,
        child_ask_user=mock_ask_user,
    )
    
    async def run():
        ctx = ToolContext(workspace=tmp_path)
        enter_spec = orch._tools._tools.get("enter_plan_mode")
        await enter_spec.execute(ctx, {})
        
        present_spec = orch._tools._tools.get("present_plan")
        result = await present_spec.execute(ctx, {"plan": "test plan"})
        
        assert "plan approved" in result
        assert orch.plan_mode is False

    asyncio.run(run())


def test_present_plan_with_no_rejection_keeps_plan_mode(tmp_path):
    """present_plan with no answer keeps plan_mode True and returns feedback."""
    import asyncio
    from tools.base import ToolContext

    provider = FakeProvider()

    async def mock_ask_user(question, kind="confirm"):
        return "no, this won't work because X"

    orch = _make_orchestrator(
        llm=provider,
        workspace=tmp_path,
        child_ask_user=mock_ask_user,
    )
    
    async def run():
        ctx = ToolContext(workspace=tmp_path)
        enter_spec = orch._tools._tools.get("enter_plan_mode")
        await enter_spec.execute(ctx, {})
        
        present_spec = orch._tools._tools.get("present_plan")
        result = await present_spec.execute(ctx, {"plan": "test plan"})
        
        assert "plan rejected" in result
        assert orch.plan_mode is True
        assert "no, this won't work because X" in result

    asyncio.run(run())


def test_spawn_coder_blocked_in_plan_mode(tmp_path):
    """spawn("coder", ...) returns error while plan_mode is True."""
    import asyncio
    from tools.base import ToolContext
    from agents.profile import discover_profiles

    provider = FakeProvider()
    profiles = discover_profiles()
    orch = Orchestrator(
        llm=provider,
        workspace=tmp_path,
        all_tools=ToolRegistry(),
        profiles=profiles,
    )
    
    async def run():
        # Enter plan mode
        ctx = ToolContext(workspace=tmp_path)
        enter_spec = orch._tools._tools.get("enter_plan_mode")
        await enter_spec.execute(ctx, {})
        
        # Try to spawn coder
        result = await orch.spawn("coder", "do something")
        
        assert result.startswith("error:")
        assert "plan mode is active" in result
        assert "present_plan" in result
        
        # Verify no task was created
        assert len(orch._child_tasks) == 0
        assert len(orch._children) == 0

    asyncio.run(run())


def test_spawn_tester_blocked_in_plan_mode(tmp_path):
    """spawn("tester", ...) returns error while plan_mode is True."""
    import asyncio
    from tools.base import ToolContext

    provider = FakeProvider()
    profiles = discover_profiles()
    orch = Orchestrator(
        llm=provider,
        workspace=tmp_path,
        all_tools=ToolRegistry(),
        profiles=profiles,
    )
    
    async def run():
        # Enter plan mode
        ctx = ToolContext(workspace=tmp_path)
        enter_spec = orch._tools._tools.get("enter_plan_mode")
        await enter_spec.execute(ctx, {})
        
        # Try to spawn tester
        result = await orch.spawn("tester", "run tests")
        
        assert result.startswith("error:")
        assert "plan mode is active" in result
        
        # Verify no task was created
        assert len(orch._child_tasks) == 0

    asyncio.run(run())


def test_spawn_ask_allowed_in_plan_mode(tmp_path):
    """spawn("ask", ...) is NOT blocked in plan mode (read-only)."""
    import asyncio
    from tools.base import ToolContext

    provider = FakeProvider()
    profiles = discover_profiles()
    orch = Orchestrator(
        llm=provider,
        workspace=tmp_path,
        all_tools=ToolRegistry(),
        profiles=profiles,
    )
    
    async def run():
        # Enter plan mode
        ctx = ToolContext(workspace=tmp_path)
        enter_spec = orch._tools._tools.get("enter_plan_mode")
        await enter_spec.execute(ctx, {})
        
        # Try to spawn ask - should proceed (ask doesn't have needs_worktree)
        result = await orch.spawn("ask", "find something")
        
        # Should start successfully (not an error about plan mode)
        assert not result.startswith("error: plan mode is active")
        assert "started agent_id=" in result
        
        # Task was created
        assert len(orch._child_tasks) >= 1

    asyncio.run(run())


def test_spawn_researcher_allowed_in_plan_mode(tmp_path):
    """spawn("researcher", ...) is NOT blocked in plan mode (read-only)."""
    import asyncio
    from tools.base import ToolContext

    provider = FakeProvider()
    profiles = discover_profiles()
    orch = Orchestrator(
        llm=provider,
        workspace=tmp_path,
        all_tools=ToolRegistry(),
        profiles=profiles,
    )
    
    async def run():
        ctx = ToolContext(workspace=tmp_path)
        enter_spec = orch._tools._tools.get("enter_plan_mode")
        await enter_spec.execute(ctx, {})
        
        result = await orch.spawn("researcher", "research something")
        
        assert not result.startswith("error: plan mode is active")
        assert "started agent_id=" in result
        assert len(orch._child_tasks) >= 1

    asyncio.run(run())


def test_spawn_debugger_allowed_in_plan_mode(tmp_path):
    """spawn("debugger", ...) is NOT blocked in plan mode (read-only)."""
    import asyncio
    from tools.base import ToolContext

    provider = FakeProvider()
    profiles = discover_profiles()
    orch = Orchestrator(
        llm=provider,
        workspace=tmp_path,
        all_tools=ToolRegistry(),
        profiles=profiles,
    )
    
    async def run():
        ctx = ToolContext(workspace=tmp_path)
        enter_spec = orch._tools._tools.get("enter_plan_mode")
        await enter_spec.execute(ctx, {})
        
        result = await orch.spawn("debugger", "debug something")
        
        assert not result.startswith("error: plan mode is active")
        assert "started agent_id=" in result

    asyncio.run(run())


def test_spawn_reviewer_allowed_in_plan_mode(tmp_path):
    """spawn("reviewer", ...) is NOT blocked in plan mode (read-only)."""
    import asyncio
    from tools.base import ToolContext

    provider = FakeProvider()
    profiles = discover_profiles()
    orch = Orchestrator(
        llm=provider,
        workspace=tmp_path,
        all_tools=ToolRegistry(),
        profiles=profiles,
    )
    
    async def run():
        ctx = ToolContext(workspace=tmp_path)
        enter_spec = orch._tools._tools.get("enter_plan_mode")
        await enter_spec.execute(ctx, {})
        
        result = await orch.spawn("reviewer", "review something")
        
        assert not result.startswith("error: plan mode is active")
        assert "started agent_id=" in result

    asyncio.run(run())


def test_coder_spawn_allowed_after_plan_approval(tmp_path):
    """After plan is approved and plan_mode cleared, coder spawn works again."""
    import asyncio
    from tools.base import ToolContext

    provider = FakeProvider()
    profiles = discover_profiles()

    async def mock_ask_user(question, kind="confirm"):
        return "yes"

    orch = Orchestrator(
        llm=provider,
        workspace=tmp_path,
        all_tools=ToolRegistry(),
        profiles=profiles,
        child_ask_user=mock_ask_user,
    )
    
    async def run():
        ctx = ToolContext(workspace=tmp_path)
        # Enter plan mode
        enter_spec = orch._tools._tools.get("enter_plan_mode")
        await enter_spec.execute(ctx, {})
        assert orch.plan_mode is True
        
        # Present and approve plan
        present_spec = orch._tools._tools.get("present_plan")
        result = await present_spec.execute(ctx, {"plan": "test plan"})
        assert "plan approved" in result
        assert orch.plan_mode is False
        
        # Now spawn coder should work
        coder_result = await orch.spawn("coder", "make a change")
        
        assert not coder_result.startswith("error: plan mode is active")
        assert "started agent_id=" in coder_result

    asyncio.run(run())


def test_plan_mode_rejected_and_revised(tmp_path):
    """After rejection, can call present_plan again with revised plan."""
    import asyncio
    from tools.base import ToolContext

    provider = FakeProvider()
    profiles = discover_profiles()
    call_count = []

    async def mock_ask_user(question, kind="confirm"):
        call_count.append(1)
        # First call: reject
        # Second call: approve
        return "yes" if len(call_count) > 1 else "no"

    orch = Orchestrator(
        llm=provider,
        workspace=tmp_path,
        all_tools=ToolRegistry(),
        profiles=profiles,
        child_ask_user=mock_ask_user,
    )
    
    async def run():
        ctx = ToolContext(workspace=tmp_path)
        enter_spec = orch._tools._tools.get("enter_plan_mode")
        await enter_spec.execute(ctx, {})
        
        present_spec = orch._tools._tools.get("present_plan")
        
        # First attempt: rejected
        result1 = await present_spec.execute(ctx, {"plan": "first plan"})
        assert "plan rejected" in result1
        assert orch.plan_mode is True
        
        # Second attempt: approved
        result2 = await present_spec.execute(ctx, {"plan": "revised plan"})
        assert "plan approved" in result2
        assert orch.plan_mode is False

    asyncio.run(run())
