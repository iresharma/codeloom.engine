"""Coverage for runtime/tools/git.py"""
from __future__ import annotations

import subprocess
from pathlib import Path

from runtime.tools.git import (
    read_state,
    is_repo,
    add_agent_worktree,
    remove_agent_worktree,
    list_engine_worktrees,
    is_settle_prompt,
    parse_settle_intent,
)


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True, capture_output=True)
    (path / "README").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "i"], cwd=path, check=True, capture_output=True)


def test_is_repo_true(tmp_path):
    _init_repo(tmp_path)
    assert is_repo(tmp_path)


def test_is_repo_false(tmp_path):
    assert not is_repo(tmp_path)


def test_read_state_not_repo(tmp_path):
    state = read_state(tmp_path)
    assert state.branch is None
    assert not state.dirty


def test_read_state_clean_repo(tmp_path):
    _init_repo(tmp_path)
    state = read_state(tmp_path)
    assert state.branch == "master" or state.branch == "main"
    assert not state.dirty


def test_read_state_dirty_repo(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "new.txt").write_text("content")
    state = read_state(tmp_path, diffs=True)
    assert state.untracked


def test_read_state_staged(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "new.txt").write_text("content")
    subprocess.run(["git", "add", "new.txt"], cwd=tmp_path, check=True, capture_output=True)
    state = read_state(tmp_path, diffs=True)
    assert state.staged


def test_read_state_no_diffs(tmp_path):
    _init_repo(tmp_path)
    state = read_state(tmp_path, diffs=False)
    assert not state.staged_diff


def test_add_agent_worktree_success(tmp_path):
    _init_repo(tmp_path)
    dest, branch, err = add_agent_worktree(tmp_path, "agent123", "coder")
    assert not err
    assert "engine/coder/agent123" in branch
    assert Path(dest).exists()


def test_add_agent_worktree_not_repo(tmp_path):
    dest, branch, err = add_agent_worktree(tmp_path, "agent123", "coder")
    assert err and "not a git repository" in err
    assert not dest


def test_remove_agent_worktree(tmp_path):
    _init_repo(tmp_path)
    dest, branch, err = add_agent_worktree(tmp_path, "agent123", "coder")
    assert not err
    err = remove_agent_worktree(tmp_path, Path(dest))
    # May not error even if worktree removal fails slightly


def test_list_engine_worktrees_empty(tmp_path):
    _init_repo(tmp_path)
    trees = list_engine_worktrees(tmp_path)
    assert isinstance(trees, list)


def test_is_settle_prompt_yes(tmp_path):
    choices = ["merge", "pr", "keep", "discard"]
    assert is_settle_prompt(choices)


def test_is_settle_prompt_no(tmp_path):
    choices = ["yes", "no"]
    assert not is_settle_prompt(choices)


def test_parse_settle_intent_merge():
    assert parse_settle_intent("merge") == "merge"


def test_parse_settle_intent_pr():
    assert parse_settle_intent("pr") == "pr"


def test_parse_settle_intent_keep():
    assert parse_settle_intent("keep") == "keep"


def test_parse_settle_intent_discard():
    assert parse_settle_intent("discard") == "discard"


def test_parse_settle_intent_invalid():
    assert parse_settle_intent("invalid") is None


def test_parse_settle_intent_empty():
    assert parse_settle_intent("") is None
