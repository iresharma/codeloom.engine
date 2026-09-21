from __future__ import annotations

import asyncio
from unittest.mock import patch

from agents.agent_loop import AgentLoop
from agents.hooks import AgentHooks
from agents.profiles.ask import PROFILE
from agents.subagent import Subagent
from llm.provider import LLMResult, Usage
from protocol.commands import StartSession
from protocol.snapshot import AgentRun, Stats
from runtime.config import EngineConfig
from runtime.metrics import EngineMetrics
from runtime.session import EngineSession
from tests.fakes import FakeProvider
from tools.registry import ToolRegistry


def test_observe_usage_tool_edit_spawn(tmp_path):
    metrics = EngineMetrics(session_id="s1", workspace=tmp_path)
    usage = Usage(
        prompt_tokens=10,
        completion_tokens=4,
        reasoning_tokens=1,
        cached_tokens=2,
        total_tokens=17,
        cost=0.05,
        requests=1,
    )
    metrics.observe_usage(
        usage, profile="coder", agent_id="a1", model="anthropic/claude-sonnet-5"
    )
    stats = Stats(
        prompt_tokens=10,
        completion_tokens=4,
        reasoning_tokens=1,
        cached_tokens=2,
        total_tokens=17,
        cost=0.05,
        requests=1,
        turns=1,
        tool_calls=0,
    )
    metrics.sync_run(stats)
    metrics.observe_tool("str_replace", "coder", True, 0.2)
    metrics.observe_edit("src/app.py")
    metrics.observe_edit("src/app.py")
    metrics.observe_edit("src/other.py")
    metrics.observe_agent_started("coder", "a1")
    metrics.observe_agent_finished("coder", "a1", "ok", usage)
    metrics.observe_compact("orchestrator", "summarize")
    metrics.observe_worktree("coder", "commit", True)
    metrics.observe_llm_request("anthropic/claude-sonnet-5", "coder", 1.5, False)
    metrics.observe_error("session")

    assert metrics.sample("engine_run_cost_usd") == 0.05
    assert metrics.sample("engine_run_tokens", {"kind": "prompt"}) == 10
    assert metrics.sample("engine_run_tokens", {"kind": "total"}) == 17
    assert metrics.sample("engine_run_files_edited") == 3
    assert metrics.sample("engine_run_files_unique") == 2
    assert metrics.sample("engine_run_agents_spawned") == 1
    assert metrics.sample("engine_run_agents_live") == 0
    assert (
        metrics.sample(
            "engine_model_cost_usd", {"model": "anthropic/claude-sonnet-5"}
        )
        == 0.05
    )
    assert (
        metrics.sample(
            "engine_agent_cost_usd", {"profile": "coder", "agent_id": "a1"}
        )
        == 0.05
    )
    assert (
        metrics.sample(
            "engine_agent_finished",
            {"profile": "coder", "agent_id": "a1", "status": "ok"},
        )
        == 1
    )
    assert (
        metrics.sample(
            "engine_tool_calls",
            {"tool": "str_replace", "profile": "coder", "ok": "true"},
        )
        == 1
    )
    assert (
        metrics.sample(
            "engine_llm_request_duration_seconds_count",
            {"model": "anthropic/claude-sonnet-5", "profile": "coder"},
        )
        == 1
    )
    assert metrics.sample("engine_compactions", {"profile": "orchestrator", "strategy": "summarize"}) == 1
    assert metrics.sample(
        "engine_worktrees_settled",
        {"profile": "coder", "action": "commit", "ok": "true"},
    ) == 1
    assert metrics.sample("engine_errors", {"kind": "session"}) == 1
    assert metrics.sample(
        "engine_run_info", {"session_id": "s1", "workspace": tmp_path.name}
    ) == 1


def test_hydrate_from_stats(tmp_path):
    stats = Stats(
        prompt_tokens=9,
        completion_tokens=3,
        cost=1.25,
        requests=2,
        turns=2,
        tool_calls=4,
        elapsed_s=8.5,
        last_turn_cost=0.4,
        last_turn_tokens=12,
        agent_runs=[
            AgentRun(
                agent_id="abc",
                profile="researcher",
                cost=0.8,
                prompt_tokens=10,
                cached_tokens=4,
                total_tokens=12,
                requests=3,
            )
        ],
    )
    metrics = EngineMetrics(session_id="s1", workspace=tmp_path)
    metrics.hydrate(stats, live_agents=1)
    assert metrics.sample("engine_run_cost_usd") == 1.25
    assert metrics.sample("engine_run_requests") == 2
    assert metrics.sample("engine_run_tool_calls") == 4
    assert metrics.sample("engine_run_elapsed_seconds") == 8.5
    assert metrics.sample("engine_run_agents_spawned") == 1
    assert metrics.sample("engine_run_agents_live") == 1
    assert (
        metrics.sample(
            "engine_agent_cost_usd",
            {"profile": "researcher", "agent_id": "abc"},
        )
        == 0.8
    )
    assert (
        metrics.sample(
            "engine_agent_tokens",
            {"profile": "researcher", "agent_id": "abc", "kind": "cached"},
        )
        == 4
    )


