from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from agents.compactor import SUMMARY_CLIP, compress_for_parent
from agents.orchestrator import Orchestrator, _reviewer_task, _tester_task
from agents.profiles.coder import PROFILE as CODER
from agents.profiles.debugger import PROFILE as DEBUGGER
from agents.profiles.researcher import PROFILE as RESEARCHER
from agents.profiles.reviewer import PROFILE as REVIEWER
from agents.profiles.tester import PROFILE as TESTER
from agents.subagent import Subagent
from llm.provider import LLMResult, ToolCall
from protocol.events import AgentStarted
from runtime.config import EngineConfig
from runtime.judge_decisions import VERIFY_TOOLS, classify_loop
from tests.fakes import FakeProvider
from tests.test_orchestrator import _bind, _queued, _wait_idle
from tools.registry import discover_tools


def _init_repo_with_tests(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.t"], cwd=path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=path, check=True, capture_output=True
    )
    (path / "app.py").write_text("VALUE = 0\n")
    (path / "tests").mkdir()
    (path / "tests" / "test_app.py").write_text("def test_value():\n    assert True\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "i"], cwd=path, check=True, capture_output=True
    )


def _tool_names_from_messages(messages) -> list[str]:
    names: list[str] = []
    for message in messages:
        for call in message.get("tool_calls") or []:
            fn = call.get("function") if isinstance(call.get("function"), dict) else {}
            names.append(fn.get("name") or call.get("name") or "")
    return names


def _last_user(messages) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return str(message.get("content") or "")
    return ""


_CLOSER = (
    "what: added FLAG\n"
    "paths: flag.py, test_flag.py\n"
    "facts: FLAG is 1\n"
    "verdict: done\n"
    "reasoning: Used a module constant so callers import FLAG directly. "
    "Rejected a getter; nothing needs lazy init.\n"
    "Look at the export surface.\n"
    "test_plan: python -c \"from flag import FLAG; assert FLAG == 1\"\n"
    "Also run pytest test_flag.py. Expect pass. Edge: FLAG is int, not str.\n"
    "checks: get_diagnostics on flag.py and test_flag.py"
)


class _CoderSourceOnlyThenTest(FakeProvider):
    """Tries to finish after a source edit; writes the test only after the nudge."""

    def __init__(self):
        super().__init__()
        self.saw_nudge = False

    async def complete(self, messages, tools=None, *, on_delta=None, **kwargs):
        called = _tool_names_from_messages(messages)
        if "Not finished yet" in _last_user(messages):
            self.saw_nudge = True
        if "create_file" not in called:
            return LLMResult(
                text="",
                tool_calls=[
                    ToolCall(
                        id="src",
                        name="create_file",
                        arguments_json=json.dumps(
                            {"path": "nudge.py", "content": "N = 1\n"}
                        ),
                    )
                ],
            )
        if "get_diagnostics" not in called:
            return LLMResult(
                text="",
                tool_calls=[
                    ToolCall(
                        id="diag",
                        name="get_diagnostics",
                        arguments_json='{"path":"nudge.py"}',
                    )
                ],
            )
        if not self.saw_nudge:
            return LLMResult(
                text="what: source only\npaths: nudge.py\nfacts: N=1\nverdict: done"
            )
        if called.count("create_file") < 2:
            return LLMResult(
                text="",
                tool_calls=[
                    ToolCall(
                        id="test",
                        name="create_file",
                        arguments_json=json.dumps(
                            {
                                "path": "tests/test_nudge.py",
                                "content": "from nudge import N\n\ndef test_n():\n    assert N == 1\n",
                            }
                        ),
                    )
                ],
            )
        return LLMResult(
            text=(
                "what: added N and a test\n"
                "paths: nudge.py, test_nudge.py\n"
                "facts: N is 1\n"
                "verdict: done\n"
                "reasoning: Constant is enough.\n"
                "test_plan: pytest test_nudge.py\n"
            )
        )


def _patch_child_run(fn):
    orig = Subagent.run
    Subagent.run = fn  # type: ignore[method-assign]
    return orig


