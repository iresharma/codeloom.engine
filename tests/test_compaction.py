from __future__ import annotations

from agents.compactor import (
    CONTEXT_ERROR_MARKERS,
    compact,
    estimate_tokens,
    looks_like_overflow,
    read_context_md,
    trim_tool_results,
    validate_history,
)
from llm.provider import LLMResult
from protocol.codec import encode
from protocol.events import ContextCompacted
from runtime.subscriber import EVENT_SOFT_LIMIT, approx_size


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

    import asyncio

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


def test_context_md(tmp_path):
    assert read_context_md(tmp_path) == ""
    engine = tmp_path / ".engine"
    engine.mkdir()
    (engine / "context.md").write_text("note\n")
    assert "note" in read_context_md(tmp_path)