def test_disabled_url_never_pushes(tmp_path):
    with patch("runtime.metrics.push_to_gateway") as push:
        metrics = EngineMetrics(url="", session_id="s1", workspace=tmp_path)
        metrics.observe_error("session")
        metrics.flush()
        push.assert_not_called()


def test_enabled_url_flushes(tmp_path):
    with patch("runtime.metrics.push_to_gateway") as push:
        metrics = EngineMetrics(
            url="http://pushgateway:9091",
            job="engine-bench",
            session_id="abc123",
            workspace=tmp_path,
        )
        metrics.flush()
        push.assert_called_once()
        kwargs = push.call_args.kwargs
        assert push.call_args.args[0] == "http://pushgateway:9091"
        assert kwargs["job"] == "engine-bench"
        assert kwargs["grouping_key"] == {"instance": "abc123"}


def test_metrics_instance_overrides_session_id(tmp_path):
    with patch("runtime.metrics.push_to_gateway") as push:
        metrics = EngineMetrics(
            url="http://pushgateway:9091",
            job="engine",
            session_id="abc123",
            instance="baseline",
            workspace=tmp_path,
        )
        metrics.flush()
        assert push.call_args.kwargs["grouping_key"] == {"instance": "baseline"}


def test_from_config_passes_metrics_instance(tmp_path):
    config = EngineConfig(
        pushgateway_url="http://pushgateway:9091",
        metrics_instance="judge",
    )
    metrics = EngineMetrics.from_config(
        config, session_id="s1", workspace=tmp_path
    )
    with patch("runtime.metrics.push_to_gateway") as push:
        metrics.flush()
        assert push.call_args.kwargs["grouping_key"] == {"instance": "judge"}


def test_push_failure_warns(tmp_path):
    warnings: list[str] = []
    with patch("runtime.metrics.push_to_gateway", side_effect=OSError("down")):
        metrics = EngineMetrics(
            url="http://pushgateway:9091",
            session_id="s1",
            workspace=tmp_path,
            on_warning=warnings.append,
        )
        metrics.flush()
    assert warnings
    assert "metrics push failed" in warnings[0]


def test_session_resume_hydrates(tmp_path):
    async def run():
        db = tmp_path / "session.db"
        session = EngineSession(tmp_path, db)
        await session.start()
        await session.handle(StartSession(workspace=str(tmp_path)))
        session_id = session._state.session_id
        session._state.stats = Stats(
            cost=2.5,
            prompt_tokens=40,
            requests=3,
            agent_runs=[
                AgentRun(agent_id="c1", profile="coder", cost=1.1, total_tokens=20)
            ],
        )
        session._persist()
        session.close_session()

        resumed = EngineSession(tmp_path, db)
        await resumed.start()
        await resumed.handle(
            StartSession(workspace=str(tmp_path), session_id=session_id)
        )
        metrics = resumed._metrics
        assert metrics is not None
        assert metrics.sample("engine_run_cost_usd") == 2.5
        assert (
            metrics.sample(
                "engine_agent_cost_usd", {"profile": "coder", "agent_id": "c1"}
            )
            == 1.1
        )
        resumed.close_session()

    asyncio.run(run())


def test_session_close_flushes_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGINE_PUSHGATEWAY_URL", "http://pushgateway:9091")
    with patch("runtime.metrics.push_to_gateway") as push:

        async def run():
            session = EngineSession(tmp_path, tmp_path / "session.db")
            await session.start()
            await session.handle(StartSession(workspace=str(tmp_path)))
            session.close_session()

        asyncio.run(run())
    assert push.called
    assert push.call_args.kwargs["grouping_key"]["instance"]


def test_session_close_disabled_never_pushes(monkeypatch, tmp_path):
    monkeypatch.delenv("ENGINE_PUSHGATEWAY_URL", raising=False)
    with patch("runtime.metrics.push_to_gateway") as push:

        async def run():
            session = EngineSession(tmp_path, tmp_path / "session.db")
            await session.start()
            await session.handle(StartSession(workspace=str(tmp_path)))
            session.close_session()

        asyncio.run(run())
    push.assert_not_called()


