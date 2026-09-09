from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from protocol.snapshot import GitState

SETTLE_CHOICES = ("merge", "pr", "keep", "discard")


def read_state(workspace: Path, *, diffs: bool = True) -> GitState:
    workspace = workspace.resolve()
    if not _is_repo(workspace):
        return GitState.empty()

    branch = _run(workspace, "rev-parse", "--abbrev-ref", "HEAD").strip() or None
    porcelain = _run(workspace, "status", "--porcelain")
    staged, unstaged, untracked = _parse_porcelain(porcelain)
    staged_diff = _run(workspace, "diff", "--cached") if diffs else ""
    unstaged_diff = _run(workspace, "diff") if diffs else ""
    dirty = bool(staged or unstaged or untracked)
    return GitState(
        branch=branch,
        dirty=dirty,
        staged=staged,
        unstaged=unstaged,
        untracked=untracked,
        staged_diff=staged_diff,
        unstaged_diff=unstaged_diff,
    )


def _is_repo(workspace: Path) -> bool:
    try:
        output = _run(workspace, "rev-parse", "--is-inside-work-tree")
    except (OSError, subprocess.TimeoutExpired):
        return False
    return output.strip() == "true"


def is_repo(workspace: Path) -> bool:
    return _is_repo(workspace)


def add_agent_worktree(
    workspace: Path, agent_id: str, profile: str
) -> tuple[str, str, str]:
    """Create a linked checkout. Returns (path, branch, error)."""
    workspace = workspace.resolve()
    if not _is_repo(workspace):
        return "", "", "not a git repository"
    branch = f"engine/{profile}/{agent_id}"
    dest = workspace / ".engine" / "worktrees" / agent_id
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        remove_agent_worktree(workspace, dest)
    result = subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(dest)],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "worktree add failed").strip()
        return "", "", err
    return str(dest.resolve()), branch, ""


def remove_agent_worktree(workspace: Path, dest: Path) -> str:
    workspace = workspace.resolve()
    dest = Path(dest)
    result = subprocess.run(
        ["git", "worktree", "remove", "--force", str(dest)],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    if result.returncode != 0 and dest.exists():
        return (result.stderr or "worktree remove failed").strip()
    return ""


def worktree_has_changes(workspace: Path, dest: Path) -> bool:
    dest = Path(dest)
    if not dest.is_dir():
        return False
    state = read_state(dest, diffs=False)
    if state.dirty:
        return True
    base = _run(workspace, "rev-parse", "HEAD").strip()
    head = _run(dest, "rev-parse", "HEAD").strip()
    return bool(base and head and base != head)


def normalize_settle_action(text: str) -> str:
    raw = (text or "").strip().lower()
    if not raw:
        return "keep"
    if "discard" in raw or raw in {"delete", "remove", "drop"}:
        return "discard"
    if raw in {"keep", "leave", "later", "skip", "no"}:
        return "keep"
    if raw in {"merge", "m"} or raw.startswith("merge"):
        return "merge"
    if (
        raw in {"pr", "pull-request", "pull request"}
        or "pr" in raw.split()
        or raw.startswith("create")
        or raw.startswith("open")
    ):
        return "pr"
    return "keep"


def commit_if_dirty(dest: Path, message: str) -> str:
    dest = Path(dest)
    state = read_state(dest, diffs=False)
    if not state.dirty:
        return ""
    added = _exec(dest, ["git", "add", "-A"])
    if added.returncode != 0:
        return (added.stderr or added.stdout or "git add failed").strip()
    commit = _exec(dest, ["git", "commit", "-m", message or "engine worktree"])
    if commit.returncode != 0:
        return (commit.stderr or commit.stdout or "git commit failed").strip()
    return ""


def apply_worktree(
    workspace: Path,
    dest: Path,
    branch: str,
    action: str,
    *,
    message: str,
    title: str = "",
    body: str = "",
) -> tuple[bool, str, str]:
    """Apply a finished writer worktree. Returns (ok, detail, pr_url)."""
    workspace = Path(workspace).resolve()
    dest = Path(dest)
    action = normalize_settle_action(action)
    if action == "keep":
        return True, f"left worktree on {branch}", ""
    if action == "discard":
        err = remove_agent_worktree(workspace, dest)
        _exec(workspace, ["git", "branch", "-D", branch])
        if err:
            return False, err, ""
        return True, f"discarded {branch}", ""
    err = commit_if_dirty(dest, message)
    if err:
        return False, err, ""
    if action == "merge":
        merged = _exec(workspace, ["git", "merge", "--no-edit", branch])
        if merged.returncode != 0:
            _exec(workspace, ["git", "merge", "--abort"])
            detail = (merged.stderr or merged.stdout or "merge failed").strip()
            return False, detail, ""
        remove_agent_worktree(workspace, dest)
        _exec(workspace, ["git", "branch", "-d", branch])
        return True, f"merged {branch}", ""
    if action == "pr":
        pushed = _exec(
            workspace, ["git", "push", "-u", "origin", branch], timeout=120
        )
        if pushed.returncode != 0:
            return False, (pushed.stderr or pushed.stdout or "git push failed").strip(), ""
        created = _exec(
            workspace,
            [
                "gh",
                "pr",
                "create",
                "--head",
                branch,
                "--title",
                title or message,
                "--body",
                body or message,
            ],
            timeout=120,
        )
        if created.returncode != 0:
            return False, (created.stderr or created.stdout or "gh pr create failed").strip(), ""
        url = ""
        if created.stdout:
            url = created.stdout.strip().splitlines()[-1].strip()
        remove_agent_worktree(workspace, dest)
        extra = f": {url}" if url else ""
        return True, f"opened pull request for {branch}{extra}", url
    return False, f"unknown action {action}", ""


def drop_empty_worktree(workspace: Path, dest: Path, branch: str) -> str:
    err = remove_agent_worktree(workspace, dest)
    _exec(workspace, ["git", "branch", "-D", branch])
    return err


def _exec(
    workspace: Path, args: list[str], *, timeout: float = 60
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GH_PROMPT_DISABLED"] = "1"
    return subprocess.run(
        args,
        cwd=str(workspace),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
    )


def tracked_paths(workspace: Path) -> list[str] | None:
    workspace = workspace.resolve()
    if not _is_repo(workspace):
        return None
    output = _run(workspace, "ls-files")
    if not output:
        return []
    return [line for line in output.splitlines() if line]


def _run(workspace: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout


def _parse_porcelain(porcelain: str) -> tuple[list[str], list[str], list[str]]:
    staged: list[str] = []
    unstaged: list[str] = []
    untracked: list[str] = []
    for raw in porcelain.splitlines():
        if not raw:
            continue
        code = raw[:2]
        path = raw[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if code == "??":
            untracked.append(path)
            continue
        if code[0] not in (" ", "?"):
            staged.append(path)
        if code[1] not in (" ", "?"):
            unstaged.append(path)
    return staged, unstaged, untracked
