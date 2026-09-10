from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import patch

from llm.provider import LLMResult
from protocol.commands import AnswerPrompt, SubmitUserMessage
from protocol.events import UserPromptRequested, WorktreeSettled
from tests.fakes import FakeProvider
from runtime.tools.git import (
    add_agent_worktree,
    apply_worktree,
    commit_if_dirty,
    drop_empty_worktree,
    list_engine_worktrees,
    normalize_settle_action,
    parse_settle_intent,
    worktree_has_changes,
)
from tests.test_orchestrator import (
    _HangChild,
    _bind,
    _init_git,
    _is_orch,
    _queued,
    _wait_idle,
)


def test_normalize_settle_action():
    assert normalize_settle_action("merge") == "merge"
    assert normalize_settle_action("Merge the worktree") == "merge"
    assert normalize_settle_action("please merge it") == "merge"
    assert normalize_settle_action("no my bad please merge it") == "merge"
    assert normalize_settle_action("pr") == "pr"
    assert normalize_settle_action("create a branch and PR") == "pr"
    assert normalize_settle_action("open a pull request") == "pr"
    assert normalize_settle_action("keep") == "keep"
    assert normalize_settle_action("don't merge") == "keep"
    assert normalize_settle_action("discard") == "discard"
    assert normalize_settle_action("") == "keep"
    assert parse_settle_intent("what's in the diff?") is None
    assert parse_settle_intent("please merge it") == "merge"


def test_apply_worktree_merge(tmp_path):
    _init_git(tmp_path)
    path, branch, err = add_agent_worktree(tmp_path, "abc123", "coder")
    assert not err
    dest = Path(path)
    (dest / "flag.py").write_text("x = 1\n", encoding="utf-8")
    assert worktree_has_changes(tmp_path, dest)
    ok, detail, url = apply_worktree(
        tmp_path, dest, branch, "merge", message="engine(coder): add flag"
    )
    assert ok
    assert "merged" in detail
    assert url == ""
    assert (tmp_path / "flag.py").read_text(encoding="utf-8") == "x = 1\n"
    assert not dest.exists()


def test_apply_worktree_discard(tmp_path):
    _init_git(tmp_path)
    path, branch, err = add_agent_worktree(tmp_path, "def456", "coder")
    assert not err
    dest = Path(path)
    (dest / "gone.py").write_text("nope\n", encoding="utf-8")
    ok, detail, _ = apply_worktree(
        tmp_path, dest, branch, "discard", message="drop"
    )
    assert ok
    assert "discarded" in detail
    assert not dest.exists()
    assert not (tmp_path / "gone.py").exists()


def test_drop_empty_worktree(tmp_path):
    _init_git(tmp_path)
    path, branch, err = add_agent_worktree(tmp_path, "empty1", "coder")
    assert not err
    dest = Path(path)
    assert not worktree_has_changes(tmp_path, dest)
    assert drop_empty_worktree(tmp_path, dest, branch) == ""
    assert not dest.exists()


def test_apply_worktree_pr_uses_gh(tmp_path):
    _init_git(tmp_path)
    path, branch, err = add_agent_worktree(tmp_path, "pr789", "coder")
    assert not err
    dest = Path(path)
    (dest / "flag.py").write_text("x = 1\n", encoding="utf-8")
    real_exec = __import__("runtime.tools.git", fromlist=["_exec"])._exec

    def fake_exec(workspace, args, timeout=60, env=None):
        if args[:2] == ["git", "push"] or args[:1] == ["gh"]:
            stdout = "https://example.com/pr/1\n" if args[0] == "gh" else ""
            return subprocess.CompletedProcess(args, 0, stdout, "")
        return real_exec(workspace, args, timeout=timeout)

    with patch("runtime.tools.git._exec", fake_exec):
        ok, detail, url = apply_worktree(
            tmp_path,
            dest,
            branch,
            "pr",
            message="engine(coder): add flag",
            title="add flag",
            body="adds flag.py",
        )
    assert ok
    assert url == "https://example.com/pr/1"
    assert "opened pull request" in detail
    assert not dest.exists()


