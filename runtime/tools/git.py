from __future__ import annotations

import subprocess
from pathlib import Path

from protocol.snapshot import GitState


def read_state(workspace: Path) -> GitState:
    workspace = workspace.resolve()
    if not _is_repo(workspace):
        return GitState.empty()

    branch = _run(workspace, "rev-parse", "--abbrev-ref", "HEAD").strip() or None
    porcelain = _run(workspace, "status", "--porcelain")
    staged, unstaged, untracked = _parse_porcelain(porcelain)
    staged_diff = _run(workspace, "diff", "--cached")
    unstaged_diff = _run(workspace, "diff")
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