def test_compact_counts_usage(tmp_path):
    usages: list[Usage] = []

    def on_usage(usage, model=""):
        usages.append(usage)

    provider = FakeProvider(
        results=[
            LLMResult(
                text="earlier work summarized",
                usage=Usage(
                    prompt_tokens=7,
                    completion_tokens=2,
                    total_tokens=9,
                    cost=0.02,
                    requests=1,
                ),
                model="fake",
            )
        ]
    )
    loop = AgentLoop(
        provider,
        workspace=tmp_path,
        hooks=AgentHooks(on_usage=on_usage),
        config=EngineConfig(context_budget=8, compact_trigger=0.0),
    )
    loop._history = [
        {"role": "user", "content": "head " * 80},
        {"role": "assistant", "content": "mid " * 80},
        {"role": "user", "content": "more " * 80},
        {"role": "assistant", "content": "tail " * 80},
        {"role": "user", "content": "now " * 80},
        {"role": "assistant", "content": "end " * 80},
    ]

    async def run():
        await loop._maybe_compact(force=True)

    asyncio.run(run())
    assert usages
    assert usages[0].prompt_tokens == 7
    assert abs(usages[0].cost - 0.02) < 1e-9
    assert loop._usage.prompt_tokens == 7


def test_compress_for_parent_counts_usage(tmp_path):
    usages: list[Usage] = []

    def on_usage(usage, model=""):
        usages.append(usage)

    provider = FakeProvider(
        results=[
            LLMResult(
                text="what: survey\npaths: a.py",
                usage=Usage(
                    prompt_tokens=5,
                    completion_tokens=3,
                    total_tokens=8,
                    cost=0.01,
                    requests=1,
                ),
                model="fake",
            )
        ]
    )
    child = Subagent(
        PROFILE,
        llm=provider,
        tools=ToolRegistry(),
        workspace=tmp_path,
        hooks=AgentHooks(on_usage=on_usage),
    )
    child._history = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
        {"role": "assistant", "content": "four"},
        {"role": "user", "content": "five"},
        {"role": "assistant", "content": "unlabeled closer that is not a briefing"},
    ]

    async def run():
        return await child.finish("ok")

    result = asyncio.run(run())
    assert result.status == "ok"
    assert usages
    assert usages[0].total_tokens == 8
    assert child._usage.total_tokens == 8


def test_llm_error_records_histogram(tmp_path):
    class Boom(FakeProvider):
        async def complete(self, messages, tools=None, *, on_delta=None, **kwargs):
            self.calls += 1
            raise RuntimeError("provider down")

    seen: list[tuple[str, float, bool]] = []

    def on_llm_request(model, duration_s, failed):
        seen.append((model, duration_s, failed))

    loop = AgentLoop(
        Boom(),
        workspace=tmp_path,
        hooks=AgentHooks(on_llm_request=on_llm_request),
        model="anthropic/claude-sonnet-5",
    )

    async def run():
        try:
            await loop._complete([{"role": "user", "content": "hi"}], [])
        except RuntimeError:
            return

    asyncio.run(run())
    assert seen
    assert seen[0][0] == "anthropic/claude-sonnet-5"
    assert seen[0][2] is True


def test_observe_judge_request_and_decision(tmp_path):
    metrics = EngineMetrics(session_id="s1", workspace=tmp_path)
    metrics.set_judge_model("jev-latest")

    class Usage:
        input_tokens = 10
        output_tokens = 5
        cost = 0.002

    class Verdict:
        cache_hit = False
        latency_ms = 120
        usage = Usage()

    metrics.observe_judge_request("exec_approval", Verdict(), failed=False)
    metrics.observe_judge_decision("exec_approval", "allow", False)
    cached = Verdict()
    cached.cache_hit = True
    metrics.observe_judge_request("exec_approval", cached, failed=False)
    metrics.observe_judge_request("call_verify", None, failed=True)

    assert metrics.sample("engine_judge_info", {"model": "jev-latest"}) == 1
    assert (
        metrics.sample(
            "engine_judge_requests", {"tag": "exec_approval", "result": "ok"}
        )
        == 1
    )
    assert (
        metrics.sample(
            "engine_judge_requests", {"tag": "exec_approval", "result": "cache"}
        )
        == 1
    )
    assert (
        metrics.sample(
            "engine_judge_requests", {"tag": "call_verify", "result": "error"}
        )
        == 1
    )
    assert metrics.sample("engine_judge_tokens", {"kind": "input"}) == 10
    assert metrics.sample("engine_judge_tokens", {"kind": "output"}) == 5
    assert metrics.sample("engine_judge_tokens", {"kind": "total"}) == 15
    assert abs(metrics.sample("engine_judge_cost_usd") - 0.002) < 1e-9
    assert (
        metrics.sample(
            "engine_judge_decisions",
            {"tag": "exec_approval", "outcome": "allow", "enforced": "false"},
        )
        == 1
    )
    assert (
        metrics.sample(
            "engine_judge_request_duration_seconds_count",
            {"tag": "exec_approval"},
        )
        == 1
    )


def test_from_config_sets_judge_model(tmp_path):
    config = EngineConfig(judge_model="jev-1.13")
    metrics = EngineMetrics.from_config(
        config, session_id="s1", workspace=tmp_path
    )
    assert metrics.sample("engine_judge_info", {"model": "jev-1.13"}) == 1