def test_coder_files_touched_spawns_tester_and_reviewer_and_holds_settle(tmp_path):
    hang = asyncio.Event()

    async def fake_run(self, task):
        self._history.append({"role": "user", "content": task})
        if self.profile == "coder":
            (self._ctx.workspace / "flag.py").write_text("FLAG = 1\n")
            (self._ctx.workspace / "test_flag.py").write_text(
                "from flag import FLAG\n\ndef test_flag():\n    assert FLAG == 1\n"
            )
            self._files_touched.extend(["flag.py", "test_flag.py"])
            self._tools_called.add("get_diagnostics")
            self._history.append({"role": "assistant", "content": _CLOSER})
            return _CLOSER
        await hang.wait()
        if self.profile == "tester":
            self._tools_called.add("run_command")
            text = "what: tests\npaths: test_flag.py\nfacts: pass\nverdict: pass"
            self._history.append({"role": "assistant", "content": text})
            return text
        text = "what: review\npaths: flag.py\nfacts: ok\nverdict: approve"
        self._history.append({"role": "assistant", "content": text})
        return text

    async def run():
        _init_repo_with_tests(tmp_path)
        session = await _bind(tmp_path, FakeProvider())()
        queue = session.subscribe()
        while not queue.empty():
            queue.get_nowait()
        orch: Orchestrator = session._loop
        orch._user_task = "add a FLAG helper"
        orig = _patch_child_run(fake_run)
        try:
            text = await orch.spawn("coder", "add FLAG = 1 in flag.py")
            assert text.startswith("started")
            coder_id = text.split("agent_id=")[1].split()[0]
            await orch.wait_children()
            started = [item for item in _queued(queue) if isinstance(item, AgentStarted)]
            profiles = {item.profile for item in started}
            assert profiles >= {"coder", "tester", "reviewer"}
            tester = next(item for item in started if item.profile == "tester")
            reviewer = next(item for item in started if item.profile == "reviewer")
            dest = orch._worktrees[coder_id]
            assert Path(tester.worktree).resolve() == dest.resolve()
            assert "Coder's test plan:" in tester.task
            assert "python -c" in tester.task
            assert "add a FLAG helper" in tester.task
            assert "flag.py" in tester.task
            assert "Coder's reasoning:" in reviewer.task
            assert "module constant" in reviewer.task
            assert coder_id in orch._verifying
            assert coder_id in orch._deferred_settles
            assert coder_id not in orch._pending_settles
            hang.set()
            await orch.wait_children()
            assert not orch._verifying.get(coder_id)
            assert coder_id not in orch._deferred_settles
        finally:
            Subagent.run = orig  # type: ignore[method-assign]

    asyncio.run(run())


def test_coder_without_files_touched_does_not_verify(tmp_path):
    async def fake_run(self, task):
        self._tools_called.add("get_diagnostics")
        return "what: nothing to edit\npaths:\nfacts: already done\nverdict: no change"

    async def run():
        _init_repo_with_tests(tmp_path)
        session = await _bind(tmp_path, FakeProvider())()
        queue = session.subscribe()
        while not queue.empty():
            queue.get_nowait()
        orch: Orchestrator = session._loop
        orig = _patch_child_run(fake_run)
        try:
            await orch.spawn("coder", "already done")
            await orch.wait_children()
            await _wait_idle(session)
        finally:
            Subagent.run = orig  # type: ignore[method-assign]
        started = [item for item in _queued(queue) if isinstance(item, AgentStarted)]
        assert {item.profile for item in started} == {"coder"}
        assert not orch._verifying
        assert not orch._deferred_settles
        assert not orch._pending_settles

    asyncio.run(run())


def test_stopped_coder_does_not_settle(tmp_path):
    async def run():
        _init_repo_with_tests(tmp_path)
        session = await _bind(tmp_path, FakeProvider())()
        orch: Orchestrator = session._loop

        async def stopped(self, task):
            self._files_touched.append("app.py")
            self._tools_called.add("get_diagnostics")
            self._exit_status = "stopped"
            return "stopped mid-edit"

        orig = _patch_child_run(stopped)
        try:
            text = await orch.spawn("coder", "add a flag")
            coder_id = text.split("agent_id=")[1].split()[0]
            await orch.wait_children()
        finally:
            Subagent.run = orig  # type: ignore[method-assign]
        await _wait_idle(session)
        assert coder_id not in orch._pending_settles
        assert coder_id not in orch._deferred_settles
        assert not orch._verifying

    asyncio.run(run())


def test_coder_stays_alive_until_test_edit(tmp_path):
    async def run():
        _init_repo_with_tests(tmp_path)
        provider = _CoderSourceOnlyThenTest()
        tools = discover_tools().subset(CODER.tool_names, profile="coder")
        child = Subagent(
            CODER,
            llm=provider,
            tools=tools,
            workspace=tmp_path,
            journal=tmp_path / "session.db",
            config=EngineConfig(max_turns=8),
            lsp=None,
        )
        text = await child.run("add N")
        assert provider.saw_nudge
        assert "tests/test_nudge.py" in child._files_touched
        assert "nudge.py" in child._files_touched
        assert "get_diagnostics" in child._tools_called
        assert "verdict: done" in text

    asyncio.run(run())


def test_finish_nudge_requires_test_path(tmp_path):
    async def run():
        _init_repo_with_tests(tmp_path)
        tools = discover_tools().subset(CODER.tool_names, profile="coder")
        child = Subagent(
            CODER,
            llm=FakeProvider(),
            tools=tools,
            workspace=tmp_path,
            journal=tmp_path / "session.db",
            config=EngineConfig(max_turns=8),
        )
        child._tools_called.add("get_diagnostics")
        child._files_touched.append("app.py")
        child._repo_has_tests = True
        nudge = await child._finish_nudge()
        assert nudge is not None
        assert "app.py" in nudge
        assert "without adding or updating one" in nudge
        child._files_touched.append("tests/test_app.py")
        assert await child._finish_nudge() is None

    asyncio.run(run())


