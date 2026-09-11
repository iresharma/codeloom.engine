from __future__ import annotations

import asyncio
import json

from agents.compactor import (
    OUTCOME_CLIP,
    SUMMARY_CLIP,
    compress_for_parent,
)
from agents.profile import REPORT_TO_ORCH, TEST_GLOBS, discover_profiles
from llm.provider import LLMResult
from runtime.tools.edits import apply_edit
from runtime.tools.fileid import read_source
from tools.registry import discover_tools


def test_discover_builtin_profiles():
    registry = discover_profiles()
    assert {"ask", "coder", "tester", "researcher", "debugger", "reviewer"} <= registry.names()
    assert not registry.errors
    for name in registry.names():
        tools = registry.get(name).tool_names
        assert "activate_skill" in tools
        assert "read_skill" in tools
        assert "remember" in tools
    assert "mcp" in registry.get("researcher").tool_names
    assert "mcp" in registry.get("debugger").tool_names
    assert "mcp" not in registry.get("coder").tool_names
    assert "mcp" not in registry.get("ask").tool_names


def test_duplicate_profile_recorded():
    registry = discover_profiles()
    first = registry.get("ask")
    registry.register(first)
    assert any("duplicate profile" in item for item in registry.errors)


def test_orch_and_profile_allowlists():
    tools = discover_tools()
    profiles = discover_profiles()
    orch = tools.subset([])
    for spec in profiles.as_tools(_spawn):
        orch.register(spec)
    names = orch.names()
    assert "ask" in names
    assert "coder" in names
    assert "list_files" not in names
    assert "read_file" not in names
    assert "search" not in names
    assert "str_replace" not in names
    assert "run_command" not in names
    ask = tools.subset(profiles.get("ask").tool_names)
    assert "str_replace" not in ask.names()
    assert "run_command" not in ask.names()
    assert "list_files" in ask.names()
    assert "todo_scan" in ask.names()
    assert "gh_pr_view" not in ask.names()
    coder = tools.subset(profiles.get("coder").tool_names)
    assert "str_replace" in coder.names()
    assert "ask" not in coder.names()
    assert "git_log" in coder.names()
    assert "tldr" in coder.names()
    assert "gh_pr_view" not in coder.names()
    assert "gh_pr_create" not in coder.names()
    reviewer = tools.subset(profiles.get("reviewer").tool_names)
    assert "gh_pr_view" in reviewer.names()
    assert "github_repo" in reviewer.names()
    assert "github_tree" in reviewer.names()
    assert "gh_pr_create" not in reviewer.names()
    assert "gh_pr_comment" not in reviewer.names()
    researcher = tools.subset(profiles.get("researcher").tool_names)
    assert "gh_pr_comment" not in researcher.names()
    assert "gh_pr_view" not in researcher.names()
    assert "docs_lookup" in researcher.names()
    assert "github_repo" in researcher.names()
    assert "github_tree" in researcher.names()
    assert "web_fetch" in researcher.names()
    assert "web_search" in researcher.names()
    assert "gh_pr_create" not in researcher.names()
    assert "run_command" not in researcher.names()
    assert "str_replace" not in researcher.names()
    assert "get_diagnostics" not in researcher.names()
    assert "browser_open" not in researcher.names()
    researcher_profile = profiles.get("researcher")
    assert researcher_profile.write_globs == []
    prompt = researcher_profile.system_prompt
    assert "github_repo" in prompt
    assert "github_tree" in prompt
    assert "Do not web_fetch github.com HTML" in prompt
    assert "docs_lookup" in prompt
    assert "A README title is not a survey" in prompt
    assert "fetch a known URL" not in prompt
    assert "GitHub repo" in researcher_profile.description
    from agents.orchestrator import ORCH_SYSTEM

    assert "GitHub repo" in ORCH_SYSTEM
    assert "For external docs, spawn researcher" not in ORCH_SYSTEM
    assert "Check workspace memory before spawning ask" in ORCH_SYSTEM
    assert "For code questions, spawn ask" not in ORCH_SYSTEM
    debugger = tools.subset(profiles.get("debugger").tool_names)
    assert "gh_pr_comment" in debugger.names()
    assert "http_request" in debugger.names()
    assert "github_tree" in debugger.names()
    tester_tools = tools.subset(profiles.get("tester").tool_names)
    assert "http_request" in tester_tools.names()
    assert "gh_pr_view" not in tester_tools.names()
    tester = profiles.get("tester")
    assert tester.write_globs == list(TEST_GLOBS)
    assert tester.required_tools == ["run_command"]
    assert profiles.get("coder").required_tools == ["get_diagnostics"]
    assert profiles.get("coder").needs_worktree is True
    assert profiles.get("tester").needs_worktree is True
    assert profiles.get("ask").needs_worktree is False
    assert profiles.get("debugger").needs_worktree is False
    assert profiles.get("reviewer").needs_worktree is False
    assert profiles.get("reviewer").join_worktree is True
    assert profiles.get("ask").max_turns == 32
    assert profiles.get("coder").max_turns == 32
    assert profiles.get("tester").max_turns == 32
    assert profiles.get("debugger").max_turns == 32
    assert profiles.get("reviewer").max_turns == 32
    assert profiles.get("researcher").max_turns == 32
    assert profiles.get("ask").model == "anthropic/claude-haiku-4.5"
    assert profiles.get("tester").model == "anthropic/claude-haiku-4.5"
    assert profiles.get("researcher").model is None


