from __future__ import annotations

import asyncio

from agents.compactor import CONTEXT_MD_CAP, SUMMARY_CLIP, compress_for_parent, write_context_md
from agents.profile import REPORT_TO_ORCH, TEST_GLOBS, discover_profiles
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
    coder = tools.subset(profiles.get("coder").tool_names)
    assert "str_replace" in coder.names()
    assert "ask" not in coder.names()
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
    assert len(result.outcome) <= SUMMARY_CLIP
    assert len(result.summary) <= SUMMARY_CLIP


def test_report_to_orch_is_defined():
    assert "orchestrator" in REPORT_TO_ORCH
    assert "markdown" in REPORT_TO_ORCH


def test_write_context_trims(tmp_path):
    note = "n" * (CONTEXT_MD_CAP + 50)
    write_context_md(tmp_path, note)
    text = (tmp_path / ".engine" / "context.md").read_text()
    assert len(text) <= CONTEXT_MD_CAP
    write_context_md(tmp_path, "tail-unique")
    text = (tmp_path / ".engine" / "context.md").read_text()
    assert "tail-unique" in text
    assert len(text) <= CONTEXT_MD_CAP
