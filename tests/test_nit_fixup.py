"""Item 6: trivial nits get fixed before the PR opens, followups reach the PR
body, and a fix-up that breaks a passing build is rolled back.

Boundaries mocked: the LLM provider and the `gh` binary. The undo journal,
git, and the verify subprocesses are all real.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from agents.orchestrator import Orchestrator, _agent_id_from_spawn
from agents.profile import discover_profiles
from agents.review_verdict import followup_nits, parse_nits, trivial_nits
from llm.provider import LLMResult, ToolCall
from runtime.config import EngineConfig
from runtime.store.edits import batches_after, ensure_schema, max_edit_id
from runtime.tools.edits import apply_edit, revert_since_sync
from runtime.tools.git import add_agent_worktree, build_pr_body, commit_if_dirty
from runtime.verify import VerifyResult
from tests.conftest import seed
from tests.fakes import FakeProvider
from tools.registry import discover_tools

NITS = """verdict: approve
=== REQUIREMENTS ===
| cap redirects at five hops | hard | met | http.py:4 |
=== END REQUIREMENTS ===
=== NITS ===
| nit | class | evidence |
|---|---|---|
| dead `or err == "error: fetch timed out"` clause | trivial | web.py:88 |
| redundant cache-delete call | trivial | cache.py:12 |
| split the retry helper out of http.py | followup | http.py |
=== END NITS ===
"""


# --------------------------------------------------------------------------
# classification


def test_trivial_and_followup_are_split():
    assert [nit.text for nit in trivial_nits(NITS)] == [
        'dead `or err == "error: fetch timed out"` clause',
        "redundant cache-delete call",
    ]
    assert [nit.text for nit in followup_nits(NITS)] == [
        "split the retry helper out of http.py"
    ]


def test_an_unclassified_nit_is_a_followup():
    text = "=== NITS ===\n| tidy the error path |\n=== END NITS ==="
    nits = parse_nits(text)
    assert len(nits) == 1
    assert not nits[0].is_trivial


def test_a_dashed_list_is_accepted():
    text = "=== NITS ===\n- dead clause -- trivial\n- big refactor -- followup\n=== END NITS ==="
    assert [nit.kind for nit in parse_nits(text)] == ["trivial", "followup"]


def test_no_nits_section_means_no_nits():
    assert parse_nits("verdict: approve\nlooks fine") == []


def test_followups_land_in_the_pr_body():
    body = build_pr_body(
        "Capped redirects in http.py.",
        followups=["split the retry helper out of http.py"],
    )
    assert "## Follow-ups" in body
    assert "- split the retry helper out of http.py" in body


def test_spawn_reply_agent_id_is_extracted():
    reply = "started agent_id=abc123 profile=coder batch_id=b1 batch_name=x"
    assert _agent_id_from_spawn(reply) == "abc123"
    assert _agent_id_from_spawn("error: spawn budget exhausted") == ""


# --------------------------------------------------------------------------
# the undo journal can walk back a whole slice


async def _edit(ctx, rel: str, old: str, new: str) -> str:
    """One real write through the funnel, journalled like any other edit."""
    return await apply_edit(
        ctx, rel, lambda src: src.text.replace(old, new), "str_replace"
    )


@pytest.mark.asyncio
async def test_revert_since_undoes_every_batch_after_the_marker(ctx):
    seed(ctx, "a.py", "x = 1\n")
    seed(ctx, "b.py", "y = 1\n")
    marker = max_edit_id(Path(ctx.journal), ctx.session_id)

    await _edit(ctx, "a.py", "x = 1", "x = 2")
    await _edit(ctx, "b.py", "y = 1", "y = 2")
    assert (ctx.workspace / "a.py").read_text() == "x = 2\n"
    assert (ctx.workspace / "b.py").read_text() == "y = 2\n"
    assert len(batches_after(Path(ctx.journal), ctx.session_id, marker)) == 2

    result = revert_since_sync(ctx, marker)
    assert result.ok, result.message
    assert (ctx.workspace / "a.py").read_text() == "x = 1\n"
    assert (ctx.workspace / "b.py").read_text() == "y = 1\n"


@pytest.mark.asyncio
async def test_revert_since_walks_back_two_edits_to_the_same_file(ctx):
    seed(ctx, "a.py", "v = 0\n")
    marker = max_edit_id(Path(ctx.journal), ctx.session_id)
    await _edit(ctx, "a.py", "v = 0", "v = 1")
    await _edit(ctx, "a.py", "v = 1", "v = 2")
    assert revert_since_sync(ctx, marker).ok
    assert (ctx.workspace / "a.py").read_text() == "v = 0\n"


@pytest.mark.asyncio
async def test_revert_since_with_nothing_after_the_marker_is_a_no_op(ctx):
    seed(ctx, "a.py", "v = 0\n")
    await _edit(ctx, "a.py", "v = 0", "v = 1")
    marker = max_edit_id(Path(ctx.journal), ctx.session_id)
    result = revert_since_sync(ctx, marker)
    assert result.ok
    assert (ctx.workspace / "a.py").read_text() == "v = 1\n"


@pytest.mark.asyncio
async def test_revert_since_refuses_to_clobber_an_out_of_band_change(ctx):
    seed(ctx, "a.py", "v = 0\n")
    marker = max_edit_id(Path(ctx.journal), ctx.session_id)
    await _edit(ctx, "a.py", "v = 0", "v = 1")
    (ctx.workspace / "a.py").write_text("someone else wrote this\n")
    result = revert_since_sync(ctx, marker)
    assert not result.ok
    assert "changed on disk" in result.message
    assert (ctx.workspace / "a.py").read_text() == "someone else wrote this\n"


# --------------------------------------------------------------------------
# end to end


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


class _ReviewThenFixUp(FakeProvider):
    """Orchestrator spawns a reviewer; the reviewer approves with nits; the
    fix-up coder edits the file the nits name."""

    def __init__(self, review_text: str, coder_edit: tuple[str, str, str] | None):
        super().__init__()
        self._review = review_text
        self._coder_edit = coder_edit
        self.reviewer_ran = 0
        self.coder_tasks: list[str] = []
        self._spawned = False
        self._coder_turn = 0

    async def complete(self, messages, tools=None, *, on_delta=None, **kwargs):
        self.calls += 1
        names = {
            (item.get("function") or item).get("name")
            for item in (tools or [])
            if isinstance(item, dict)
        }
        if "reviewer" in names:
            if not self._spawned:
                self._spawned = True
                return LLMResult(
                    text="",
                    tool_calls=[
                        ToolCall(
                            id="c1",
                            name="reviewer",
                            arguments_json=json.dumps({"task": "Review the diff."}),
                        )
                    ],
                )
            return LLMResult(text="reviewer started")
        if "run_verify" in names:
            self.reviewer_ran += 1
            return LLMResult(text=self._review)
        # The fix-up coder.
        last_user = next(
            (m["content"] for m in reversed(messages) if m.get("role") == "user"), ""
        )
        self._coder_turn += 1
        if self._coder_turn == 1:
            self.coder_tasks.append(last_user)
            if self._coder_edit is not None:
                path, old, new = self._coder_edit
                return LLMResult(
                    text="",
                    tool_calls=[
                        ToolCall(
                            id="e1",
                            name="read_file",
                            arguments_json=json.dumps({"path": path}),
                        )
                    ],
                )
            return LLMResult(text="nothing to do")
        if self._coder_turn == 2 and self._coder_edit is not None:
            path, old, new = self._coder_edit
            return LLMResult(
                text="",
                tool_calls=[
                    ToolCall(
                        id="e2",
                        name="str_replace",
                        arguments_json=json.dumps(
                            {"path": path, "old_string": old, "new_string": new}
                        ),
                    )
                ],
            )
        return LLMResult(text="fixed the nits")


async def _wait_children(orch: Orchestrator, timeout: float = 25.0) -> None:
    import asyncio

    for _ in range(int(timeout / 0.02)):
        live = [task for task in orch._child_tasks.values() if not task.done()]
        if not live:
            # A finishing child may have spawned the fix-up slice; give the
            # loop a tick to register it before deciding we are done.
            await asyncio.sleep(0.05)
            if not any(not task.done() for task in orch._child_tasks.values()):
                return
        await asyncio.sleep(0.02)
    raise AssertionError("children did not finish")


def _build(repo: Path, provider, verify_cmd: str, agent_id: str = "n1"):
    db = repo / "session.db"
    ensure_schema(db)
    orch = Orchestrator(
        provider,
        all_tools=discover_tools(),
        profiles=discover_profiles(),
        workspace=repo,
        config=EngineConfig(verify_command=verify_cmd, nit_fixup_turns=4),
        journal=db,
        session_id="nit-session",
    )
    dest, branch, err = add_agent_worktree(repo, agent_id, "coder")
    assert not err, err
    work = Path(dest)
    (work / "web.py").write_text('def ok(err):\n    return err == "timeout"\n')
    commit_if_dirty(work, "work")
    orch._remember_worktree(agent_id, work, branch, "coder", "b1")
    return orch, work


@pytest.mark.asyncio
async def test_approve_with_trivial_nits_runs_a_capped_fix_up_slice(tmp_path):
    repo = tmp_path / "repo"
    _init_git(repo)
    provider = _ReviewThenFixUp(NITS, None)
    orch, work = _build(repo, provider, "printf '== 3 passed in 0.1s ==\\n'")

    await orch.run("cap redirects")
    await _wait_children(orch)

    assert provider.reviewer_ran == 1
    assert provider.coder_tasks, "the fix-up slice never ran"
    task = provider.coder_tasks[0]
    assert "trivial nits" in task
    assert 'dead `or err == "error: fetch timed out"` clause' in task
    assert "redundant cache-delete call" in task
    # The followup was not sent to the coder.
    assert "split the retry helper" not in task
    # ...it was parked for the PR body instead.
    assert orch._followups_for(work) == ["split the retry helper out of http.py"]


@pytest.mark.asyncio
async def test_a_fix_up_that_breaks_verify_is_rolled_back(tmp_path):
    repo = tmp_path / "repo"
    _init_git(repo)
    # The verify command passes only while web.py still says "timeout".
    verify = "grep -q timeout web.py"
    provider = _ReviewThenFixUp(
        NITS, ("web.py", 'err == "timeout"', 'err == "BROKEN"')
    )
    orch, work = _build(repo, provider, verify)
    orch._verify_by_tree[str(work.resolve())] = VerifyResult(
        command=verify, exit_code=0, source="config"
    )
    notes: list[str] = []
    orch._on_agent_result = lambda aid, profile, text: notes.append(text)

    await orch.run("cap redirects")
    await _wait_children(orch)

    # The slice really did edit the file...
    assert provider.coder_tasks, "the fix-up slice never ran"
    # ...and the rollback put it back.
    assert (work / "web.py").read_text() == 'def ok(err):\n    return err == "timeout"\n'
    rollback = [text for text in notes if "reverted via the undo journal" in text]
    assert rollback, notes
    # The pre-nit verify result is what settles.
    assert orch.latest_verify(work).ok is True


@pytest.mark.asyncio
async def test_a_fix_up_that_keeps_verify_green_is_kept(tmp_path):
    repo = tmp_path / "repo"
    _init_git(repo)
    verify = "grep -q def web.py"
    provider = _ReviewThenFixUp(
        NITS, ("web.py", 'err == "timeout"', 'err in ("timeout",)')
    )
    orch, work = _build(repo, provider, verify)
    notes: list[str] = []
    orch._on_agent_result = lambda aid, profile, text: notes.append(text)

    await orch.run("cap redirects")
    await _wait_children(orch)

    assert 'err in ("timeout",)' in (work / "web.py").read_text()
    assert any("nit fix-up verified" in text for text in notes), notes
    assert orch.latest_verify(work).ok is True


@pytest.mark.asyncio
async def test_followups_reach_the_opened_pr_body(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_git(repo)
    remote = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(remote)],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "gh.json"
    shim = bin_dir / "gh"
    shim.write_text(
        "#!/bin/sh\n"
        f'python3 -c "import json,sys;open(sys.argv[1],\'w\').write(json.dumps(sys.argv[2:]))" '
        f'"{record}" "$@"\n'
        "echo https://github.com/o/r/pull/9\n"
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("ENGINE_METRICS_INSTANCE", raising=False)

    orch, work = _build(repo, FakeProvider(), "true", agent_id="n9")
    orch._closing_summary = "Capped redirects in web.py."
    orch._verify_by_tree[str(work.resolve())] = VerifyResult(
        command="true", exit_code=0, source="config"
    )
    orch._followups_by_tree[str(work.resolve())] = [
        "split the retry helper out of http.py"
    ]

    reply = await orch.apply_named_worktree("pr", agent_id="n9")
    assert reply.startswith("ok pr "), reply
    args = json.loads(record.read_text())
    body = args[args.index("--body") + 1]
    assert "## Follow-ups" in body
    assert "- split the retry helper out of http.py" in body


@pytest.mark.asyncio
async def test_request_changes_does_not_stack_a_nit_slice(tmp_path):
    repo = tmp_path / "repo"
    _init_git(repo)
    text = NITS.replace("verdict: approve", "verdict: request changes")
    provider = _ReviewThenFixUp(text, None)
    orch, work = _build(repo, provider, "true", agent_id="n3")

    await orch.run("cap redirects")
    await _wait_children(orch)

    assert provider.coder_tasks == []
    # The followup is still parked, so nothing is lost.
    assert orch._followups_for(work) == ["split the retry helper out of http.py"]