async def _wait_prompt(session, timeout: float = 2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        pending = session._prompts.pending()
        if pending is not None:
            return pending
        await asyncio.sleep(0.02)
    raise AssertionError("no worktree prompt")


def test_session_prompts_then_merges(tmp_path):
    async def run():
        _init_git(tmp_path)
        hang = asyncio.Event()
        session = await _bind(tmp_path, _HangChild(hang))()
        queue = session.subscribe()
        while not queue.empty():
            queue.get_nowait()
        orch = session._loop
        text = await orch.spawn("coder", "add a flag")
        assert "worktree=" in text
        dest = next(iter(orch._worktrees.values()))
        (dest / "flag.py").write_text("x = 1\n", encoding="utf-8")
        hang.set()
        await orch.wait_children()
        await _wait_idle(session)
        pending = await _wait_prompt(session)
        assert "merge" in pending.choices
        assert "pr" in pending.choices
        await session.handle(AnswerPrompt(prompt_id=pending.prompt_id, text="merge"))
        await orch.wait_settle()
        await _wait_idle(session)
        assert (tmp_path / "flag.py").read_text(encoding="utf-8") == "x = 1\n"
        assert not dest.exists()
        events = _queued(queue)
        settled = [item for item in events if isinstance(item, WorktreeSettled)]
        assert settled
        assert settled[-1].action == "merge"
        assert settled[-1].ok is True
        prompts = [item for item in events if isinstance(item, UserPromptRequested)]
        assert prompts

    asyncio.run(run())


def test_session_skips_prompt_when_worktree_clean(tmp_path):
    async def run():
        _init_git(tmp_path)
        hang = asyncio.Event()
        session = await _bind(tmp_path, _HangChild(hang))()
        orch = session._loop
        await orch.spawn("coder", "noop")
        hang.set()
        await orch.wait_children()
        await _wait_idle(session)
        await orch.wait_settle()
        assert session._prompts.pending() is None
        root = tmp_path / ".engine" / "worktrees"
        assert not root.exists() or not any(root.iterdir())

    asyncio.run(run())


class _HangEachChild(FakeProvider):
    def __init__(self):
        super().__init__()
        self.hangs: list[asyncio.Event] = []

    async def complete(self, messages, tools=None, *, on_delta=None, **kwargs):
        if _is_orch(tools):
            return LLMResult(text="ok")
        hang = asyncio.Event()
        self.hangs.append(hang)
        await hang.wait()
        return LLMResult(text="child")


def test_reviewer_joins_writer_then_prompts(tmp_path):
    async def run():
        _init_git(tmp_path)
        provider = _HangEachChild()
        session = await _bind(tmp_path, provider)()
        orch = session._loop
        started = await orch.spawn("coder", "add a flag")
        assert "worktree=" in started
        dest = next(iter(orch._worktrees.values()))
        (dest / "flag.py").write_text("x = 1\n", encoding="utf-8")
        for _ in range(50):
            if len(provider.hangs) >= 1:
                break
            await asyncio.sleep(0.02)
        review = await orch.spawn("reviewer", "review the diff")
        assert str(dest) in review
        for _ in range(50):
            if len(provider.hangs) >= 2:
                break
            await asyncio.sleep(0.02)
        assert session._prompts.pending() is None
        provider.hangs[0].set()
        await asyncio.sleep(0.05)
        assert session._prompts.pending() is None
        provider.hangs[1].set()
        await orch.wait_children()
        await _wait_idle(session)
        pending = await _wait_prompt(session)
        assert "merge" in pending.choices
        await session.handle(AnswerPrompt(prompt_id=pending.prompt_id, text="keep"))
        await orch.wait_settle()
        assert dest.exists()
        assert (dest / "flag.py").read_text(encoding="utf-8") == "x = 1\n"
        log = subprocess.run(
            ["git", "log", "--oneline", "engine/coder/" + dest.name],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )
        assert log.returncode == 0
        assert log.stdout.strip()

    asyncio.run(run())


def test_worktree_behind_main_without_unique_commits(tmp_path):
    _init_git(tmp_path)
    path, branch, err = add_agent_worktree(tmp_path, "behind01", "coder")
    assert not err
    dest = Path(path)
    (tmp_path / "later.txt").write_text("y\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "advance"], cwd=tmp_path, check=True, capture_output=True
    )
    assert not worktree_has_changes(tmp_path, dest)
    assert drop_empty_worktree(tmp_path, dest, branch) == ""
    assert not dest.exists()


def test_keep_commits_then_settle_tool_merges(tmp_path):
    async def run():
        _init_git(tmp_path)
        hang = asyncio.Event()
        session = await _bind(tmp_path, _HangChild(hang))()
        orch = session._loop
        await orch.spawn("coder", "add a flag")
        dest = next(iter(orch._worktrees.values()))
        (dest / "flag.py").write_text("x = 1\n", encoding="utf-8")
        hang.set()
        await orch.wait_children()
        await _wait_idle(session)
        pending = await _wait_prompt(session)
        await session.handle(AnswerPrompt(prompt_id=pending.prompt_id, text="keep"))
        await orch.wait_settle()
        assert dest.exists()
        result = await orch.apply_named_worktree("merge")
        assert result.startswith("ok merge")
        assert (tmp_path / "flag.py").read_text(encoding="utf-8") == "x = 1\n"
        assert not dest.exists()

    asyncio.run(run())


def test_recover_leftover_worktree_then_merge(tmp_path):
    async def run():
        _init_git(tmp_path)
        path, branch, err = add_agent_worktree(tmp_path, "deadbeefcafebabe", "coder")
        assert not err
        dest = Path(path)
        (dest / "flag.py").write_text("x = 1\n", encoding="utf-8")
        session = await _bind(tmp_path, FakeProvider())()
        orch = session._loop
        listed = orch.describe_worktrees()
        assert "deadbeefcafebabe" in listed
        result = await orch.apply_named_worktree("merge", agent_id="deadbeef")
        assert "merged" in result
        assert (tmp_path / "flag.py").read_text(encoding="utf-8") == "x = 1\n"
        assert not dest.exists()

    asyncio.run(run())


def test_please_merge_it_answers_settle_prompt(tmp_path):
    async def run():
        _init_git(tmp_path)
        hang = asyncio.Event()
        session = await _bind(tmp_path, _HangChild(hang))()
        orch = session._loop
        await orch.spawn("coder", "add a flag")
        dest = next(iter(orch._worktrees.values()))
        (dest / "flag.py").write_text("x = 1\n", encoding="utf-8")
        hang.set()
        await orch.wait_children()
        await _wait_idle(session)
        await _wait_prompt(session)
        await session.handle(SubmitUserMessage(text="no my bad please merge it"))
        await orch.wait_settle()
        assert (tmp_path / "flag.py").read_text(encoding="utf-8") == "x = 1\n"
        assert not dest.exists()

    asyncio.run(run())


def test_unrelated_chat_leaves_settle_prompt(tmp_path):
    async def run():
        _init_git(tmp_path)
        hang = asyncio.Event()
        session = await _bind(tmp_path, _HangChild(hang))()
        orch = session._loop
        await orch.spawn("coder", "add a flag")
        dest = next(iter(orch._worktrees.values()))
        (dest / "flag.py").write_text("x = 1\n", encoding="utf-8")
        hang.set()
        await orch.wait_children()
        await _wait_idle(session)
        pending = await _wait_prompt(session)
        await session.handle(SubmitUserMessage(text="what files changed?"))
        assert session._prompts.pending() is not None
        assert session._prompts.pending().prompt_id == pending.prompt_id
        await session.handle(AnswerPrompt(prompt_id=pending.prompt_id, text="discard"))
        await orch.wait_settle()
        await _wait_idle(session)
        assert not dest.exists()
        assert not (tmp_path / "flag.py").exists()

    asyncio.run(run())


def test_list_engine_worktrees(tmp_path):
    _init_git(tmp_path)
    path, branch, err = add_agent_worktree(tmp_path, "listme123", "coder")
    assert not err
    rows = list_engine_worktrees(tmp_path)
    assert any(item[0] == "listme123" and item[1] == branch for item in rows)
    (Path(path) / "flag.py").write_text("x = 1\n", encoding="utf-8")
    assert not commit_if_dirty(Path(path), "engine(coder): list")
    assert worktree_has_changes(tmp_path, Path(path))
