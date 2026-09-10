from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from protocol.snapshot import GitState
from runtime.tools.fs import WorkspacePathError, resolve_in_workspace

SETTLE_CHOICES = ("merge", "pr", "keep", "discard")
LOG_MAX = 50
SHOW_CAP = 40_000
BLAME_CAP = 20_000
_ENGINE_AUTHOR = "engine"
_ENGINE_EMAIL = "engine@localhost"


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
    if not base:
        return False
    ahead = _run(dest, "rev-list", "--count", f"{base}..HEAD").strip()
    try:
        return int(ahead or "0") > 0
    except ValueError:
        return False


def is_settle_prompt(choices) -> bool:
    return set(choices or ()) == set(SETTLE_CHOICES)


def parse_settle_intent(text: str) -> str | None:
    """Return merge/pr/keep/discard when `text` clearly settles a worktree."""
    raw = " ".join((text or "").strip().lower().split())
    if not raw:
        return None
    if raw in {"merge", "m"}:
        return "merge"
    if raw in {"pr", "pull-request", "pull request"}:
        return "pr"
    if raw in {"keep", "leave", "later", "skip", "no"}:
        return "keep"
    if raw in {"discard", "delete", "remove", "drop"}:
        return "discard"
    if re.search(r"\b(don't|dont|do not)\s+merge\b", raw):
        return "keep"
    if "discard" in raw:
        return "discard"
    if "pull request" in raw or re.search(r"\bprs?\b", raw):
        return "pr"
    if raw.startswith("open") or raw.startswith("create"):
        if "pr" in raw.split() or "pull" in raw:
            return "pr"
    if re.search(r"\bmerge\b", raw):
        return "merge"
    if raw.startswith("keep") or "keep the worktree" in raw:
        return "keep"
    return None


def normalize_settle_action(text: str) -> str:
    return parse_settle_intent(text) or "keep"


def commit_if_dirty(dest: Path, message: str) -> str:
    dest = Path(dest)
    state = read_state(dest, diffs=False)
    if not state.dirty:
        return ""
    added = _exec(dest, ["git", "add", "-A"])
    if added.returncode != 0:
        return (added.stderr or added.stdout or "git add failed").strip()
    commit = _exec(
        dest,
        ["git", "commit", "-m", message or "engine worktree"],
        env=_commit_identity(dest),
    )
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
            dest, ["git", "push", "-u", "origin", "HEAD"], timeout=120
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


def list_engine_worktrees(workspace: Path) -> list[tuple[str, str, Path]]:
    """Engine worktrees still registered with git. (agent_id, branch, dest)."""
    workspace = Path(workspace).resolve()
    root = (workspace / ".engine" / "worktrees").resolve()
    if not _is_repo(workspace) or not root.is_dir():
        return []
    result = exec_cmd(
        workspace, ["git", "worktree", "list", "--porcelain"], timeout=20
    )
    if result.returncode != 0:
        return []
    found: list[tuple[str, str, Path]] = []
    path = ""
    branch = ""
    for line in (result.stdout or "").splitlines():
        if line.startswith("worktree "):
            if path:
                item = _engine_worktree_row(root, path, branch)
                if item is not None:
                    found.append(item)
            path = line[9:].strip()
            branch = ""
            continue
        if line.startswith("branch "):
            ref = line[7:].strip()
            branch = ref[11:] if ref.startswith("refs/heads/") else ref
            continue
        if line == "":
            if path:
                item = _engine_worktree_row(root, path, branch)
                if item is not None:
                    found.append(item)
            path = ""
            branch = ""
    if path:
        item = _engine_worktree_row(root, path, branch)
        if item is not None:
            found.append(item)
    return found


def _engine_worktree_row(
    root: Path, path: str, branch: str
) -> tuple[str, str, Path] | None:
    try:
        dest = Path(path).resolve()
        dest.relative_to(root)
    except (OSError, ValueError):
        return None
    if not dest.is_dir() or dest == root:
        return None
    agent_id = dest.name
    if not agent_id:
        return None
    if not branch:
        branch = _run(dest, "rev-parse", "--abbrev-ref", "HEAD").strip()
    return agent_id, branch, dest


def proc_env() -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GH_PROMPT_DISABLED"] = "1"
    return env


