"""Item 3: the harness verifies before the reviewer, and settle refuses an
unverified pull request.

Boundaries mocked: the LLM provider (FakeProvider) and, in the settle cases,
the `gh` binary via a PATH shim. Verify commands are real subprocesses
against real files in tmp_path -- `run_verify` is never stubbed.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from agents.orchestrator import Orchestrator
from agents.profile import discover_profiles
from llm.provider import LLMResult
from runtime.config import EngineConfig
from runtime.tools.git import add_agent_worktree, commit_if_dirty
from runtime.verify import (
    DisposableWorktree,
    VerifyPlan,
    VerifyResult,
    detect_verify_commands,
    format_verify_block,
    run_verify,
    settle_verify_refusal,
    task_declares_no_tests,
)
from tests.fakes import FakeProvider
from tools.registry import discover_tools


def _init_git(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, capture_output=True)
    for key, value in (("user.email", "t@t.t"), ("user.name", "t")):
        subprocess.run(
            ["git", "config", key, value], cwd=path, check=True, capture_output=True
        )
    (path / "README").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"], cwd=path, check=True, capture_output=True
    )


def _orch(repo: Path, **config_kw) -> Orchestrator:
    return Orchestrator(
        FakeProvider(),
        all_tools=discover_tools(),
        profiles=discover_profiles(),
        workspace=repo,
        config=EngineConfig(**config_kw),
    )


# --------------------------------------------------------------------------
# command choice


def test_explicit_config_beats_detection(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n")
    plan = detect_verify_commands(
        tmp_path, config=EngineConfig(verify_command="make check")
    )
    assert plan.commands == ["make check"]
    assert plan.source == "config"


def test_env_var_is_honoured_when_config_is_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("ENGINE_VERIFY_CMD", "just test")
    plan = detect_verify_commands(tmp_path, config=EngineConfig())
    assert plan.commands == ["just test"]
    assert plan.source == "config"


def test_detects_go(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n")
    plan = detect_verify_commands(tmp_path, config=EngineConfig())
    assert plan.commands == ["go build ./... && go vet ./... && go test ./..."]
    assert plan.source == "detected"


def test_detects_python_from_pytest_ini(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    plan = detect_verify_commands(tmp_path, config=EngineConfig())
    assert plan.commands == ["pytest -q"]


def test_detects_python_from_a_tests_dir(tmp_path):
    (tmp_path / "tests").mkdir()
    assert detect_verify_commands(tmp_path, config=EngineConfig()).commands == [
        "pytest -q"
    ]


def test_detects_npm_only_with_a_real_test_script(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"test": "vitest run"}}')
    assert detect_verify_commands(tmp_path, config=EngineConfig()).commands == [
        "npm test"
    ]

    (tmp_path / "package.json").write_text(
        '{"scripts": {"test": "echo \\"Error: no test specified\\" && exit 1"}}'
    )
    assert detect_verify_commands(tmp_path, config=EngineConfig()).commands == []


def test_falls_back_to_the_coders_last_successful_command(tmp_path):
    plan = detect_verify_commands(
        tmp_path, config=EngineConfig(), last_command="python3 -m unittest discover"
    )
    assert plan.commands == ["python3 -m unittest discover"]
    assert plan.source == "coder"


def test_no_command_at_all(tmp_path):
    plan = detect_verify_commands(tmp_path, config=EngineConfig())
    assert plan.commands == []
    assert plan.source == "none"


# --------------------------------------------------------------------------
# running it


@pytest.mark.asyncio
async def test_run_verify_reports_structured_pass(tmp_path):
    result = await run_verify(
        tmp_path, VerifyPlan(["printf '==== 7 passed in 0.5s ====\\n'"], "config")
    )
    assert result.ok is True
    assert result.exit_code == 0
    assert result.source == "config"


@pytest.mark.asyncio
async def test_run_verify_stops_at_the_first_failing_leg(tmp_path):
    marker = tmp_path / "second-ran"
    plan = VerifyPlan(["exit 3", f"touch {marker}"], "config")
    result = await run_verify(tmp_path, plan)
    assert result.ok is False
    assert result.exit_code == 3
    assert result.command == "exit 3"
    assert not marker.exists()


@pytest.mark.asyncio
async def test_run_verify_pytest_counts_land_in_the_block(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "pytest"
    shim.write_text(
        "#!/bin/sh\n"
        "echo '==== 90 passed, 1 failed, 3 skipped in 5.0s ===='\n"
        "exit 1\n"
    )
    shim.chmod(0o755)
    old = os.environ["PATH"]
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{old}"
    try:
        result = await run_verify(tmp_path, VerifyPlan(["pytest -q"], "detected"))
    finally:
        os.environ["PATH"] = old
    assert result.ok is False
    assert (result.passed, result.failed, result.skipped) == (90, 1, 3)
    block = format_verify_block(result)
    assert "verdict: FAILED" in block
    assert '"failed": 1' in block
    assert "90 passed, 1 failed, 3 skipped in 5.0s" in block


def test_format_verify_block_says_so_when_nothing_ran():
    block = format_verify_block(VerifyResult.unavailable("no verify command"))
    assert "not run" in block
    assert "unverified" in block


# --------------------------------------------------------------------------
# the reviewer gate


class _ReviewerSpawn(FakeProvider):
    """Orchestrator spawns one reviewer, then reports."""

    def __init__(self):
        super().__init__()
        self.reviewer_tasks: list[str] = []
        self.coder_tasks: list[str] = []
        self._turn = 0

    async def complete(self, messages, tools=None, *, on_delta=None, **kwargs):
        self.calls += 1
        names = {
            (item.get("function") or item).get("name")
            for item in (tools or [])
            if isinstance(item, dict)
        }
        if "reviewer" in names:
            self._turn += 1
            if self._turn == 1:
                from llm.provider import ToolCall

                return LLMResult(
                    text="",
                    tool_calls=[
                        ToolCall(
                            id="c1",
                            name="reviewer",
                            arguments_json='{"task": "Review the redirect cap."}',
                        )
                    ],
                )
            return LLMResult(text="started the reviewer")
        last_user = next(
            (m["content"] for m in reversed(messages) if m.get("role") == "user"), ""
        )
        if "run_verify" in names:
            # The reviewer child's own loop -- it is the only profile with
            # run_verify and no run_command.
            self.reviewer_tasks.append(last_user)
            return LLMResult(text="verdict: approve")
        self.coder_tasks.append(last_user)
        return LLMResult(text="fixed the failing test")


async def _wait_children(orch: Orchestrator, timeout: float = 20.0) -> None:
    import asyncio

    for _ in range(int(timeout / 0.02)):
        if not any(not task.done() for task in orch._child_tasks.values()):
            return
        await asyncio.sleep(0.02)
    raise AssertionError("children did not finish")


@pytest.mark.asyncio
async def test_reviewer_brief_carries_the_harness_verify_result(tmp_path):
    repo = tmp_path / "repo"
    _init_git(repo)
    provider = _ReviewerSpawn()
    orch = _orch(repo, verify_command="printf '== 12 passed in 1.0s ==\\n'")
    orch._llm = provider

    dest, branch, err = add_agent_worktree(repo, "w1", "coder")
    assert not err, err
    work = Path(dest)
    (work / "http.py").write_text("cap = 5\n")
    commit_if_dirty(work, "work")
    orch._remember_worktree("w1", work, branch, "coder", "b1")

    await orch.run("cap redirects")
    await _wait_children(orch)

    assert provider.reviewer_tasks, "the reviewer never ran"
    brief = provider.reviewer_tasks[0]
    assert "HARNESS VERIFY" in brief
    assert "verdict: PASSED" in brief
    assert "12 passed in 1.0s" in brief
    assert orch.latest_verify(work).ok is True


@pytest.mark.asyncio
async def test_failing_verify_blocks_the_reviewer_and_goes_to_the_coder(tmp_path):
    repo = tmp_path / "repo"
    _init_git(repo)
    provider = _ReviewerSpawn()
    orch = _orch(repo, verify_command="printf '== 1 failed in 1.0s ==\\n'; exit 1")
    orch._llm = provider
    results: list[tuple[str, str]] = []
    orch._on_agent_result = lambda aid, profile, text: results.append((profile, text))

    dest, branch, err = add_agent_worktree(repo, "w2", "coder")
    assert not err, err
    work = Path(dest)
    (work / "http.py").write_text("cap = 5\n")
    commit_if_dirty(work, "work")
    orch._remember_worktree("w2", work, branch, "coder", "b1")

    await orch.run("cap redirects")
    await _wait_children(orch)

    # The reviewer never got a turn.
    assert provider.reviewer_tasks == []
    reviewer_reports = [text for profile, text in results if profile == "reviewer"]
    assert reviewer_reports, "no reviewer report was emitted"
    assert "harness verify failed" in reviewer_reports[0]
    assert "status: blocked" in reviewer_reports[0]
    # And a coder slice was started in the same worktree to fix it.
    assert "fix-up slice: started agent_id=" in reviewer_reports[0]
    assert provider.coder_tasks, "no coder slice ran"
    assert "Harness verification failed" in provider.coder_tasks[0]
    assert "1 failed in 1.0s" in provider.coder_tasks[0]


@pytest.mark.asyncio
async def test_fix_up_slice_turn_budget_is_capped(tmp_path):
    repo = tmp_path / "repo"
    _init_git(repo)
    orch = _orch(repo, nit_fixup_turns=4)
    dest, branch, err = add_agent_worktree(repo, "w3", "coder")
    assert not err, err
    orch._remember_worktree("w3", Path(dest), branch, "coder", "b1")

    captured: list[int] = []
    real = orch._make_subagent

    def spy(profile, agent_id, workspace, isolated):
        child = real(profile, agent_id, workspace, isolated)
        captured.append(child._config.max_turns)
        return child

    orch._make_subagent = spy
    orch._llm = FakeProvider(results=[LLMResult(text="fixed")])
    reply = await orch._start_fix_slice("w3", "fix the failing test", reason="test")
    assert reply.startswith("started agent_id="), reply
    await _wait_children(orch)
    assert captured == [4], captured
    # The clamp is per-slice: the next ordinary coder is back to normal.
    assert orch._fix_slice_turns == 0


# --------------------------------------------------------------------------
# settle gate


def test_settle_refuses_without_a_verify_result():
    refusal = settle_verify_refusal(None, task="cap redirects")
    assert "no harness verify result" in refusal


def test_settle_refuses_on_a_failing_verify():
    result = VerifyResult(command="pytest -q", exit_code=1, runner="pytest", failed=2)
    refusal = settle_verify_refusal(result, task="cap redirects")
    assert "harness verify failed" in refusal
    assert "2 failed" in refusal


def test_settle_allows_a_passing_verify():
    result = VerifyResult(command="pytest -q", exit_code=0, runner="pytest", passed=9)
    assert settle_verify_refusal(result, task="cap redirects") == ""


def test_task_declaring_no_tests_waives_the_test_requirement():
    assert task_declares_no_tests("Edit the README; there are no tests for docs.")
    assert not task_declares_no_tests("Add tests for the redirect cap.")
    assert settle_verify_refusal(None, task="no tests in this repo") == ""


def test_no_tests_declaration_still_requires_a_passing_build():
    build = VerifyResult(command="go build ./...", exit_code=2, runner="go build")
    refusal = settle_verify_refusal(build, task="there are no tests here")
    assert "the build still failed" in refusal


@pytest.mark.asyncio
async def test_settle_pr_refuses_when_verify_failed(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_git(repo)
    orch = _orch(repo)
    dest, branch, err = add_agent_worktree(repo, "w4", "coder")
    assert not err, err
    work = Path(dest)
    (work / "http.py").write_text("cap = 5\n")
    commit_if_dirty(work, "work")
    orch._remember_worktree("w4", work, branch, "coder", "b1")
    orch._user_task = "cap redirects and add a test"
    orch._verify_by_tree[str(work.resolve())] = VerifyResult(
        command="pytest -q", exit_code=1, runner="pytest", failed=1
    )

    # A `gh` shim that records every invocation: the gate has to refuse
    # before anything is pushed, so this must never be touched.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "gh_called"
    shim = bin_dir / "gh"
    shim.write_text(f"#!/bin/sh\ntouch {record}\necho https://x/pull/1\n")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    reply = await orch.apply_named_worktree("pr", agent_id="w4")
    assert reply.startswith("error: harness verify failed"), reply
    assert not record.exists(), "settle reached gh pr create on a failing verify"
    # The tree is still open, so the coder can fix it and settle again.
    assert "w4" in orch._worktrees


# --------------------------------------------------------------------------
# run_verify cannot touch the real worktree


@pytest.mark.asyncio
async def test_run_verify_tool_runs_in_a_copy_and_leaves_the_worktree_alone(tmp_path):
    from tools.base import ToolContext
    from tools.verify import run_verify as run_verify_tool

    (tmp_path / "http.py").write_text("MAX = 5\n")
    marker = tmp_path / "side-effect"
    ctx = ToolContext(
        workspace=tmp_path,
        config=EngineConfig(),
        verify_command=f"touch {marker.name} && cat http.py",
    )
    out = await run_verify_tool(ctx)
    assert "MAX = 5" in out
    # The command really ran -- but in the copy, so the real tree is clean.
    assert not marker.exists()
    assert (tmp_path / "http.py").read_text() == "MAX = 5\n"


@pytest.mark.asyncio
async def test_run_verify_mutation_applies_only_to_the_copy(tmp_path):
    from tools.base import ToolContext
    from tools.verify import run_verify as run_verify_tool

    (tmp_path / "http.py").write_text("MAX = 5\n")
    ctx = ToolContext(
        workspace=tmp_path, config=EngineConfig(), verify_command="cat http.py"
    )
    out = await run_verify_tool(
        ctx, mutate_path="http.py", mutate_old="MAX = 5", mutate_new="MAX = 999"
    )
    assert "MAX = 999" in out
    assert "spot-check" in out
    # The real file is untouched.
    assert (tmp_path / "http.py").read_text() == "MAX = 5\n"


@pytest.mark.asyncio
async def test_run_verify_rejects_a_path_outside_the_worktree(tmp_path):
    from tools.base import ToolContext
    from tools.verify import run_verify as run_verify_tool

    outside = tmp_path.parent / "outside.py"
    outside.write_text("SECRET = 1\n")
    ctx = ToolContext(
        workspace=tmp_path, config=EngineConfig(), verify_command="true"
    )
    out = await run_verify_tool(
        ctx, mutate_path="../outside.py", mutate_old="SECRET = 1", mutate_new="X"
    )
    assert out.startswith("error:")
    assert outside.read_text() == "SECRET = 1\n"

    out = await run_verify_tool(
        ctx, mutate_path=str(outside), mutate_old="SECRET = 1", mutate_new="X"
    )
    assert out.startswith("error:")
    assert outside.read_text() == "SECRET = 1\n"


@pytest.mark.asyncio
async def test_run_verify_refuses_without_a_harness_command(tmp_path):
    from tools.base import ToolContext
    from tools.verify import run_verify as run_verify_tool

    ctx = ToolContext(workspace=tmp_path, config=EngineConfig())
    out = await run_verify_tool(ctx)
    assert out.startswith("error: no verify command")


@pytest.mark.asyncio
async def test_run_verify_rejects_an_ambiguous_mutation(tmp_path):
    from tools.base import ToolContext
    from tools.verify import run_verify as run_verify_tool

    (tmp_path / "http.py").write_text("x = 1\nx = 1\n")
    ctx = ToolContext(
        workspace=tmp_path, config=EngineConfig(), verify_command="true"
    )
    out = await run_verify_tool(
        ctx, mutate_path="http.py", mutate_old="x = 1", mutate_new="x = 2"
    )
    assert "appears 2 times" in out


def test_disposable_worktree_is_removed_and_skips_heavy_dirs(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("a\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "big").write_text("x" * 100)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref\n")

    holder = DisposableWorktree(tmp_path)
    with holder as copy:
        assert (copy / "src" / "a.py").read_text() == "a\n"
        assert not (copy / "node_modules").exists()
        assert not (copy / ".git").exists()
        recorded = copy
    assert not recorded.exists()


def test_reviewer_has_run_verify_but_no_shell():
    profile = discover_profiles().get("reviewer")
    assert "run_verify" in profile.tool_names
    assert "run_command" not in profile.tool_names
    assert profile.write_globs == []
    edit_tools = {"str_replace", "create_file", "apply_patch", "replace_lines"}
    assert not edit_tools & set(profile.tool_names)


def test_detects_python_from_setup_cfg(tmp_path):
    (tmp_path / "setup.cfg").write_text("[tool:pytest]\ntestpaths = tests\n")
    assert detect_verify_commands(tmp_path, config=EngineConfig()).commands == [
        "pytest -q"
    ]


def test_setup_cfg_without_a_pytest_section_is_not_python(tmp_path):
    (tmp_path / "setup.cfg").write_text("[metadata]\nname = x\n")
    assert detect_verify_commands(tmp_path, config=EngineConfig()).commands == []


def test_detects_python_from_pyproject_only_when_it_mentions_pytest(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    assert detect_verify_commands(tmp_path, config=EngineConfig()).commands == []
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'x'\n[tool.pytest.ini_options]\naddopts = '-q'\n"
    )
    assert detect_verify_commands(tmp_path, config=EngineConfig()).commands == [
        "pytest -q"
    ]


def test_detects_python_from_tox_ini(tmp_path):
    (tmp_path / "tox.ini").write_text("[tox]\n")
    assert detect_verify_commands(tmp_path, config=EngineConfig()).commands == [
        "pytest -q"
    ]
