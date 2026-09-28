from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from protocol.snapshot import GitState
from runtime.tools.fs import WorkspacePathError, resolve_in_workspace
from runtime.tools.toolchain import detect_toolchain, skip_commit_path

SETTLE_CHOICES = ("merge", "pr", "keep", "discard")
LOG_MAX = 50
SHOW_CAP = 40_000
BLAME_CAP = 20_000
_ENGINE_AUTHOR = "engine"
_ENGINE_EMAIL = "engine@localhost"
# GitHub renders a PR title far past this, but a title is a headline: one
# sentence, cut on a word boundary. Never a raw character slice -- that is
# what shipped "Add HTTP redirect cap and pin pyinstalle".
PR_TITLE_MAX = 72
PR_BODY_MAX = 60_000
_SENTENCE_END = re.compile(r"(?<=[.!?])[\s]")
_BRIEFING_LABELS = frozenset(
    {
        "what",
        "paths",
        "facts",
        "verdict",
        "leftover",
        "reasoning",
        "test_plan",
        "testplan",
        "checks",
    }
)


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


def read_workspace_state(workspace: Path, *, diffs: bool = True) -> GitState:
    """Git status for the main checkout plus every engine worktree.

    Writers edit linked worktrees, so `read_state(workspace)` stays clean
    while the Changes panel would otherwise show nothing.
    """
    workspace = workspace.resolve()
    main = read_state(workspace, diffs=diffs)
    staged = list(main.staged)
    unstaged = list(main.unstaged)
    untracked = list(main.untracked)
    seen = set(staged + unstaged + untracked)
    base = _run(workspace, "rev-parse", "HEAD").strip() if _is_repo(workspace) else ""

    def _add(bucket: list[str], path: str) -> None:
        if path and path not in seen:
            seen.add(path)
            bucket.append(path)

    for _agent_id, _branch, dest in list_engine_worktrees(workspace):
        child = read_state(dest, diffs=False)
        for path in child.staged:
            _add(staged, path)
        for path in child.unstaged:
            _add(unstaged, path)
        for path in child.untracked:
            _add(untracked, path)
        if base:
            for path in _run(dest, "diff", "--name-only", base).splitlines():
                _add(unstaged, path.strip())
    return GitState(
        branch=main.branch,
        dirty=bool(staged or unstaged or untracked),
        staged=staged,
        unstaged=unstaged,
        untracked=untracked,
        staged_diff=main.staged_diff,
        unstaged_diff=main.unstaged_diff,
    )


def _is_repo(workspace: Path) -> bool:
    try:
        output = _run(workspace, "rev-parse", "--is-inside-work-tree")
    except (OSError, subprocess.TimeoutExpired):
        return False
    return output.strip() == "true"


def is_repo(workspace: Path) -> bool:
    return _is_repo(workspace)


def read_head_file(repo: Path, rel: str) -> str | None:
    """Blob at HEAD:rel, or None if the path is not in HEAD."""
    if not rel or not _is_repo(repo):
        return None
    result = exec_cmd(repo, ["git", "show", f"HEAD:{rel}"], timeout=10)
    if result.returncode != 0:
        return None
    return result.stdout


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


