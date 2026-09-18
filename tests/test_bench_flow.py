from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


def _load():
    path = Path(__file__).resolve().parents[1] / "scripts" / "bench_flow.py"
    spec = importlib.util.spec_from_file_location("bench_flow", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_dewrap_rejoins_mid_word_stream_chunks():
    mod = _load()
    text = "not currently visibly broken, b\nroken code that will misbehave"
    assert "broken code" in mod.dewrap(text)


def test_dewrap_keeps_paragraph_and_list_breaks():
    mod = _load()
    text = "intro line\n\n- item one\n- item two"
    out = mod.dewrap(text)
    assert "intro line\n\n" in out
    assert "- item one\n- item two" in out


def test_parse_client_log_extracts_judge_calls_and_phases(tmp_path):
    mod = _load()
    log = tmp_path / "client-judge.log"
    log.write_text(
        "session abc123\n"
        "user: do the thing\n"
        "judge intent_route -> ambiguous (enforced, 951ms): do the thing\n"
        "  is_multi_file=0.74  is_ambiguous=0.86\n"
        "agent started coder 1a5c6c62c078422e9ed86ef9fffc4611 batch=after ask "
        "task=build it worktree=/tmp/wt branch=engine/coder/1a5c\n"
        "judge call_verify -> allow (enforced, 100ms) [1a5c6c62c078422e9ed86ef9fffc4611]: create_file(...)\n"
        "  tool_suits_request=0.90  path_was_read=0.10\n"
        "tool create_file started [1a5c6c62c078422e9ed86ef9fffc4611]\n"
        "tool create_file ok (5ms) [1a5c6c62c078422e9ed86ef9fffc4611]\n"
        "agent finished coder 1a5c6c62c078422e9ed86ef9fffc4611 ok: $0.100 1000 tok 500 cached done building\n"
    )
    session_id, prompt, orch_judge, other_tools, phases, by_id, warnings = mod.parse_client_log(log)

    assert session_id == "abc123"
    assert prompt == "do the thing"
    assert len(orch_judge) == 1
    assert orch_judge[0].kind == "intent_route"
    assert orch_judge[0].scores == {"is_multi_file": 0.74, "is_ambiguous": 0.86}

    assert len(phases) == 1
    phase = phases[0]
    assert phase.profile == "coder"
    assert phase.status == "ok"
    assert phase.cost == pytest.approx(0.1)
    assert phase.tokens == 1000
    assert len(phase.judge_calls) == 1
    assert phase.judge_calls[0].kind == "call_verify"
    assert phase.judge_calls[0].scores == {"tool_suits_request": 0.9, "path_was_read": 0.1}
    assert phase.tool_tally() == {"create_file": 1}


def test_parse_transcript_splits_blocks_by_role(tmp_path):
    mod = _load()
    path = tmp_path / "transcript-baseline.txt"
    path.write_text(
        "## user\nfix the bug\n\n"
        "## assistant\nworking on it\n\n"
        "## engine\n[agent coder b03318d3 finished]\nstatus: ok\nsummary: did the thing\noutcome: full outcome text\n"
    )
    blocks = mod.parse_transcript(path)
    assert [role for role, _ in blocks] == ["user", "assistant", "engine"]
    assert blocks[0][1] == "fix the bug"
    assert "did the thing" in blocks[2][1]


def test_split_fields_captures_labeled_sections():
    mod = _load()
    body = "status: ok\nsummary: short\noutcome: long version\nverdict: request changes\n"
    fields = mod._split_fields(body)
    assert fields["status"] == ["ok"]
    assert fields["outcome"] == ["long version"]
    assert fields["verdict"] == ["request changes"]


def test_build_side_detects_abort_and_pr(tmp_path):
    mod = _load()
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "client-baseline.log").write_text("session s1\nuser: do it\n")
    (logs / "transcript-baseline.txt").write_text(
        "## user\ndo it\n\n"
        "## engine\n[worktree coder b03318d3 pr]\nopened pull request for engine/coder/b03318d3: "
        "https://github.com/org/repo/pull/1\n\n"
        "## assistant\nPull request opened: https://github.com/org/repo/pull/1\n"
    )
    (logs / "client-judge.log").write_text("session s2\nuser: do it\n")
    (logs / "transcript-judge.txt").write_text(
        "## user\ndo it\n\n## assistant\n(aborted by the user)\n"
    )

    baseline = mod.build_side("baseline", tmp_path)
    judge = mod.build_side("judge", tmp_path)

    assert baseline.pr_url == "https://github.com/org/repo/pull/1"
    assert baseline.aborted is False
    assert baseline.has_trace is False
    assert judge.aborted is True
    assert judge.pr_url == ""


