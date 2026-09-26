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
async def test_failing_verify_blocks_the_reviewer_and_starts_no_coder(tmp_path):
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
    assert "1 failed in 1.0s" in reviewer_reports[0]
    # No coder is started automatically: the orchestrator decides. The report
    # names the still-open worktree so a continue_from spawn is one call.
    assert provider.coder_tasks == [], "a coder was started without being asked"
    assert "worktree still open as agent_id=w2" in reviewer_reports[0]
    assert "continue_from=w2" in reviewer_reports[0]


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


# --------------------------------------------------------------------------
# failures that predate the change must not block it
#
# Two of three trial runs were derailed by one: reach-auth-proxy's `go vet`
# copylocks, and codeloom.engine's test_session_trace under the harness's
# ENGINE_TRACE_CALLS. `git` is real here, and so are the verify subprocesses.

from runtime.verify import (  # noqa: E402
    compare_to_baseline,
    failure_signature,
    run_baseline,
)

PYTEST_FAILS = (
    "FAILED tests/test_a.py::test_x - AssertionError: boom\n"
    "FAILED tests/test_b.py::test_y - KeyError: 'k'\n"
    "==== 2 failed, 9 passed in 1.0s ====\n"
)
GO_VET = (
    "# example.com/app/internal/RPC/storage\n"
    "internal/RPC/storage/main.go:45:9: return copies lock value: "
    "protoimpl.MessageState contains sync.Mutex\n"
    "internal/RPC/page/main.go:12:3: return copies lock value: "
    "protoimpl.MessageState contains sync.Mutex\n"
)


def test_failure_signature_reads_pytest_ids():
    assert failure_signature(PYTEST_FAILS) == {
        "pytest:tests/test_a.py::test_x",
        "pytest:tests/test_b.py::test_y",
    }


def test_failure_signature_reads_go_test_and_packages():
    out = "--- FAIL: TestCap (0.00s)\nFAIL\nFAIL\texample.com/app/http\t0.1s\n"
    assert failure_signature(out) == {
        "go-test:TestCap",
        "go-pkg:example.com/app/http",
    }


def test_failure_signature_drops_line_and_column_from_go_diagnostics():
    shifted = GO_VET.replace(":45:9:", ":51:2:")
    assert failure_signature(GO_VET) == failure_signature(shifted)
    assert any("return copies lock value" in item for item in failure_signature(GO_VET))


def test_failure_signature_is_empty_for_unrecognised_output():
    assert failure_signature("something went wrong\n") == frozenset()


def _fail(sig, **kw):
    return VerifyResult(command="x", exit_code=1, signature=frozenset(sig), **kw)


def test_identical_failures_are_preexisting():
    change = compare_to_baseline(_fail({"a", "b"}), _fail({"a", "b"}))
    assert change.preexisting is True
    assert change.passes_gate is True
    assert change.ok is False


def test_a_failure_the_base_did_not_have_is_introduced():
    change = compare_to_baseline(_fail({"a", "new"}), _fail({"a"}))
    assert change.preexisting is False
    assert change.new_failures == ["new"]
    assert change.passes_gate is False


def test_fewer_failures_than_the_base_is_still_preexisting():
    assert compare_to_baseline(_fail({"a"}), _fail({"a", "b"})).preexisting is True


def test_a_passing_base_means_the_failure_is_the_changes():
    base = VerifyResult(command="x", exit_code=0)
    assert compare_to_baseline(_fail({"a"}), base).preexisting is False


def test_unrecognisable_output_is_never_called_preexisting():
    assert compare_to_baseline(_fail(set()), _fail({"a"})).preexisting is False
    assert compare_to_baseline(_fail({"a"}), _fail(set())).preexisting is False


def test_no_baseline_leaves_the_failure_counted_against_the_change():
    assert compare_to_baseline(_fail({"a"}), None).passes_gate is False


def test_a_clean_result_is_untouched():
    clean = VerifyResult(command="x", exit_code=0)
    assert compare_to_baseline(clean, _fail({"a"})) is clean
    assert clean.passes_gate is True


def _repo_with_failing_base(tmp_path: Path, failures: str) -> Path:
    repo = tmp_path / "repo"
    _init_git(repo)
    (repo / "failures.txt").write_text(failures)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "red base"], cwd=repo, check=True, capture_output=True
    )
    return repo


