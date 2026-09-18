from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path

from prometheus_client import CollectorRegistry, Gauge, Histogram, push_to_gateway

from llm.provider import Usage
from protocol.snapshot import Stats
from runtime.config import EngineConfig

TOKEN_KINDS = ("prompt", "completion", "reasoning", "cached", "total")
LLM_BUCKETS = (1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0, 300.0, 600.0)
TOOL_BUCKETS = (0.01, 0.05, 0.1, 0.5, 1.0, 2.0, 5.0, 15.0, 30.0, 60.0, 120.0)
JUDGE_BUCKETS = (0.05, 0.1, 0.2, 0.4, 0.8, 1.5, 3.0)


class EngineMetrics:
    """Per-session Prometheus registry pushed to Pushgateway."""

    def __init__(
        self,
        *,
        url: str = "",
        job: str = "engine",
        interval_s: float = 2.0,
        session_id: str = "",
        workspace: Path | str = "",
        on_warning: Callable[[str], None] | None = None,
        registry: CollectorRegistry | None = None,
    ):
        self._url = (url or "").strip()
        self._job = (job or "engine").strip() or "engine"
        self._interval_s = max(0.0, float(interval_s))
        self._session_id = session_id or ""
        self._on_warning = on_warning
        self.registry = registry or CollectorRegistry()
        self._last_push = 0.0
        self._pending: asyncio.Task | None = None
        self._files: set[str] = set()
        self._started_at: dict[str, float] = {}
        self._live = 0

        workspace_name = Path(workspace).name if workspace else ""
        self._info = Gauge(
            "engine_run_info",
            "Constant 1 labeled with this session's identity",
            ["session_id", "workspace"],
            registry=self.registry,
        )
        self._info.labels(
            session_id=self._session_id, workspace=workspace_name
        ).set(1)

        self._run_cost = Gauge(
            "engine_run_cost_usd",
            "Total LLM cost for this session in USD",
            registry=self.registry,
        )
        self._run_tokens = Gauge(
            "engine_run_tokens",
            "Session token totals by kind",
            ["kind"],
            registry=self.registry,
        )
        self._run_requests = Gauge(
            "engine_run_requests",
            "LLM request count for this session",
            registry=self.registry,
        )
        self._run_llm_turns = Gauge(
            "engine_run_llm_turns",
            "LLM completions counted as session turns",
            registry=self.registry,
        )
        self._run_tool_calls = Gauge(
            "engine_run_tool_calls",
            "Tool calls finished in this session",
            registry=self.registry,
        )
        self._run_files_edited = Gauge(
            "engine_run_files_edited",
            "File edit events in this session",
            registry=self.registry,
        )
        self._run_files_unique = Gauge(
            "engine_run_files_unique",
            "Distinct file paths edited in this session",
            registry=self.registry,
        )
        self._run_agents_spawned = Gauge(
            "engine_run_agents_spawned",
            "Subagents spawned in this session",
            registry=self.registry,
        )
        self._run_agents_live = Gauge(
            "engine_run_agents_live",
            "Currently running subagents",
            registry=self.registry,
        )
        self._run_elapsed = Gauge(
            "engine_run_elapsed_seconds",
            "Wall time spent in orchestrator turns",
            registry=self.registry,
        )
        self._run_last_turn_cost = Gauge(
            "engine_run_last_turn_cost_usd",
            "LLM cost of the current/last user turn",
            registry=self.registry,
        )
        self._run_last_turn_tokens = Gauge(
            "engine_run_last_turn_tokens",
            "Tokens of the current/last user turn",
            registry=self.registry,
        )

        self._agent_spawned = Gauge(
            "engine_agent_spawned",
            "1 if this agent was spawned",
            ["profile", "agent_id"],
            registry=self.registry,
        )
        self._agent_finished = Gauge(
            "engine_agent_finished",
            "1 if this agent finished with the given status",
            ["profile", "agent_id", "status"],
            registry=self.registry,
        )
        self._agent_cost = Gauge(
            "engine_agent_cost_usd",
            "LLM cost attributed to one agent",
            ["profile", "agent_id"],
            registry=self.registry,
        )
        self._agent_tokens = Gauge(
            "engine_agent_tokens",
            "Tokens attributed to one agent",
            ["profile", "agent_id", "kind"],
            registry=self.registry,
        )
        self._agent_requests = Gauge(
            "engine_agent_requests",
            "LLM requests attributed to one agent",
            ["profile", "agent_id"],
            registry=self.registry,
        )
        self._agent_duration = Gauge(
            "engine_agent_duration_seconds",
            "Wall time from spawn to finish",
            ["profile", "agent_id"],
            registry=self.registry,
        )

        self._model_cost = Gauge(
            "engine_model_cost_usd",
            "LLM cost by model",
            ["model"],
            registry=self.registry,
        )
        self._model_tokens = Gauge(
            "engine_model_tokens",
            "Tokens by model and kind",
            ["model", "kind"],
            registry=self.registry,
        )
        self._model_requests = Gauge(
            "engine_model_requests",
            "LLM requests by model",
            ["model"],
            registry=self.registry,
        )
        self._llm_duration = Histogram(
            "engine_llm_request_duration_seconds",
            "LLM complete() wall time",
            ["model", "profile"],
            registry=self.registry,
            buckets=LLM_BUCKETS,
        )
        self._llm_errors = Gauge(
            "engine_llm_errors",
            "Failed LLM complete() calls",
            ["model", "profile"],
            registry=self.registry,
        )

        self._tool_calls = Gauge(
            "engine_tool_calls",
            "Finished tool calls by name",
            ["tool", "profile", "ok"],
            registry=self.registry,
        )
        self._tool_duration = Histogram(
            "engine_tool_duration_seconds",
            "Tool execution wall time",
            ["tool", "profile"],
            registry=self.registry,
            buckets=TOOL_BUCKETS,
        )

        self._compactions = Gauge(
            "engine_compactions",
            "Context compaction events",
            ["profile", "strategy"],
            registry=self.registry,
        )
        self._worktrees = Gauge(
            "engine_worktrees_settled",
            "Worktree settle outcomes",
            ["profile", "action", "ok"],
            registry=self.registry,
        )
        self._errors = Gauge(
            "engine_errors",
            "Session errors by kind",
            ["kind"],
            registry=self.registry,
        )

        self._judge_info = Gauge(
            "engine_judge_info",
            "Constant 1 labeled with the TypeSafe model for this session",
            ["model"],
            registry=self.registry,
        )
        self._judge_requests = Gauge(
            "engine_judge_requests",
            "TypeSafe System One calls by tag and result",
            ["tag", "result"],
            registry=self.registry,
        )
        self._judge_latency = Histogram(
            "engine_judge_request_duration_seconds",
            "TypeSafe System One wall time, excluding cache hits",
            ["tag"],
            registry=self.registry,
            buckets=JUDGE_BUCKETS,
        )
        self._judge_tokens = Gauge(
            "engine_judge_tokens",
            "TypeSafe token usage by kind",
            ["kind"],
            registry=self.registry,
        )
        self._judge_cost = Gauge(
            "engine_judge_cost_usd",
            "TypeSafe reported cost for this session in USD",
            registry=self.registry,
        )
        self._judge_decisions = Gauge(
            "engine_judge_decisions",
            "Judged outcomes emitted as JudgementMade",
            ["tag", "outcome", "enforced"],
            registry=self.registry,
        )

        for kind in TOKEN_KINDS:
            self._run_tokens.labels(kind=kind).set(0)

    @classmethod
    def from_config(
        cls,
        config: EngineConfig,
        *,
        session_id: str,
        workspace: Path | str = "",
        on_warning: Callable[[str], None] | None = None,
    ) -> EngineMetrics:
        metrics = cls(
            url=config.pushgateway_url,
            job=config.metrics_job,
            interval_s=config.metrics_push_interval_s,
            session_id=session_id,
            workspace=workspace,
            on_warning=on_warning,
        )
        metrics.set_judge_model(config.judge_model)
        return metrics

    def sample(self, name: str, labels: dict[str, str] | None = None) -> float | None:
        return self.registry.get_sample_value(name, labels or {})

    def hydrate(self, stats: Stats, live_agents: int = 0) -> None:
        self.sync_run(stats)
        spawned = 0
        for row in stats.agent_runs:
            spawned += 1
            profile = row.profile or "unknown"
            agent_id = row.agent_id
            labels = {"profile": profile, "agent_id": agent_id}
            self._agent_spawned.labels(**labels).set(1)
            self._agent_cost.labels(**labels).set(row.cost)
            self._agent_requests.labels(**labels).set(row.requests)
            self._agent_tokens.labels(**labels, kind="prompt").set(row.prompt_tokens)
            self._agent_tokens.labels(**labels, kind="cached").set(row.cached_tokens)
            self._agent_tokens.labels(**labels, kind="total").set(row.total_tokens)
        if spawned:
            self._run_agents_spawned.set(spawned)
        self.set_live_agents(live_agents)

    def sync_run(self, stats: Stats) -> None:
        self._run_cost.set(stats.cost)
        self._run_tokens.labels(kind="prompt").set(stats.prompt_tokens)
        self._run_tokens.labels(kind="completion").set(stats.completion_tokens)
        self._run_tokens.labels(kind="reasoning").set(stats.reasoning_tokens)
        self._run_tokens.labels(kind="cached").set(stats.cached_tokens)
        self._run_tokens.labels(kind="total").set(stats.total_tokens)
        self._run_requests.set(stats.requests)
        self._run_llm_turns.set(stats.turns)
        self._run_tool_calls.set(stats.tool_calls)
        self._run_elapsed.set(stats.elapsed_s)
        self._run_last_turn_cost.set(stats.last_turn_cost)
        self._run_last_turn_tokens.set(stats.last_turn_tokens)

    def observe_usage(
        self,
        usage: Usage,
        *,
        profile: str = "",
        agent_id: str = "",
        model: str = "",
    ) -> None:
        profile = profile or "orchestrator"
        model = model or "unknown"
        self._add_tokens(self._model_tokens, usage, model=model)
        self._model_cost.labels(model=model).inc(usage.cost)
        self._model_requests.labels(model=model).inc(usage.requests or 1)
        labels = {"profile": profile, "agent_id": agent_id}
        self._add_tokens(self._agent_tokens, usage, **labels)
        self._agent_cost.labels(**labels).inc(usage.cost)
        self._agent_requests.labels(**labels).inc(usage.requests or 1)
        self.request_push()

    def observe_llm_request(
        self, model: str, profile: str, duration_s: float, failed: bool
    ) -> None:
        model = model or "unknown"
        profile = profile or "orchestrator"
        self._llm_duration.labels(model=model, profile=profile).observe(
            max(0.0, duration_s)
        )
        if failed:
            self._llm_errors.labels(model=model, profile=profile).inc()
            self._errors.labels(kind="llm").inc()
        self.request_push()

    def observe_tool(
        self, tool: str, profile: str, ok: bool, duration_s: float
    ) -> None:
        profile = profile or "orchestrator"
        flag = "true" if ok else "false"
        self._tool_calls.labels(tool=tool, profile=profile, ok=flag).inc()
        self._tool_duration.labels(tool=tool, profile=profile).observe(
            max(0.0, duration_s)
        )
        if not ok:
            self._errors.labels(kind="tool").inc()
        self.request_push()

    def observe_edit(self, path: str) -> None:
        self._run_files_edited.inc()
        if path:
            self._files.add(path)
            self._run_files_unique.set(len(self._files))
        self.request_push()

    def observe_agent_started(self, profile: str, agent_id: str) -> None:
        profile = profile or "unknown"
        self._started_at[agent_id] = time.monotonic()
        self._agent_spawned.labels(profile=profile, agent_id=agent_id).set(1)
        self._run_agents_spawned.inc()
        self.set_live_agents(self._live + 1)
        self.request_push()

    def observe_agent_finished(
        self,
        profile: str,
        agent_id: str,
        status: str,
        usage: Usage | None = None,
    ) -> None:
        profile = profile or "unknown"
        status = status or "ok"
        started = self._started_at.pop(agent_id, None)
        if started is not None:
            self._agent_duration.labels(profile=profile, agent_id=agent_id).set(
                max(0.0, time.monotonic() - started)
            )
        self._agent_finished.labels(
            profile=profile, agent_id=agent_id, status=status
        ).set(1)
        if usage is not None:
            labels = {"profile": profile, "agent_id": agent_id}
            self._agent_cost.labels(**labels).set(usage.cost)
            self._agent_requests.labels(**labels).set(usage.requests)
            self._set_tokens(self._agent_tokens, usage, **labels)
        self.set_live_agents(self._live - 1)
        self.request_push(force=True)

    def observe_compact(self, profile: str, strategy: str) -> None:
        if not strategy or strategy == "noop":
            return
        self._compactions.labels(
            profile=profile or "orchestrator", strategy=strategy
        ).inc()
        self.request_push()

    def observe_worktree(self, profile: str, action: str, ok: bool) -> None:
        flag = "true" if ok else "false"
        self._worktrees.labels(
            profile=profile or "unknown", action=action or "unknown", ok=flag
        ).inc()
        self.request_push()

    def observe_error(self, kind: str) -> None:
        self._errors.labels(kind=kind or "session").inc()
        self.request_push()

    def set_judge_model(self, model: str) -> None:
        self._judge_info.labels(model=model or "jev-latest").set(1)

    def observe_judge_request(self, tag: str, verdict, failed: bool) -> None:
        tag = tag or "unknown"
        cache_hit = bool(verdict is not None and getattr(verdict, "cache_hit", False))
        if failed:
            result = "error"
        elif cache_hit:
            result = "cache"
        else:
            result = "ok"
        self._judge_requests.labels(tag=tag, result=result).inc()
        if result == "ok" and verdict is not None:
            latency_s = max(0.0, float(getattr(verdict, "latency_ms", 0) or 0) / 1000.0)
            self._judge_latency.labels(tag=tag).observe(latency_s)
            usage = getattr(verdict, "usage", None)
            if usage is not None:
                inp = int(getattr(usage, "input_tokens", 0) or 0)
                out = int(getattr(usage, "output_tokens", 0) or 0)
                cost = float(getattr(usage, "cost", 0) or 0)
                self._judge_tokens.labels(kind="input").inc(inp)
                self._judge_tokens.labels(kind="output").inc(out)
                self._judge_tokens.labels(kind="total").inc(inp + out)
                if cost:
                    self._judge_cost.inc(cost)
        self.request_push()

    def observe_judge_decision(
        self, tag: str, outcome: str, enforced: bool
    ) -> None:
        self._judge_decisions.labels(
            tag=tag or "unknown",
            outcome=outcome or "unknown",
            enforced="true" if enforced else "false",
        ).inc()
        self.request_push()

    def set_live_agents(self, count: int) -> None:
        self._live = max(0, int(count))
        self._run_agents_live.set(self._live)

    def request_push(self, force: bool = False) -> None:
        if not self._url or not self._session_id:
            return
        if force:
            self.flush()
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.flush()
            return
        now = time.monotonic()
        wait = self._interval_s - (now - self._last_push)
        if self._pending is not None and not self._pending.done():
            if wait > 0:
                return
        if wait <= 0:
            self._pending = loop.create_task(self._push_async())
            return
        self._pending = loop.create_task(self._push_later(wait))

    def flush(self) -> None:
        self._cancel_pending()
        self._push_now()

    def close(self) -> None:
        self.flush()

    def _cancel_pending(self) -> None:
        task = self._pending
        self._pending = None
        if task is not None and not task.done():
            task.cancel()

    async def _push_later(self, delay: float) -> None:
        try:
            await asyncio.sleep(max(0.0, delay))
            await self._push_async()
        except asyncio.CancelledError:
            return

    async def _push_async(self) -> None:
        try:
            await asyncio.to_thread(self._push_now)
        except asyncio.CancelledError:
            return

    def _push_now(self) -> None:
        if not self._url or not self._session_id:
            return
        try:
            push_to_gateway(
                self._url,
                job=self._job,
                registry=self.registry,
                grouping_key={"instance": self._session_id},
            )
            self._last_push = time.monotonic()
        except Exception as exc:  # noqa: BLE001
            if self._on_warning is not None:
                self._on_warning(f"metrics push failed: {exc}")

    def _add_tokens(self, gauge: Gauge, usage: Usage, **labels) -> None:
        gauge.labels(**labels, kind="prompt").inc(usage.prompt_tokens)
        gauge.labels(**labels, kind="completion").inc(usage.completion_tokens)
        gauge.labels(**labels, kind="reasoning").inc(usage.reasoning_tokens)
        gauge.labels(**labels, kind="cached").inc(usage.cached_tokens)
        gauge.labels(**labels, kind="total").inc(usage.total_tokens)

    def _set_tokens(self, gauge: Gauge, usage: Usage, **labels) -> None:
        gauge.labels(**labels, kind="prompt").set(usage.prompt_tokens)
        gauge.labels(**labels, kind="completion").set(usage.completion_tokens)
        gauge.labels(**labels, kind="reasoning").set(usage.reasoning_tokens)
        gauge.labels(**labels, kind="cached").set(usage.cached_tokens)
        gauge.labels(**labels, kind="total").set(usage.total_tokens)