async def _spawn(name: str, task: str) -> str:
    return f"{name}:{task}"


def test_tester_write_globs(ctx):
    ctx.profile = "tester"
    ctx.write_globs = list(TEST_GLOBS)
    (ctx.workspace / "runtime").mkdir()
    (ctx.workspace / "runtime" / "session.py").write_text("x = 1\n")
    (ctx.workspace / "tests").mkdir()
    (ctx.workspace / "tests" / "test_foo.py").write_text("x = 1\n")
    src = read_source(ctx.workspace, "runtime/session.py")
    ctx.files.mark(src.rel, src.raw_sha256)
    src2 = read_source(ctx.workspace, "tests/test_foo.py")
    ctx.files.mark(src2.rel, src2.raw_sha256)

    async def run():
        denied = await apply_edit(
            ctx, "runtime/session.py", lambda s: "y = 1\n", "str_replace"
        )
        allowed = await apply_edit(
            ctx, "tests/test_foo.py", lambda s: "y = 1\n", "str_replace"
        )
        return denied, allowed

    denied, allowed = asyncio.run(run())
    assert denied.startswith("error:")
    assert "cannot write" in denied
    assert allowed.startswith("ok:")


def test_coder_incomplete_without_diagnostics():
    async def run():
        return await compress_for_parent(
            [
                {"role": "system", "content": "coder"},
                {"role": "user", "content": "edit"},
                {"role": "assistant", "content": "done"},
            ],
            required_tools=["get_diagnostics"],
            tools_called=set(),
        )

    result = asyncio.run(run())
    assert result.status == "incomplete"
    assert "get_diagnostics" in result.missing_checks


def test_tester_incomplete_without_run_command():
    async def run():
        missing = await compress_for_parent(
            [{"role": "user", "content": "test"}, {"role": "assistant", "content": "ok"}],
            required_tools=["run_command"],
            tools_called=set(),
        )
        ok = await compress_for_parent(
            [{"role": "user", "content": "test"}, {"role": "assistant", "content": "ok"}],
            required_tools=["run_command"],
            tools_called={"run_command"},
        )
        return missing, ok

    missing, ok = asyncio.run(run())
    assert missing.status == "incomplete"
    assert ok.status == "ok"


def test_compress_drops_tool_transcript():
    async def run():
        return await compress_for_parent(
            [
                {"role": "system", "content": "ask"},
                {"role": "user", "content": "where?"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "1",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"path": "a.py"}',
                            },
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "1", "content": "huge " * 500},
                {"role": "assistant", "content": "it is in a.py"},
            ],
            files_touched=["a.py"],
        )

    result = asyncio.run(run())
    assert "huge" not in result.as_text()
    assert "a.py" in result.files_touched
    assert result.outcome == "it is in a.py"


def test_compress_clips_long_closer():
    async def run():
        return await compress_for_parent(
            [
                {"role": "user", "content": "x"},
                {"role": "assistant", "content": "# Hello\n\n" + ("word " * 200)},
            ]
        )

    result = asyncio.run(run())
    assert len(result.outcome) <= OUTCOME_CLIP
    assert len(result.summary) <= SUMMARY_CLIP


