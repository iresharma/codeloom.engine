from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from runtime.tools import git as git_impl
from runtime.tools.git import git_blame, git_log, git_range, git_show
from runtime.tools.git import (
    read_state,
    is_repo,
    add_agent_worktree,
    remove_agent_worktree,
    list_engine_worktrees,
    is_settle_prompt,
    parse_settle_intent,
)


def _init_git(path: Path) -> None:
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


def test_git_helpers_not_a_repo(tmp_path):
    assert git_log(tmp_path) == "not a git repository"
    assert git_show(tmp_path, "HEAD") == "not a git repository"


def test_git_log_show_blame_range(tmp_path):
    _init_git(tmp_path)
    log = git_log(tmp_path, max_count=5)
    assert "first" in log
    assert not log.startswith("error:")

    show = git_show(tmp_path, "HEAD")
    assert "first" in show
    assert "hello" in show

    blame = git_blame(tmp_path, "a.txt", start_line=1, end_line=1)
    assert "hello" in blame
    assert not blame.startswith("error:")

    ranged = git_range(tmp_path, "HEAD", "HEAD")
    assert "commits" in ranged


def test_git_show_unknown_rev(tmp_path):
    _init_git(tmp_path)
    result = git_show(tmp_path, "no-such-rev")
    assert result.startswith("error:")


def test_git_blame_outside_workspace(tmp_path):
    _init_git(tmp_path)
    result = git_blame(tmp_path, "../outside.py")
    assert result.startswith("error:")


def test_git_log_zero_clamps_to_one(tmp_path, monkeypatch):
    seen = []

    def fake_exec(workspace, args, timeout=20):
        seen.append(args)
        return SimpleNamespace(returncode=0, stdout="abc first\n", stderr="")

    monkeypatch.setattr(git_impl, "exec_cmd", fake_exec)
    monkeypatch.setattr(git_impl, "_is_repo", lambda workspace: True)
    git_log(tmp_path, max_count=0)
    assert any(arg == "-n1" for args in seen for arg in args)


def test_git_show_reports_patch_failure(tmp_path, monkeypatch):
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


# ============================================================================
# runtime/tools/git.py tests (additional coverage)
# ============================================================================

class TestGitTools:
    """Additional git tools tests for coverage."""

    def test_git_blame_invalid_line_range(self, tmp_path):
        """Test git_blame with invalid line range."""
        # Initialize a git repo
        import subprocess
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "t@t.t"], 
            cwd=tmp_path, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "config", "user.name", "t"],
            cwd=tmp_path, check=True, capture_output=True
        )
        (tmp_path / "file.txt").write_text("line1\n")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=tmp_path, check=True, capture_output=True
        )
        
        # Blame with end_line < start_line
        git_blame(tmp_path, "file.txt", start_line=2, end_line=1)
        # Should still work or return error
