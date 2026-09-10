from __future__ import annotations

import subprocess
from pathlib import Path

from runtime.tools.git import git_blame, git_log, git_range, git_show


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