def test_outcome_keeps_long_facts():
    facts = "facts: " + ("src/app.py Foo.bar retries 3 times using tenacity; " * 20)
    closer = "\n".join(
        [
            "what: surveyed acme/engine",
            "paths: README.md, src/app.py",
            facts,
            "verdict: retry lives in Foo.bar",
            "leftover: confirm v2 API",
        ]
    )
    assert len(closer) > SUMMARY_CLIP

    async def run():
        return await compress_for_parent(
            [
                {"role": "user", "content": "x"},
                {"role": "assistant", "content": closer},
            ]
        )

    result = asyncio.run(run())
    assert len(result.outcome) > SUMMARY_CLIP
    assert len(result.outcome) <= OUTCOME_CLIP
    assert "retries 3 times" in result.outcome
    assert len(result.summary) <= SUMMARY_CLIP


def test_compress_prompt_keeps_facts():
    captured = []

    async def complete(prompt):
        captured.append(prompt[0]["content"])
        return LLMResult(text="what: ok\nfacts: Foo.bar retries")

    async def run():
        messages = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
            {"role": "assistant", "content": "d"},
            {"role": "user", "content": "e"},
            {"role": "assistant", "content": "done"},
        ]
        return await compress_for_parent(messages, complete=complete)

    result = asyncio.run(run())
    assert captured
    text = captured[0]
    assert "facts must be specific" in text
    assert "Do not collapse a survey into a one-liner" in text
    assert "6 short labeled lines" not in text
    assert "Foo.bar retries" in result.summary


def test_compress_skips_llm_when_labeled():
    called = []

    async def complete(prompt):
        called.append(prompt)
        return LLMResult(text="should not run")

    closer = "\n".join(
        [
            "what: surveyed acme/engine",
            "paths: README.md",
            "facts: Foo.bar retries",
            "verdict: skip always-on",
        ]
    )
    messages = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"},
        {"role": "assistant", "content": "d"},
        {"role": "user", "content": "e"},
        {"role": "assistant", "content": closer},
    ]

    async def run():
        return await compress_for_parent(messages, complete=complete)

    result = asyncio.run(run())
    assert called == []
    assert "Foo.bar retries" in result.outcome
    assert "surveyed" in result.summary


def test_compress_runs_when_what_prefix_is_not_a_label():
    called = []

    async def complete(prompt):
        called.append(prompt)
        return LLMResult(text="what: ok\nfacts: recovered")

    closer = "\n".join(
        [
            "what_if_we_try_this: maybe",
            "facts: leftover prose",
        ]
    )
    messages = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"},
        {"role": "assistant", "content": "d"},
        {"role": "user", "content": "e"},
        {"role": "assistant", "content": closer},
    ]

    async def run():
        return await compress_for_parent(messages, complete=complete)

    result = asyncio.run(run())
    assert called
    assert "recovered" in result.summary


def test_report_to_orch_is_defined():
    assert "orchestrator" in REPORT_TO_ORCH
    assert "markdown" in REPORT_TO_ORCH


def _attempted_required(name="get_diagnostics", path="a.py"):
    return [
        {"role": "system", "content": "coder"},
        {"role": "user", "content": "edit"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "1",
                    "function": {
                        "name": name,
                        "arguments": json.dumps({"path": path}),
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "1", "content": "cancelled"},
        {"role": "assistant", "content": "done"},
    ]


def test_empty_tools_called_does_not_scan_history():
    async def run():
        return await compress_for_parent(
            _attempted_required(),
            required_tools=["get_diagnostics"],
            tools_called=set(),
        )

    result = asyncio.run(run())
    assert result.status == "incomplete"
    assert "get_diagnostics" in result.missing_checks


def test_empty_files_touched_does_not_scan_history():
    async def run():
        return await compress_for_parent(
            _attempted_required(),
            files_touched=[],
        )

    result = asyncio.run(run())
    assert result.files_touched == []


def test_paths_from_history_fallback_reads_dest_and_file():
    async def run():
        return await compress_for_parent(
            [
                {"role": "user", "content": "x"},
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "1",
                            "function": {
                                "name": "copy",
                                "arguments": json.dumps(
                                    {
                                        "dest": "out.py",
                                        "file": "src.py",
                                        "glob": "*.py",
                                    }
                                ),
                            },
                        }
                    ],
                },
            ]
        )

    result = asyncio.run(run())
    assert result.files_touched == ["src.py", "out.py"]
    assert "*.py" not in result.files_touched


def test_leftover_from_closer():
    async def run():
        return await compress_for_parent(
            [
                {"role": "user", "content": "x"},
                {
                    "role": "assistant",
                    "content": "verdict: ok\nleftover: confirm the API; which branch?",
                },
            ]
        )

    result = asyncio.run(run())
    assert result.leftover_questions == ["confirm the API", "which branch?"]
    assert "leftover_questions:" in result.as_text()