def _title_source(summary: str) -> str:
    """Prefer the what: body over briefing labels like paths:."""
    what = ""
    unlabeled: list[str] = []
    for line in (summary or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        key, sep, rest = stripped.partition(":")
        key_norm = key.strip().lower().replace(" ", "")
        if sep and key_norm in _BRIEFING_LABELS:
            if key_norm == "what" and rest.strip():
                what = rest.strip()
            continue
        unlabeled.append(stripped)
    if what:
        return what
    if unlabeled:
        return "\n".join(unlabeled)
    return ""


def pr_title_from_summary(summary: str, *, limit: int = PR_TITLE_MAX) -> str:
    """First sentence of `summary`, cut on a word boundary to `limit` chars.

    Never a raw character slice: a PR title that ends mid-word ("pin
    pyinstalle") is what the two-coder trial shipped. When the first word
    alone is longer than `limit` there is no word boundary to cut on, so
    that single word is returned whole rather than sliced.
    """
    source = _title_source(summary)
    first = next((ln for ln in (source or "").splitlines() if ln.strip()), "")
    heading = _TITLE_LINE.match(first)
    if heading:
        # A summary that opens with a standalone **Title** or `# Title` line
        # already has one: use it whole rather than its first sentence.
        title = " ".join(next(g for g in heading.groups() if g).split())
        return pr_title_from_summary(title, limit=limit)
    text = " ".join((source or "").replace("\n", " ").split())
    if not text:
        return ""
    # Strip a leading bullet/label the orchestrator may have opened with.
    text = re.sub(r"^(?:[-*+\u2022]\s+|\d+[.)]\s+)", "", text)
    sentence = _SENTENCE_END.split(text, 1)[0].strip()
    if not sentence:
        sentence = text
    sentence = sentence.rstrip()
    if len(sentence) <= limit:
        return sentence.rstrip(".,;:")
    words = sentence.split(" ")
    out = ""
    for word in words:
        candidate = word if not out else f"{out} {word}"
        if len(candidate) > limit:
            break
        out = candidate
    if not out:
        # A single unbreakable word: keep it whole rather than mangle it.
        out = words[0]
    return out.rstrip(".,;:")


_RULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")
# A reply that talks *to the user* -- offers a choice, invites a reply -- is a
# conversation turn, not a description of the change.
_ASKS_USER = re.compile(
    r"(would you like|do you want|want me to|shall i|should i|let me know|"
    r"which (?:would|do) you|how would you like|please (?:confirm|choose|tell))",
    re.IGNORECASE,
)
_TITLE_LINE = re.compile(r"^\s*(?:#{1,6}\s+(.+?)|\*\*(.+?)\*\*|__(.+?)__)\s*$")


def pr_summary_from_reply(reply: str) -> str:
    """The PR-ready summary inside an orchestrator reply, or "" when there is none.

    A headless run cannot answer the orchestrator, so its last reply is
    usually a conversation turn: a preamble, the summary the task asked for,
    and a closing "would you like me to merge, open a PR, or keep it?". Using
    the whole reply as a PR body put that question -- and, in one trial, a bare
    status update -- on the pull request.

    So a summary counts only when it is set apart: a block between `---`
    rules, or a `>` blockquote (the longest wins). Failing that, the whole
    reply counts only if it never addresses the user. Anything else returns
    "" and settle falls back to the task plus the diff stat.
    """
    text = (reply or "").strip()
    if not text:
        return ""
    lines = text.splitlines()

    blocks: list[str] = []
    rules = [i for i, line in enumerate(lines) if _RULE.match(line)]
    for start, end in zip(rules[0::2], rules[1::2], strict=False):
        block = "\n".join(lines[start + 1 : end]).strip()
        if block:
            blocks.append(block)
    quoted: list[str] = []
    for line in lines:
        match = _QUOTE.match(line)
        if match:
            quoted.append(match.group(1))
        elif quoted and quoted[-1] != "\x00":
            quoted.append("\x00")
    run: list[str] = []
    for item in quoted + ["\x00"]:
        if item == "\x00":
            block = "\n".join(run).strip()
            if block:
                blocks.append(block)
            run = []
        else:
            run.append(item)
    blocks = [b for b in blocks if len(b) >= 40 and not _asks_user(b)]
    if blocks:
        return max(blocks, key=len)

    if _asks_user(text):
        return ""
    return text


def parse_generated_pr(text: str, *, limit: int = PR_TITLE_MAX) -> tuple[str, str] | None:
    """(title, body) from a model's reply, or None if it is not usable.

    The model is asked for a JSON object, but replies wrap it in fences or a
    sentence often enough that this looks for the first object rather than
    demanding the reply be bare JSON. Rejected: missing fields, and a body
    that asks the user something (the failure this whole path exists to
    avoid). An over-long title is cut on a word boundary, not rejected.
    """
    raw = (text or "").strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    title = pr_title_from_summary(str(data.get("title") or ""), limit=limit)
    body = str(data.get("body") or "").strip()
    if not title or not body or _asks_user(body):
        return None
    return title, body


def _asks_user(text: str) -> bool:
    if _ASKS_USER.search(text):
        return True
    return any(line.rstrip().endswith("?") for line in text.splitlines())


def worktree_diff_stat(workspace: Path, dest: Path) -> tuple[str, list[str]]:
    """`git diff --stat` for a writer worktree against its merge base.

    Returns (stat_text, changed_paths). Both empty when this is not a repo
    or the base cannot be resolved -- callers degrade to summary-only.
    """
    workspace = Path(workspace).resolve()
    dest = Path(dest)
    if not dest.is_dir():
        return "", []
    base = _run(workspace, "rev-parse", "HEAD").strip()
    if not base:
        return "", []
    merge_base = _run(dest, "merge-base", base, "HEAD").strip() or base
    stat = exec_cmd(dest, ["git", "diff", "--stat", merge_base, "HEAD"], timeout=30)
    names = exec_cmd(dest, ["git", "diff", "--name-only", merge_base, "HEAD"], timeout=30)
    stat_text = (stat.stdout or "").strip() if stat.returncode == 0 else ""
    paths: list[str] = []
    if names.returncode == 0:
        paths = [line.strip() for line in (names.stdout or "").splitlines() if line.strip()]
    return stat_text, paths


def top_level_paths(paths: list[str]) -> list[str]:
    """First path component of each changed path, de-duplicated, ordered."""
    out: list[str] = []
    for path in paths or ():
        head = (path or "").strip().split("/", 1)[0]
        if head and head not in out:
            out.append(head)
    return out


def uncovered_paths(body: str, paths: list[str]) -> list[str]:
    """Changed top-level paths that `body` never mentions."""
    lowered = (body or "").lower()
    return [item for item in top_level_paths(paths) if item.lower() not in lowered]


def build_pr_body(
    summary: str,
    *,
    task: str = "",
    stat_text: str = "",
    changed_paths: list[str] | None = None,
    followups: list[str] | None = None,
) -> str:
    """The PR body: the orchestrator's closing summary, plus a deterministic
    coverage section when the summary misses a changed top-level path.

    The coverage section is built from the diff stat, never by asking the
    model a second time. With no summary at all the body falls back to the
    original task prompt plus the stat -- never a lone child's report.
    """
    body = (summary or "").strip()
    parts: list[str] = []
    if body:
        parts.append(body)
    else:
        fallback = (task or "").strip()
        if fallback:
            parts.append(f"Task:\n{fallback}")
        if stat_text:
            parts.append(f"## Files changed\n\n```\n{stat_text}\n```")
        elif not fallback:
            parts.append("(no summary available)")
        return _clip("\n\n".join(parts), PR_BODY_MAX)
    missing = uncovered_paths(body, changed_paths or [])
    if missing and stat_text:
        listed = ", ".join(missing)
        parts.append(
            "## Files changed\n\n"
            f"Not mentioned above: {listed}\n\n```\n{stat_text}\n```"
        )
    if followups:
        lines = "\n".join(f"- {item}" for item in followups if str(item).strip())
        if lines:
            parts.append(f"## Follow-ups\n\n{lines}")
    return _clip("\n\n".join(parts), PR_BODY_MAX)


def _paths_to_stage(dest: Path, state) -> list[str]:
    manager = detect_toolchain(dest).manager
    out: list[str] = []
    seen: set[str] = set()
    untracked = set(state.untracked)
    for path in (*state.staged, *state.unstaged, *state.untracked):
        if not path or path in seen:
            continue
        if skip_commit_path(
            dest, path, untracked=path in untracked, manager=manager
        ):
            continue
        seen.add(path)
        out.append(path)
    return out


def commit_if_dirty(dest: Path, message: str) -> str:
    dest = Path(dest)
    state = read_state(dest, diffs=False)
    if not state.dirty:
        return ""
    to_stage = _paths_to_stage(dest, state)
    if not to_stage:
        return ""
    added = _exec(dest, ["git", "add", "--", *to_stage])
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
        pr_title = title or message
        pr_body = body or message
        created = _exec(
            workspace,
            [
                "gh",
                "pr",
                "create",
                "--head",
                branch,
                "--title",
                pr_title,
                "--body",
                pr_body,
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
        if not _keep_status_path(path):
            continue
        if code == "??":
            untracked.append(path)
            continue
        if code[0] not in (" ", "?"):
            staged.append(path)
        if code[1] not in (" ", "?"):
            unstaged.append(path)
    return staged, unstaged, untracked


def _keep_status_path(path: str) -> bool:
    head = path.split("/", 1)[0].rstrip("/")
    return head not in {".engine", ".git"}
