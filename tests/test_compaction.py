from __future__ import annotations

import asyncio

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


def test_context_md(tmp_path):
    assert read_context_md(tmp_path) == ""
    engine = tmp_path / ".engine"
    engine.mkdir()
    (engine / "context.md").write_text("note\n")
    assert "note" in read_context_md(tmp_path)


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


def test_context_md_reports_encoding_replacement(tmp_path):
    engine = tmp_path / ".engine"
    engine.mkdir()
    (engine / "context.md").write_bytes(b"ok \xff broken")
    text = read_context_md(tmp_path)
    assert "encoding errors replaced" in text
    assert "ok" in text