def exec_cmd(
    workspace: Path, args: list[str], *, timeout: float = 60, env: dict | None = None
) -> subprocess.CompletedProcess:
    merged = proc_env()
    if env:
        merged.update(env)
    return subprocess.run(
        args,
        cwd=str(workspace),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=merged,
    )


def _commit_identity(dest: Path) -> dict[str, str]:
    extra: dict[str, str] = {}
    name = _run(dest, "config", "user.name").strip()
    email = _run(dest, "config", "user.email").strip()
    if not name:
        extra["GIT_AUTHOR_NAME"] = _ENGINE_AUTHOR
        extra["GIT_COMMITTER_NAME"] = _ENGINE_AUTHOR
    if not email:
        extra["GIT_AUTHOR_EMAIL"] = _ENGINE_EMAIL
        extra["GIT_COMMITTER_EMAIL"] = _ENGINE_EMAIL
    return extra


def _exec(
    workspace: Path,
    args: list[str],
    *,
    timeout: float = 60,
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    return exec_cmd(workspace, args, timeout=timeout, env=env)


def git_log(workspace: Path, *, max_count: int = 20, path: str = "") -> str:
    workspace = Path(workspace).resolve()
    if not _is_repo(workspace):
        return "not a git repository"
    try:
        requested = int(max_count)
    except (TypeError, ValueError):
        requested = 20
    take = max(1, min(requested, LOG_MAX))
    args = ["log", "--oneline", f"-n{take}"]
    if path.strip():
        try:
            resolve_in_workspace(workspace, path)
        except WorkspacePathError as exc:
            return f"error: {exc}"
        args.extend(["--", path.strip()])
    result = exec_cmd(workspace, ["git", *args], timeout=20)
    if result.returncode != 0:
        return f"error: {(result.stderr or result.stdout or 'git log failed').strip()}"
    return result.stdout.strip() or "(no commits)"


def git_show(workspace: Path, rev: str) -> str:
    workspace = Path(workspace).resolve()
    if not _is_repo(workspace):
        return "not a git repository"
    rev = (rev or "").strip()
    if not rev:
        return "error: rev is required"
    result = exec_cmd(workspace, ["git", "show", "--stat", "--format=fuller", rev], timeout=20)
    if result.returncode != 0:
        return f"error: {(result.stderr or result.stdout or 'git show failed').strip()}"
    patch = exec_cmd(workspace, ["git", "show", "--format=", rev], timeout=20)
    if patch.returncode != 0:
        return f"error: {(patch.stderr or patch.stdout or 'git show failed').strip()}"
    text = (result.stdout or "") + ("\n" + (patch.stdout or "") if patch.stdout else "")
    return _clip(text.strip() or "(empty)", SHOW_CAP)


def git_blame(
    workspace: Path, path: str, *, start_line: int = 0, end_line: int = 0
) -> str:
    workspace = Path(workspace).resolve()
    if not _is_repo(workspace):
        return "not a git repository"
    path = (path or "").strip()
    if not path:
        return "error: path is required"
    try:
        resolve_in_workspace(workspace, path)
    except WorkspacePathError as exc:
        return f"error: {exc}"
    args = ["blame"]
    start = int(start_line or 0)
    end = int(end_line or 0)
    if start > 0:
        args.extend(["-L", f"{start},{end or start}"])
    args.extend(["--", path])
    result = exec_cmd(workspace, ["git", *args], timeout=20)
    if result.returncode != 0:
        return f"error: {(result.stderr or result.stdout or 'git blame failed').strip()}"
    return _clip((result.stdout or "").rstrip() or "(empty)", BLAME_CAP)


def git_range(workspace: Path, base: str, head: str) -> str:
    workspace = Path(workspace).resolve()
    if not _is_repo(workspace):
        return "not a git repository"
    base = (base or "").strip()
    head = (head or "").strip()
    if not base or not head:
        return "error: base and head are required"
    spec = f"{base}...{head}"
    log = exec_cmd(workspace, ["git", "log", "--oneline", spec], timeout=20)
    if log.returncode != 0:
        return f"error: {(log.stderr or log.stdout or 'git range failed').strip()}"
    stat = exec_cmd(workspace, ["git", "diff", "--stat", spec], timeout=20)
    commits = (log.stdout or "").strip() or "(no commits)"
    files = (stat.stdout or "").strip() or "(no diff)"
    return _clip(f"commits {spec}:\n{commits}\n\n{files}", SHOW_CAP)


def _clip(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    return text[:cap] + "\n...[truncated]"


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