def test_summarize_keeps_outcome_distinct_and_parses_leftover():
    captured = []

    async def complete(prompt):
        captured.append(prompt)
        return LLMResult(
            text="what: found retry\npaths: a.py\nverdict: ok\nleftover: confirm API"
        )

    async def run():
        messages = [
            {"role": "user", "content": "where?"},
            {"role": "assistant", "content": "looking"},
            {"role": "user", "content": "again"},
            {"role": "assistant", "content": "still"},
            {"role": "user", "content": "now"},
            {"role": "assistant", "content": "it is in a.py"},
        ]
        return await compress_for_parent(messages, complete=complete)

    result = asyncio.run(run())
    assert captured
    assert result.outcome == "it is in a.py"
    assert result.summary != result.outcome
    assert "found retry" in result.summary
    assert result.leftover_questions == ["confirm API"]
    text = result.as_text()
    assert "outcome: it is in a.py" in text
    assert "summary: " in text


def test_summarize_failure_is_visible():
    async def complete(prompt):
        raise TimeoutError("llm down")

    async def run():
        messages = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
            {"role": "assistant", "content": "d"},
            {"role": "user", "content": "e"},
            {"role": "assistant", "content": "closer text"},
        ]
        return await compress_for_parent(messages, complete=complete)

    result = asyncio.run(run())
    assert result.status == "ok"
    assert result.summary == "summarize failed: TimeoutError"
    assert result.outcome == "closer text"


def test_summary_clips_on_full_line():
    report = "\n".join(
        [
            "what: " + ("w" * 220),
            "paths: a.py",
            "verdict: ship-it-now",
            "leftover: confirm API",
            "notes: " + ("p" * 200),
        ]
    )

    async def complete(prompt):
        return LLMResult(text=report)

    async def run():
        messages = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
            {"role": "assistant", "content": "d"},
            {"role": "user", "content": "e"},
            {"role": "assistant", "content": "done"},
        ]
        return await compress_for_parent(messages, complete=complete)

    result = asyncio.run(run())
    assert len(result.summary) <= SUMMARY_CLIP
    assert "ship-it-now" in result.summary
    assert result.summary.endswith("leftover: confirm API")
    assert "ppp" not in result.summary
    assert result.leftover_questions == ["confirm API"]


def test_compress_summarize_keeps_tail():
    captured = []

    async def complete(prompt):
        captured.append(prompt[1]["content"])
        return LLMResult(text="what: ok")

    async def run():
        messages = [
            {"role": "user", "content": "head-marker"},
            {"role": "assistant", "content": "mid-a " * 4000},
            {"role": "user", "content": "mid-b " * 4000},
            {"role": "assistant", "content": "mid-c " * 4000},
            {"role": "user", "content": "mid-d " * 4000},
            {"role": "assistant", "content": "UNIQUE_TAIL closer"},
        ]
        return await compress_for_parent(messages, complete=complete)

    result = asyncio.run(run())
    assert captured
    payload = captured[0]
    assert "UNIQUE_TAIL" in payload
    assert "head-marker" in payload
    assert "middle omitted" in payload
    assert result.outcome == "UNIQUE_TAIL closer"


def test_as_text_omits_duplicate_outcome():
    async def run():
        return await compress_for_parent(
            [
                {"role": "user", "content": "x"},
                {"role": "assistant", "content": "same closer"},
            ]
        )

    result = asyncio.run(run())
    text = result.as_text()
    assert "summary: same closer" in text
    assert "outcome:" not in text


def test_finish_preserves_status_on_compaction_error(tmp_path):
    from unittest.mock import patch

    from agents.profiles.ask import PROFILE
    from agents.subagent import Subagent
    from tests.fakes import FakeProvider
    from tools.registry import ToolRegistry

    child = Subagent(
        PROFILE,
        llm=FakeProvider(),
        tools=ToolRegistry(),
        workspace=tmp_path,
    )

    async def boom(*_args, **_kwargs):
        raise RuntimeError("paths exploded")

    async def run():
        with patch("agents.subagent.compress_for_parent", boom):
            return await child.finish("ok")

    result = asyncio.run(run())
    assert result.status == "ok"
    assert result.outcome.startswith("compaction error:")
    assert "paths exploded" in result.outcome