@pytest.mark.asyncio
async def test_run_baseline_runs_on_the_base_commit_and_cleans_up(tmp_path):
    repo = _repo_with_failing_base(tmp_path, PYTEST_FAILS)
    dest, branch, err = add_agent_worktree(repo, "b1", "coder")
    assert not err, err
    work = Path(dest)
    # The change touches the worktree; the base checkout must not see it.
    (work / "failures.txt").write_text("FAILED tests/test_zzz.py::test_changed\n")
    commit_if_dirty(work, "change")

    plan = VerifyPlan(["cat failures.txt; exit 1"], "config")
    baseline = await run_baseline(repo, work, plan)
    assert baseline is not None
    assert baseline.source == "baseline"
    assert baseline.signature == {
        "pytest:tests/test_a.py::test_x",
        "pytest:tests/test_b.py::test_y",
    }
    listed = subprocess.run(
        ["git", "worktree", "list"], cwd=repo, capture_output=True, text=True
    ).stdout
    assert "engine-baseline-" not in listed


@pytest.mark.asyncio
async def test_run_baseline_returns_none_outside_a_repo(tmp_path):
    assert await run_baseline(tmp_path, tmp_path, VerifyPlan(["true"], "config")) is None


async def _gate(tmp_path, base_failures: str, change_failures: str):
    repo = _repo_with_failing_base(tmp_path, base_failures)
    provider = _ReviewerSpawn()
    orch = _orch(repo, verify_command="cat failures.txt; exit 1")
    orch._llm = provider
    dest, branch, err = add_agent_worktree(repo, "g1", "coder")
    assert not err, err
    work = Path(dest)
    (work / "failures.txt").write_text(change_failures)
    commit_if_dirty(work, "change")
    orch._remember_worktree("g1", work, branch, "coder", "b1")
    await orch.run("cap redirects")
    await _wait_children(orch)
    return orch, provider, work


@pytest.mark.asyncio
async def test_a_failure_that_predates_the_change_does_not_block_the_reviewer(tmp_path):
    orch, provider, work = await _gate(tmp_path, PYTEST_FAILS, PYTEST_FAILS)
    assert provider.reviewer_tasks, "the reviewer was blocked by a pre-existing failure"
    brief = provider.reviewer_tasks[0]
    assert "already fail on the base commit" in brief
    assert "tests/test_a.py::test_x" in brief
    assert "do not widen the scope" in brief
    result = orch.latest_verify(work)
    assert result.ok is False and result.preexisting is True
    # ...and settle agrees.
    assert orch._pr_verify_refusal(work) == ""


@pytest.mark.asyncio
async def test_a_failure_the_change_introduced_still_blocks_the_reviewer(tmp_path):
    changed = PYTEST_FAILS + "FAILED tests/test_c.py::test_new - oops\n"
    orch, provider, work = await _gate(tmp_path, PYTEST_FAILS, changed)
    assert provider.reviewer_tasks == []
    result = orch.latest_verify(work)
    assert result.preexisting is False
    assert result.new_failures == ["pytest:tests/test_c.py::test_new"]
    assert "harness verify failed" in orch._pr_verify_refusal(work)


def test_the_verify_block_lists_what_the_change_introduced():
    result = _fail({"a", "new"})
    result = compare_to_baseline(result, _fail({"a"}))
    block = format_verify_block(result)
    assert "Failures this change introduced" in block
    assert "- new" in block


# --------------------------------------------------------------------------
# the engine's own env vars do not reach the project under test


@pytest.mark.asyncio
async def test_verify_does_not_inherit_engine_env_vars(tmp_path, monkeypatch):
    monkeypatch.setenv("ENGINE_TRACE_CALLS", "1")
    monkeypatch.setenv("PROJECT_FLAG", "kept")
    result = await run_verify(
        tmp_path,
        VerifyPlan(
            ['echo "trace=${ENGINE_TRACE_CALLS:-unset} flag=${PROJECT_FLAG:-unset}"'],
            "config",
        ),
    )
    assert "trace=unset" in result.output
    assert "flag=kept" in result.output


@pytest.mark.asyncio
async def test_the_reviewers_run_verify_tool_also_drops_engine_env(tmp_path, monkeypatch):
    from tools.base import ToolContext
    from tools.verify import run_verify as run_verify_tool

    monkeypatch.setenv("ENGINE_TRACE_CALLS", "1")
    ctx = ToolContext(
        workspace=tmp_path,
        config=EngineConfig(),
        verify_command='echo "trace=${ENGINE_TRACE_CALLS:-unset}"',
    )
    assert "trace=unset" in await run_verify_tool(ctx)