def test_build_side_reads_trace_jsonl_when_present(tmp_path):
    mod = _load()
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "client-judge.log").write_text(
        "session s2\nuser: do it\n"
        "agent started coder deadbeefdeadbeefdeadbeefdeadbeef batch=after ask task=build it\n"
        "judge call_verify -> allow (enforced, 5ms) [deadbeefdeadbeefdeadbeefdeadbeef]: create_file(...)\n"
        "  tool_suits_request=0.90\n"
        "agent finished coder deadbeefdeadbeefdeadbeefdeadbeef ok: $0.10 100 tok 10 cached done\n"
    )
    (logs / "transcript-judge.txt").write_text("## user\ndo it\n")

    trace_dir = tmp_path / "ws-judge" / ".engine"
    trace_dir.mkdir(parents=True)
    (trace_dir / "trace.jsonl").write_text(
        json.dumps(
            {
                "kind": "judge",
                "tag": "call_verify",
                "request": {"call": {"name": "create_file"}},
                "response": {"allow": {"noul": 0.9}},
            }
        )
        + "\n"
        + json.dumps(
            {
                "kind": "tool",
                "name": "create_file",
                "agent_id": "deadbeefdeadbeefdeadbeefdeadbeef",
                "arguments": {"path": "a.go"},
                "result": "ok",
                "ok": True,
                "duration_ms": 12,
                "reasoning": "I need to add the file because X.",
            }
        )
        + "\n"
    )

    judge = mod.build_side("judge", tmp_path)
    assert judge.has_trace is True
    phase = judge.phases[0]
    assert phase.judge_calls[0].request == {"call": {"name": "create_file"}}
    assert phase.judge_calls[0].response == {"allow": {"noul": 0.9}}
    assert len(phase.trace_tools) == 1
    assert phase.trace_tools[0]["reasoning"] == "I need to add the file because X."


def test_parse_summary_table_survives_overflowing_columns():
    mod = _load()
    text = (
        "A/B result\n"
        "                       baseline            judge\n"
        "cost USD                 0.9756           1.6758\n"
        "  profiles            ask,coder ask,coder,reviewer\n"
        "files changed                 1                1\n"
        "PR             https://x/pull/1; https://x/pull/1**                \xe2\x80\x94\n"
    )
    rows = mod.parse_summary_table(text)
    assert rows["cost usd"] == {"baseline": "0.9756", "judge": "1.6758"}
    assert rows["profiles"] == {"baseline": "ask,coder", "judge": "ask,coder,reviewer"}
    assert rows["files changed"] == {"baseline": "1", "judge": "1"}


def test_svg_metric_grid_renders_both_series():
    mod = _load()
    out = mod.svg_metric_grid([("Cost", 1.0, 2.0, "$")])
    assert "<svg" in out
    assert mod._C_BASE in out
    assert mod._C_JUDGE in out
    assert "$1.00" in out and "$2.00" in out


def test_svg_verdict_grid_colors_by_status():
    mod = _load()
    calls = [
        mod.JudgeCall("call_verify", "allow", "enforced", 5, "a1", "x", {}, 0),
        mod.JudgeCall("call_verify", "block", "enforced", 5, "a1", "x", {}, 1),
    ]
    out = mod.svg_verdict_grid(calls)
    assert mod._C_GOOD in out  # allow
    assert mod._C_CRIT in out  # block
    assert "allow×1" in out and "block×1" in out


def test_svg_verdict_grid_empty_is_a_hint_not_a_crash():
    mod = _load()
    assert "no judge activity" in mod.svg_verdict_grid([])


def test_render_judge_call_detail_shows_request_response_when_present():
    mod = _load()
    call = mod.JudgeCall(
        "call_verify", "allow", "enforced", 5, "a1", "x", {"noul": 0.9}, 0,
        request={"call": {"name": "create_file"}},
        response={"allow": {"noul": 0.9}},
    )
    out = mod.render_judge_call_detail(call)
    assert "create_file" in out
    assert "noul" in out
    assert "not captured" not in out


def test_render_judge_call_detail_notes_missing_trace():
    mod = _load()
    call = mod.JudgeCall("call_verify", "allow", "enforced", 5, "a1", "x", {}, 0)
    out = mod.render_judge_call_detail(call)
    assert "not captured" in out


def test_render_tool_call_detail_shows_reasoning():
    mod = _load()
    rec = {
        "name": "create_file",
        "ok": True,
        "duration_ms": 12,
        "arguments": {"path": "a.go"},
        "result": "wrote 4 lines",
        "reasoning": "I need this file because the settings modal needs a config struct.",
    }
    out = mod.render_tool_call_detail(rec)
    assert "needs a config struct" in out
    assert "a.go" in out
    assert "wrote 4 lines" in out


def test_render_comparison_mermaid_is_nonempty_and_balanced(tmp_path):
    mod = _load()
    baseline = mod.SideFlow(name="baseline", pr_url="https://x/pull/1")
    judge = mod.SideFlow(name="judge", aborted=True)
    out = mod.render_comparison_mermaid({"baseline": baseline, "judge": judge})
    assert out.startswith("flowchart LR")
    assert out.count("subgraph") == 2
    assert out.count("end") >= 2
