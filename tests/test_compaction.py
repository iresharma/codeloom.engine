from __future__ import annotations

import asyncio
import json

from agents.compactor import (
    CONTEXT_ERROR_MARKERS,
    _bounded_transcript,
    compact,
    estimate_tokens,
    looks_like_overflow,
    trim_tool_results,
    validate_history,
)
from llm.provider import LLMResult
from protocol.codec import encode
from protocol.events import ContextCompacted
from runtime.subscriber import EVENT_SOFT_LIMIT, approx_size
from tests.conftest import FakeJudge, FakeVerdict


def _tool_group(call_id="1", name="search", result="lots of " * 200):
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "content": result},
    ]


def _as_text(content) -> str:
    return content if isinstance(content, str) else str(content)


def test_trim_keeps_last_three():
    messages = [{"role": "system", "content": "s"}]
    for i in range(5):
        messages.extend(_tool_group(str(i), result="x" * 1000))
    trimmed, saved = trim_tool_results(messages, keep=3)
    assert saved > 0
    assert validate_history(trimmed) == []
    tools = [m for m in trimmed if m["role"] == "tool"]
    assert len(tools[0]["content"]) < 1000
    assert len(tools[-1]["content"]) == 1000


def test_trim_keeps_last_ten():
    messages = [{"role": "system", "content": "s"}]
    for i in range(12):
        messages.extend(_tool_group(str(i), result="x" * 1000))
    trimmed, saved = trim_tool_results(messages, keep=10)
    assert saved > 0
    tools = [m for m in trimmed if m["role"] == "tool"]
    assert len(tools) == 12
    assert all(len(item["content"]) < 1000 for item in tools[:-10])
    assert all(len(item["content"]) == 1000 for item in tools[-10:])


def test_validate_detects_orphan():
    assert validate_history([{"role": "tool", "tool_call_id": "x", "content": "a"}])


def test_estimate_includes_overhead():
    many = [{"role": "user", "content": "a"} for _ in range(50)]
    assert estimate_tokens(many) > len(str(many)) // 4


def test_markers_match_verbatim():
    for _, _, marker in CONTEXT_ERROR_MARKERS:
        assert looks_like_overflow(marker, 0, 100_000)
    assert not looks_like_overflow("invalid api key", 0, 100_000)


def test_structural_overflow_without_marker():
    assert looks_like_overflow("unrelated", 90_000, 100_000)


def test_noop_under_budget():
    async def run():
        messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "hi"},
        ]
        out, info = await compact(messages, 120_000)
        assert info["strategy"] == "noop"
        assert out == messages
        assert "tokens_after" in info

    asyncio.run(run())


def test_context_compacted_clipped():
    event = ContextCompacted(
        strategy="summarize",
        messages_before=10,
        messages_after=4,
        chars_saved=100,
        summary="x" * 50_000,
    )
    # Producers clip; the event type itself can hold more, so clip like session.
    event.summary = event.summary[:400]
    assert len(encode(event)) < EVENT_SOFT_LIMIT
    assert approx_size(event) >= 400


def test_trim_list_content():
    blocks = [{"type": "text", "text": "x" * 1000}]
    messages = [
        {"role": "system", "content": "s"},
        *_tool_group("1", result=blocks),
        *_tool_group("2", result="short"),
        *_tool_group("3", result="y" * 50),
        *_tool_group("4", result="z" * 50),
    ]
    trimmed, saved = trim_tool_results(messages, keep=3)
    assert saved > 0
    assert validate_history(trimmed) == []
    first = next(m for m in trimmed if m["role"] == "tool")
    assert isinstance(first["content"], str)
    assert first["content"].startswith("x" * 10)
    assert "trimmed" in first["content"]


def test_validate_missing_ids_do_not_collide():
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "", "type": "function", "function": {"name": "a", "arguments": "{}"}},
                {"type": "function", "function": {"name": "b", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "", "content": "one"},
        {"role": "tool", "tool_call_id": "", "content": "two"},
    ]
    errors = validate_history(messages)
    assert any("missing tool_call id" in item for item in errors)
    assert len([item for item in errors if "missing tool_call id" in item]) == 2


def test_summarize_folds_extra_system():
    captured = []

    async def complete(prompt):
        captured.append(prompt)
        return LLMResult(text="kept the reminder about tabs")

    async def run():
        messages = [
            {"role": "system", "content": "s"},
            {"role": "system", "content": "always use tabs"},
            {"role": "user", "content": "first"},
            *_tool_group("1", result="a" * 4000),
            {"role": "user", "content": "second"},
            *_tool_group("2", result="b" * 4000),
        ]
        out, info = await compact(messages, 200, complete=complete)
        assert captured, "summarize should have called complete"
        payload = captured[0][1]["content"]
        assert "always use tabs" in payload
        assert out[0]["content"] == "s"
        assert "always use tabs" not in [m.get("content") for m in out]
        assert info["strategy"] in ("summarize", "truncate")
        assert "tokens_after" in info

    asyncio.run(run())


