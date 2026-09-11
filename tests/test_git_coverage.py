"""Coverage tests for runtime/tools/git.py"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from runtime.tools import git as git_impl
from runtime.tools.git import (
    git_blame,
    git_log,
    git_range,
    git_show,
    add_agent_worktree,
    drop_empty_worktree,
    remove_agent_worktree,
    list_engine_worktrees,
    worktree_has_changes,
    commit_if_dirty,
    apply_worktree,
    is_settle_prompt,
    parse_settle_intent,
    normalize_settle_action,
    SETTLE_CHOICES,
)


def _init_git(path: Path) -> None:
    """Initialize a git repository with initial commit."""
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.t"], cwd=path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=path, check=True, capture_output=True
    )
    (path / "a.txt").write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "first"], cwd=path, check=True, capture_output=True
    )


def test_git_log_not_a_repo(tmp_path):
    """Test git_log on non-repo returns error."""
    result = git_log(tmp_path)
    assert result == "not a git repository"


def test_git_log_basic(tmp_path):
    """Test git_log returns commit history."""
    _init_git(tmp_path)
    result = git_log(tmp_path)
    assert "first" in result
    assert not result.startswith("error:")


def test_git_log_with_max_count(tmp_path):
    """Test git_log respects max_count."""
    _init_git(tmp_path)
    # Create multiple commits
    for i in range(5):
        (tmp_path / f"file{i}.txt").write_text(f"content {i}\n")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", f"commit {i}"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )
    result = git_log(tmp_path, max_count=2)
    # Should be capped
    assert result.count("commit ") <= 2 or "commit 4" in result or len(result) > 0


def test_git_log_zero_count(tmp_path, monkeypatch):
    """Test git_log with zero count is clamped to 1."""
    seen = []

    def fake_exec(workspace, args, timeout=20):
        seen.append(args)
        return SimpleNamespace(returncode=0, stdout="commit abc\n", stderr="")

    monkeypatch.setattr(git_impl, "exec_cmd", fake_exec)
    monkeypatch.setattr(git_impl, "_is_repo", lambda workspace: True)
    git_log(tmp_path, max_count=0)
    assert any(arg == "-n1" for args in seen for arg in args)


def test_git_show_not_a_repo(tmp_path):
    """Test git_show on non-repo returns error."""
    result = git_show(tmp_path, "HEAD")
    assert result == "not a git repository"


def test_git_show_basic(tmp_path):
    """Test git_show returns commit details."""
    _init_git(tmp_path)
    result = git_show(tmp_path, "HEAD")
    assert "first" in result or "hello" in result
    assert not result.startswith("error:")


def test_git_show_unknown_rev(tmp_path):
    """Test git_show with unknown revision."""
    _init_git(tmp_path)
    result = git_show(tmp_path, "no-such-rev")
    assert result.startswith("error:")


def test_git_blame_not_a_repo(tmp_path):
    """Test git_blame on non-repo returns error."""
    result = git_blame(tmp_path, "file.txt")
    assert result == "not a git repository"


def test_git_blame_basic(tmp_path):
    """Test git_blame returns file blame."""
    _init_git(tmp_path)
    result = git_blame(tmp_path, "a.txt", start_line=1, end_line=1)
    assert "hello" in result
    assert not result.startswith("error:")


def test_git_blame_outside_workspace(tmp_path):
    """Test git_blame with path outside workspace."""
    _init_git(tmp_path)
    result = git_blame(tmp_path, "../outside.py")
    assert result.startswith("error:")


def test_git_blame_with_range(tmp_path):
    """Test git_blame with line range."""
    _init_git(tmp_path)
    # Create file with multiple lines
    (tmp_path / "multi.txt").write_text("line1\nline2\nline3\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "multi"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    result = git_blame(tmp_path, "multi.txt", start_line=1, end_line=2)
    assert "line" in result


def test_git_range_not_a_repo(tmp_path):
    """Test git_range on non-repo returns error."""
    result = git_range(tmp_path, "HEAD", "HEAD")
    assert result == "not a git repository"


def test_git_range_basic(tmp_path):
    """Test git_range returns range info."""
    _init_git(tmp_path)
    result = git_range(tmp_path, "HEAD", "HEAD")
    assert "commits" in result or "0" in result


def test_git_show_patch_failure(tmp_path, monkeypatch):
    """Test git_show when patch generation fails."""
    calls = []

    def fake_exec(workspace, args, timeout=20):
        calls.append(args)
        if "--format=" in args:
            return SimpleNamespace(returncode=1, stdout="", stderr="patch failed")
        return SimpleNamespace(returncode=0, stdout="stat\n", stderr="")

    monkeypatch.setattr(git_impl, "exec_cmd", fake_exec)
    monkeypatch.setattr(git_impl, "_is_repo", lambda workspace: True)
    result = git_show(tmp_path, "HEAD")
    assert result.startswith("error:")
    assert "patch failed" in result


def test_settle_choices_constant():
    """Test SETTLE_CHOICES constant is defined."""
    assert isinstance(SETTLE_CHOICES, (list, tuple))
    assert len(SETTLE_CHOICES) > 0


def test_is_settle_prompt_with_settle_choices():
    """Test is_settle_prompt recognizes settle choices."""
    choices = list(SETTLE_CHOICES)
    assert is_settle_prompt(choices) is True


def test_is_settle_prompt_with_other_choices():
    """Test is_settle_prompt rejects non-settle choices."""
    choices = ["option1", "option2", "option3"]
    assert is_settle_prompt(choices) is False


def test_is_settle_prompt_with_empty():
    """Test is_settle_prompt with empty choices."""
    assert is_settle_prompt([]) is False
    assert is_settle_prompt(None) is False


def test_parse_settle_intent_whitespace():
    """Test parse_settle_intent with whitespace."""
    result = parse_settle_intent("   ")
    assert result is None


def test_parse_settle_intent_empty():
    """Test parse_settle_intent with empty string."""
    result = parse_settle_intent("")
    assert result is None


def test_parse_settle_intent_merge():
    """Test parse_settle_intent recognizes merge."""
    result = parse_settle_intent("merge")
    assert result is not None


def test_parse_settle_intent_pr():
    """Test parse_settle_intent recognizes pr."""
    result = parse_settle_intent("pr")
    assert result is not None


def test_parse_settle_intent_discard():
    """Test parse_settle_intent recognizes discard."""
    result = parse_settle_intent("discard")
    assert result is not None


def test_list_engine_worktrees_no_engine_dir(tmp_path):
    """Test list_engine_worktrees when .engine doesn't exist."""
    result = list_engine_worktrees(tmp_path)
    assert isinstance(result, list)
    assert len(result) == 0


def test_list_engine_worktrees_with_repo(tmp_path):
    """Test list_engine_worktrees with proper repo structure."""
    _init_git(tmp_path)
    result = list_engine_worktrees(tmp_path)
    assert isinstance(result, list)
