from __future__ import annotations

import json

from runtime.trace import TraceWriter


def test_write_appends_jsonl_records(tmp_path):
    path = tmp_path / ".engine" / "trace.jsonl"
    writer = TraceWriter(path)
    writer.write("tool", name="read_file", arguments={"path": "a.go"}, ok=True)
    writer.write("judge", tag="call_verify", response={"allow": {"noul": 0.9}})

    lines = path.read_text().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["kind"] == "tool"
    assert first["name"] == "read_file"
    assert first["arguments"] == {"path": "a.go"}
    assert "ts" in first
    second = json.loads(lines[1])
    assert second["kind"] == "judge"
    assert second["tag"] == "call_verify"


def test_write_caps_oversized_string_fields(tmp_path):
    path = tmp_path / "trace.jsonl"
    writer = TraceWriter(path)
    huge = "x" * 10_000
    writer.write("tool", result=huge)

    record = json.loads(path.read_text())
    assert len(record["result"]) < len(huge)
    assert record["result"].startswith("x" * 100)
    assert "more chars" in record["result"]


def test_write_falls_back_when_still_oversized_after_field_capping(tmp_path):
    path = tmp_path / "trace.jsonl"
    writer = TraceWriter(path)
    # many capped-but-still-substantial fields can add up past the whole-line cap
    huge_dict = {f"k{i}": "y" * 3999 for i in range(10)}
    writer.write("judge", request=huge_dict)

    record = json.loads(path.read_text())
    assert record["truncated"] is True
    assert record["kind"] == "judge"
    assert "original_chars" in record


def test_write_survives_unwritable_path(tmp_path):
    path = tmp_path / "no-such-dir" / "sub" / "trace.jsonl"
    writer = TraceWriter(path)
    writer.write("tool", name="x")  # creates parent dirs; must not raise
    assert path.exists()
