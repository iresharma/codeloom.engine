from __future__ import annotations

import asyncio
import json

from runtime.session import EngineSession
from runtime.trace import TraceWriter


def _session(tmp_path) -> EngineSession:
    session = EngineSession(tmp_path, tmp_path / "session.db")

    async def start():
        await session.start()
        return session

    asyncio.run(start())
    return session


def test_on_tool_writes_trace_when_enabled(tmp_path):
    session = _session(tmp_path)
    session._config.trace_calls = True
    session._trace = TraceWriter(tmp_path / ".engine" / "trace.jsonl")

    session._on_tool(
        "call-1",
        "read_file",
        {"path": "a.go"},
        "package main",
        agent_id="",
        profile="orchestrator",
        reasoning="I need to see the entry point before editing it.",
    )

    lines = (tmp_path / ".engine" / "trace.jsonl").read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["kind"] == "tool"
    assert record["name"] == "read_file"
    assert record["arguments"] == {"path": "a.go"}
    assert record["result"] == "package main"
    assert record["reasoning"] == "I need to see the entry point before editing it."


def test_on_tool_is_a_noop_when_tracing_disabled(tmp_path):
    session = _session(tmp_path)
    assert session._trace is None

    session._on_tool("call-1", "read_file", {"path": "a.go"}, "x")

    assert not (tmp_path / ".engine" / "trace.jsonl").exists()