def test_summarize_failure_falls_back():
    async def complete(prompt):
        raise TimeoutError("llm down")

    async def run():
        messages = [{"role": "system", "content": "s"}]
        for i in range(6):
            messages.extend(_tool_group(str(i), result="x" * 2000))
        messages.append({"role": "user", "content": "latest"})
        out, info = await compact(messages, 300, complete=complete)
        assert info["strategy"] in ("trim", "truncate")
        assert "summarize failed" in info["summary"]
        assert validate_history(out) == []
        assert info["tokens_after"] > 0

    asyncio.run(run())


def test_truncate_when_tail_still_over():
    async def complete(prompt):
        return LLMResult(text="short summary")

    async def run():
        messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "old"},
            *_tool_group("1", result="n" * 500),
            {"role": "user", "content": "now"},
            *_tool_group("2", result="h" * 8000),
        ]
        out, info = await compact(messages, 400, complete=complete)
        tools = [m for m in out if m["role"] == "tool"]
        assert tools
        assert all(len(_as_text(m["content"])) <= 400 + 80 for m in tools)
        assert info["tokens_after"] < estimate_tokens(messages)
        assert validate_history(out) == []

    asyncio.run(run())


def test_bounded_transcript_keeps_head_and_tail():
    items = [
        {"role": "user", "content": "head-marker"},
        {"role": "assistant", "content": "MIDDLE " * 500},
        {"role": "user", "content": "more-middle " * 500},
        {"role": "assistant", "content": "UNIQUE_TAIL"},
    ]
    raw = json.dumps(items)
    assert len(raw) > 800
    dumped = _bounded_transcript(items, limit=800)
    assert len(dumped) <= 800
    payload = json.loads(dumped)
    assert isinstance(payload, list)
    assert "head-marker" in dumped
    assert "UNIQUE_TAIL" in dumped
    assert "middle omitted" in dumped
    assert dumped == json.dumps(payload, default=str)


def test_summarize_prompt_keeps_tail():
    captured = []

    async def complete(prompt):
        captured.append(prompt[1]["content"])
        return LLMResult(text="kept tail")

    async def run():
        messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "head-marker"},
            *_tool_group("1", result="MIDDLE " * 4000),
            {"role": "user", "content": "second"},
            *_tool_group("2", result="UNIQUE_TAIL " * 10),
            {"role": "user", "content": "latest"},
            *_tool_group("3", result="now"),
        ]
        return await compact(messages, 200, complete=complete)

    asyncio.run(run())
    assert captured
    payload = captured[0]
    json.loads(payload)
    assert "head-marker" in payload
    assert "second" in payload
    assert "middle omitted" in payload


def test_higher_trigger_is_noop_at_eighty_percent():
    async def run():
        messages = [
            {"role": "system", "content": "pad " * 80},
            {"role": "user", "content": "hi"},
        ]
        est = estimate_tokens(messages)
        budget = int(est / 0.8)
        high, high_info = await compact(messages, budget, trigger_ratio=0.9)
        low, low_info = await compact(messages, budget, trigger_ratio=0.7)
        assert high_info["strategy"] == "noop"
        assert high == messages
        assert low_info["strategy"] != "noop"

    asyncio.run(run())


def test_compact_params_keeps_falsy_trigger():
    from agents.agent_loop import compact_params
    from agents.compactor import KEEP_FULL_TOOL_RESULTS, TRIGGER_RATIO
    from runtime.config import EngineConfig

    zero = EngineConfig(compact_trigger=0.0, keep_full_tools=0)
    trigger, keep_full = compact_params(zero)
    assert trigger == 0.0
    assert keep_full == 0

    class Bare:
        pass

    trigger, keep_full = compact_params(Bare())
    assert trigger == TRIGGER_RATIO
    assert keep_full == KEEP_FULL_TOOL_RESULTS


def test_child_compact_defaults():
    from runtime.config import (
        CHILD_COMPACT_TRIGGER,
        CHILD_KEEP_FULL_TOOLS,
        EngineConfig,
    )

    orch = EngineConfig()
    assert orch.compact_trigger == 0.7
    assert orch.keep_full_tools == 3
    assert CHILD_COMPACT_TRIGGER == 2.0
    assert CHILD_KEEP_FULL_TOOLS == 10


def test_make_subagent_uses_child_compact(tmp_path):
    from agents.orchestrator import Orchestrator
    from agents.profile import discover_profiles
    from runtime.config import (
        CHILD_COMPACT_TRIGGER,
        CHILD_KEEP_FULL_TOOLS,
        EngineConfig,
    )
    from tests.fakes import FakeProvider
    from tools.registry import ToolRegistry

    orch_config = EngineConfig(compact_trigger=0.7, keep_full_tools=3)
    orch = Orchestrator(
        FakeProvider(),
        all_tools=ToolRegistry(),
        profiles=discover_profiles(),
        workspace=tmp_path,
        config=orch_config,
    )
    child = orch._make_subagent(
        discover_profiles().get("researcher"), "abc123", tmp_path, isolated=False
    )
    assert orch._config.compact_trigger == 0.7
    assert orch._config.keep_full_tools == 3
    assert child._config.compact_trigger == CHILD_COMPACT_TRIGGER
    assert child._config.keep_full_tools == CHILD_KEEP_FULL_TOOLS
    assert child._config is not orch._config
    assert child._freeze_system is True
    assert child._concurrent_tools is True
    assert child._model is None
    ask = orch._make_subagent(
        discover_profiles().get("ask"), "ask1", tmp_path, isolated=False
    )
    assert ask._model is None