def test_handoff_fields_survive_summary_clip():
    async def run():
        reasoning = "R" * (SUMMARY_CLIP + 80)
        plan = "P" * (SUMMARY_CLIP + 40)
        closer = (
            f"what: change\npaths: a.py\nfacts: x\nverdict: ok\n"
            f"reasoning: {reasoning}\n"
            f"test_plan: {plan}\n"
        )
        result = await compress_for_parent(
            [{"role": "user", "content": "do it"}, {"role": "assistant", "content": closer}],
            status="ok",
            files_touched=["a.py", "test_a.py"],
            tools_called={"get_diagnostics"},
        )
        assert result.reasoning.startswith("R")
        assert len(result.reasoning) > SUMMARY_CLIP
        assert result.test_plan.startswith("P")
        assert len(result.test_plan) > SUMMARY_CLIP
        assert result.files_touched == ["a.py", "test_a.py"]

    asyncio.run(run())


def test_tester_and_reviewer_task_copy_handoff():
    tester = _tester_task("user asked for FLAG", "add FLAG", "flag.py", "run pytest")
    reviewer = _reviewer_task("user asked for FLAG", "add FLAG", "flag.py", "constant is enough")
    assert "user asked for FLAG" in tester
    assert "run pytest" in tester
    assert "flag.py" in tester
    assert "constant is enough" in reviewer
    assert "user asked for FLAG" in reviewer


def test_reviewer_task_clips_long_handoff():
    from agents.orchestrator import _REVIEW_CLIP

    blob = "X" * (_REVIEW_CLIP + 200)
    reviewer = _reviewer_task(blob, blob, "flag.py", blob)
    assert reviewer.count("... (truncated)") == 3
    assert blob not in reviewer


def test_reviewer_is_diff_first_not_a_survey():
    assert REVIEWER.max_turns == 12
    assert REVIEWER.required_tools == ["git_diff"]
    names = set(REVIEWER.tool_names)
    assert "git_diff" in names
    assert "read_file" in names
    assert "list_files" not in names
    assert "search" not in names
    assert "github_repo" not in names
    assert "github_tree" not in names
    assert "github_search_code" not in names


def test_verify_owner_without_worktree_errors(tmp_path):
    async def run():
        _init_repo_with_tests(tmp_path)
        session = await _bind(tmp_path, FakeProvider())()
        orch: Orchestrator = session._loop
        text = await orch.spawn("tester", "prove it", verify_owner="missing")
        assert text.startswith("error:")
        assert "no open worktree" in text

    asyncio.run(run())


def test_edit_tools_are_not_call_verified():
    assert "str_replace" not in VERIFY_TOOLS
    assert "replace_lines" not in VERIFY_TOOLS
    assert "create_file" not in VERIFY_TOOLS
    assert "replace_symbol" not in VERIFY_TOOLS
    assert "apply_patch" not in VERIFY_TOOLS


def test_classify_loop_never_stops():
    class _V:
        def noul(self, name):
            scores = {
                "making_progress": 0.0,
                "repeating_itself": 1.0,
                "needs_user_input": 0.0,
                "appears_complete": 0.0,
            }
            return scores[name]

    assert classify_loop(_V()) == "continue"
    assert classify_loop(None) == "continue"


def test_researcher_and_debugger_keep_external_tools():
    assert "web_search" in RESEARCHER.tool_names
    assert "web_fetch" in RESEARCHER.tool_names
    assert "github_repo" in RESEARCHER.tool_names
    assert "github_tree" in RESEARCHER.tool_names
    assert "github_file" in RESEARCHER.tool_names
    assert "github_search_code" in RESEARCHER.tool_names
    assert "browser_open" in DEBUGGER.tool_names
    assert "browser_console" in DEBUGGER.tool_names
    assert "browser_screenshot" in DEBUGGER.tool_names
    assert "browser_network" in DEBUGGER.tool_names
    assert "run_command" in DEBUGGER.tool_names
    assert TESTER.needs_worktree
    assert CODER.requires_tests


def test_route_prompts_name_the_lanes():
    from agents.orchestrator import ORCH_SYSTEM

    assert "researcher" in ORCH_SYSTEM
    assert "debugger" in ORCH_SYSTEM
    assert "Never spawn coder for research or debug" in ORCH_SYSTEM
    assert "engine starts tester and reviewer" in ORCH_SYSTEM
    assert "replace_symbol" in CODER.system_prompt
    assert "test_plan" in CODER.system_prompt
    assert "coder's worktree" in TESTER.system_prompt
    assert "test_plan" in TESTER.system_prompt
    assert "reasoning" in REVIEWER.system_prompt
