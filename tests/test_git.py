from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from runtime.tools import git as git_impl
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