def test_freeze_system_ignores_later_memory(tmp_path):
    from agents.agent_loop import AgentLoop
    from runtime.store.memory import remember
    from tests.fakes import FakeProvider

    loop = AgentLoop(FakeProvider(), workspace=tmp_path, freeze_system=True)
    first = loop._build_messages()[0]["content"]
    remember(tmp_path, "engineering", "frozen-child-must-not-see-this")
    second = loop._build_messages()[0]["content"]
    assert first == second
    assert "frozen-child-must-not-see-this" not in second
    orch = AgentLoop(FakeProvider(), workspace=tmp_path, freeze_system=False)
    assert "frozen-child-must-not-see-this" in orch._build_messages()[0]["content"]


# ---------------------------------------------------------------------
# Phase 6a (docs/impl-plans/jev-exp-1.md): compaction by relevance
# ---------------------------------------------------------------------


def _history_with_three_groups():
    return [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "first task"},
        *_tool_group("1", result="AAAA " * 200),
        {"role": "user", "content": "second task"},
        *_tool_group("2", result="BBBB " * 200),
        {"role": "user", "content": "third task"},
        *_tool_group("3", result="CCCC " * 200),
        {"role": "user", "content": "latest"},
    ]


def test_relevance_eviction_drops_scored_group_not_just_oldest():
    from agents.compactor import _atomic_groups, _last_exchange_start, _summarize_group

    async def run():
        messages = _history_with_three_groups()
        prefix = 1
        last = _last_exchange_start(messages)
        groups = _atomic_groups(messages, prefix, last)
        summaries = {
            str(i + 1): _summarize_group(messages, a, b) for i, (a, b) in enumerate(groups)
        }
        # Score every candidate group high (safe) except the one holding the
        # BBBB tool result, which scores lowest -- it must be evicted first
        # even though an older group (AAAA's) exists.
        scores = {
            key: (0.0 if "BBBB" in text else 2.0) for key, text in summaries.items()
        }
        judge = FakeJudge()
        judge.responses["compaction"] = FakeVerdict(scores=scores)
        out, _info = await compact(
            messages, 750, judge=judge, judge_mode="enforcing", goal="do the task"
        )
        assert validate_history(out) == []
        texts = [_as_text(m["content"]) for m in out if m["role"] == "tool"]
        assert not any("BBBB" in t for t in texts)
        assert any("AAAA" in t for t in texts)
        assert any("CCCC" in t for t in texts)

    asyncio.run(run())


def test_none_verdict_matches_positional_drop_oldest():
    async def run():
        messages = _history_with_three_groups()
        baseline, baseline_info = await compact(messages, 60)
        judge = FakeJudge()  # no scripted response -> ask() returns None
        with_judge, with_info = await compact(
            messages, 60, judge=judge, judge_mode="enforcing", goal="do the task"
        )
        assert with_judge == baseline
        assert with_info["chars_saved"] == baseline_info["chars_saved"]

    asyncio.run(run())


def test_disabled_judge_matches_positional_drop_oldest():
    async def run():
        messages = _history_with_three_groups()
        baseline, _ = await compact(messages, 60)
        out, _ = await compact(messages, 60, judge=None, judge_mode="enforcing")
        assert out == baseline

    asyncio.run(run())


def test_compaction_advisory_mode_logs_but_keeps_positional_order():
    async def run():
        messages = _history_with_three_groups()
        judge = FakeJudge()
        judge.responses["compaction"] = FakeVerdict(scores={"1": 2.0, "2": 0.0, "3": 2.0})
        judgements = []
        baseline, _ = await compact(messages, 60)
        out, _ = await compact(
            messages,
            60,
            judge=judge,
            judge_mode="advisory",
            goal="do the task",
            on_judgement=lambda **kw: judgements.append(kw),
        )
        assert out == baseline  # advisory never changes the actual result
        assert judgements
        assert judgements[0]["enforced"] is False

    asyncio.run(run())


def test_relevance_eviction_preserves_history_invariant():
    async def run():
        messages = _history_with_three_groups()
        judge = FakeJudge()
        judge.responses["compaction"] = FakeVerdict(scores={"1": 0.0, "2": 1.0, "3": 2.0})
        out, _ = await compact(
            messages, 30, judge=judge, judge_mode="enforcing", goal="do the task"
        )
        assert validate_history(out) == []

    asyncio.run(run())
