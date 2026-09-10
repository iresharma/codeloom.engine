from __future__ import annotations

import json
import shutil
import urllib.parse
from pathlib import Path

from runtime.tools.git import exec_cmd

MISSING_GH = (
    "error: gh not installed (https://cli.github.com); authenticate with gh auth login"
)
LIST_LIMIT = 20
BODY_CAP = 20_000
FILE_CAP = 50_000
RUN_LOG_CAP = 20_000

_which = shutil.which


def run_gh(workspace: Path, args: list[str], *, timeout: float = 60) -> str:
    if not _which("gh"):
        return MISSING_GH
    result = exec_cmd(Path(workspace), ["gh", *args], timeout=timeout)
    if result.returncode != 0:
        msg = (result.stderr or result.stdout or "gh failed").strip()
        return f"error: {msg}"
    return result.stdout or ""


def current_repo(workspace: Path) -> str:
    raw = run_gh(workspace, ["repo", "view", "--json", "nameWithOwner"])
    if raw.startswith("error:"):
        return raw
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return "error: could not resolve repository"
    name = (payload or {}).get("nameWithOwner") or ""
    if not name:
        return "error: could not resolve repository"
    return name


def pr_list(workspace: Path, *, state: str = "open", limit: int = 20, repo: str = "") -> str:
    take = _limit(limit, LIST_LIMIT)
    state = (state or "open").strip() or "open"
    args = _repo(repo) + [
        "pr",
        "list",
        "--state",
        state,
        "--limit",
        str(take),
        "--json",
        "number,title,author,state,headRefName,url",
    ]
    return _fmt_items(run_gh(workspace, args), _fmt_pr)


def pr_view(
    workspace: Path, number: int, *, include_diff: bool = False, repo: str = ""
) -> str:
    args = _repo(repo) + [
        "pr",
        "view",
        str(int(number)),
        "--json",
        "number,title,body,author,state,baseRefName,headRefName,files,url",
    ]
    raw = run_gh(workspace, args)
    if raw.startswith("error:"):
        return raw
    try:
        item = json.loads(raw)
    except json.JSONDecodeError:
        return _clip(raw, BODY_CAP)
    files = item.get("files") or []
    names = []
    for entry in files[:40]:
        if isinstance(entry, dict):
            names.append(entry.get("path") or "")
        else:
            names.append(str(entry))
    lines = [
        f"#{item.get('number')} {item.get('state') or ''} {item.get('title') or ''}".strip(),
        f"author: {_login(item.get('author'))}",
        f"base: {item.get('baseRefName') or ''} <- {item.get('headRefName') or ''}",
        item.get("url") or "",
        "",
        _clip((item.get("body") or "").strip() or "(no body)", BODY_CAP),
        "",
        "files: " + (", ".join(n for n in names if n) or "(none)"),
    ]
    text = "\n".join(lines).strip()
    if include_diff:
        diff = run_gh(workspace, _repo(repo) + ["pr", "diff", str(int(number))], timeout=60)
        if diff.startswith("error:"):
            text += f"\n\n{diff}"
        else:
            text += "\n\n" + _clip(diff, BODY_CAP)
    return text


def pr_comments(workspace: Path, number: int, *, repo: str = "") -> str:
    raw = run_gh(
        workspace,
        _repo(repo) + ["pr", "view", str(int(number)), "--comments"],
        timeout=60,
    )
    if raw.startswith("error:"):
        return raw
    return _clip(raw.strip() or "(no comments)", BODY_CAP)


def pr_checks(workspace: Path, number: int, *, repo: str = "") -> str:
    raw = run_gh(workspace, _repo(repo) + ["pr", "checks", str(int(number))])
    if raw.startswith("error:"):
        return raw
    return _clip(raw.strip() or "(no checks)", BODY_CAP)


def issue_list(workspace: Path, *, state: str = "open", limit: int = 20, repo: str = "") -> str:
    take = _limit(limit, LIST_LIMIT)
    args = _repo(repo) + [
        "issue",
        "list",
        "--state",
        (state or "open").strip() or "open",
        "--limit",
        str(take),
        "--json",
        "number,title,author,state,url",
    ]
    return _fmt_items(run_gh(workspace, args), _fmt_issue)


def issue_view(workspace: Path, number: int, *, repo: str = "") -> str:
    raw = run_gh(
        workspace,
        _repo(repo)
        + [
            "issue",
            "view",
            str(int(number)),
            "--json",
            "number,title,body,author,state,url",
        ],
    )
    if raw.startswith("error:"):
        return raw
    try:
        item = json.loads(raw)
    except json.JSONDecodeError:
        return _clip(raw, BODY_CAP)
    return "\n".join(
        [
            f"#{item.get('number')} {item.get('state') or ''} {item.get('title') or ''}".strip(),
            f"author: {_login(item.get('author'))}",
            item.get("url") or "",
            "",
            _clip((item.get("body") or "").strip() or "(no body)", BODY_CAP),
        ]
    ).strip()


def run_list(workspace: Path, *, limit: int = 20, repo: str = "") -> str:
    take = _limit(limit, LIST_LIMIT)
    args = _repo(repo) + [
        "run",
        "list",
        "--limit",
        str(take),
        "--json",
        "databaseId,name,status,conclusion,headBranch,url,event",
    ]
    return _fmt_items(run_gh(workspace, args), _fmt_run)


def run_view(workspace: Path, run_id: str, *, repo: str = "") -> str:
    run_id = str(run_id).strip()
    if not run_id:
        return "error: run id is required"
    meta = run_gh(
        workspace,
        _repo(repo)
        + [
            "run",
            "view",
            run_id,
            "--json",
            "databaseId,name,status,conclusion,headBranch,url,event,displayTitle",
        ],
    )
    if meta.startswith("error:"):
        return meta
    try:
        item = json.loads(meta)
        head = _fmt_run(item)
    except json.JSONDecodeError:
        head = meta.strip()
    log = run_gh(
        workspace,
        _repo(repo) + ["run", "view", run_id, "--log-failed"],
        timeout=90,
    )
    if log.startswith("error:"):
        return f"{head}\n\n{log}"
    return f"{head}\n\n{_clip(log.strip() or '(no failed logs)', RUN_LOG_CAP)}"


def release_list(workspace: Path, *, limit: int = 10, repo: str = "") -> str:
    take = _limit(limit, 10)
    raw = run_gh(workspace, _repo(repo) + ["release", "list", "--limit", str(take)])
    if raw.startswith("error:"):
        return raw
    return _clip(raw.strip() or "(no releases)", BODY_CAP)


def release_view(workspace: Path, tag: str = "", *, repo: str = "") -> str:
    args = _repo(repo) + ["release", "view"]
    if tag.strip():
        args.append(tag.strip())
    args.extend(["--json", "name,tagName,body,url,publishedAt"])
    raw = run_gh(workspace, args)
    if raw.startswith("error:"):
        return raw
    try:
        item = json.loads(raw)
    except json.JSONDecodeError:
        return _clip(raw, BODY_CAP)
    return "\n".join(
        [
            f"{item.get('tagName') or ''} {item.get('name') or ''}".strip(),
            item.get("publishedAt") or "",
            item.get("url") or "",
            "",
            _clip((item.get("body") or "").strip() or "(no notes)", BODY_CAP),
        ]
    ).strip()


def compare(workspace: Path, base: str, head: str, *, repo: str = "") -> str:
    base = (base or "").strip()
    head = (head or "").strip()
    if not base or not head:
        return "error: base and head are required"
    name = (repo or "").strip() or current_repo(workspace)
    if name.startswith("error:"):
        return name
    spec = f"{base}...{head}"
    raw = run_gh(
        workspace,
        ["api", f"repos/{name}/compare/{urllib.parse.quote(spec, safe='./')}"],
        timeout=60,
    )
    if raw.startswith("error:"):
        return raw
    try:
        item = json.loads(raw)
    except json.JSONDecodeError:
        return _clip(raw, BODY_CAP)
    commits = item.get("commits") or []
    files = item.get("files") or []
    commit_lines = []
    for commit in commits[:20]:
        sha = (commit.get("sha") or "")[:7]
        msg = ((commit.get("commit") or {}).get("message") or "").splitlines()[0]
        commit_lines.append(f"{sha} {msg}".strip())
    file_names = [f.get("filename") or "" for f in files[:40] if isinstance(f, dict)]
    ahead = item.get("ahead_by")
    behind = item.get("behind_by")
    status = item.get("status") or ""
    return "\n".join(
        [
            f"{name} {spec} {status} ahead={ahead} behind={behind}",
            "commits:",
            "\n".join(commit_lines) or "(none)",
            "",
            "files: " + (", ".join(n for n in file_names if n) or "(none)"),
        ]
    )


def search_code(
    workspace: Path, query: str, *, limit: int = 20, this_repo: bool = False
) -> str:
    query = (query or "").strip()
    if not query:
        return "error: query is required"
    if this_repo:
        name = current_repo(workspace)
        if name.startswith("error:"):
            return name
        if f"repo:{name}" not in query:
            query = f"repo:{name} {query}"
    take = _limit(limit, LIST_LIMIT)
    raw = run_gh(
        workspace,
        [
            "search",
            "code",
            query,
            "--limit",
            str(take),
            "--json",
            "repository,path,url,textMatches",
        ],
        timeout=60,
    )
    if raw.startswith("error:"):
        return raw
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        return _clip(raw, BODY_CAP)
    if not items:
        return "(no results)"
    blocks = []
    for item in items[:take]:
        repo = item.get("repository") or {}
        repo_name = repo.get("nameWithOwner") if isinstance(repo, dict) else str(repo)
        path = item.get("path") or ""
        url = item.get("url") or ""
        lines = [f"{repo_name} {path}".strip(), url]
        for match in (item.get("textMatches") or [])[:3]:
            frag = (match.get("fragment") or "").replace("\n", " ").strip()
            if frag:
                lines.append(frag)
        blocks.append("\n".join(line for line in lines if line))
    return _clip("\n\n".join(blocks), BODY_CAP)


def github_file(workspace: Path, repo: str, path: str, *, ref: str = "") -> str:
    repo = (repo or "").strip()
    path = (path or "").strip().lstrip("/")
    if not path:
        return "error: path is required"
    if not repo:
        repo = current_repo(workspace)
        if repo.startswith("error:"):
            return repo
    url = f"repos/{repo}/contents/{path}"
    if ref.strip():
        url += "?ref=" + urllib.parse.quote(ref.strip())
    raw = run_gh(
        workspace,
        ["api", url, "-H", "Accept: application/vnd.github.raw"],
        timeout=60,
    )
    if raw.startswith("error:"):
        return raw
    return _clip(raw or "(empty)", FILE_CAP)


def pr_comment(workspace: Path, number: int, body: str, *, repo: str = "") -> str:
    body = (body or "").strip()
    if not body:
        return "error: body is required"
    raw = run_gh(
        workspace,
        _repo(repo) + ["pr", "comment", str(int(number)), "--body", body],
        timeout=60,
    )
    if raw.startswith("error:"):
        return raw
    return raw.strip() or "ok"


def issue_create(workspace: Path, title: str, *, body: str = "", repo: str = "") -> str:
    title = (title or "").strip()
    if not title:
        return "error: title is required"
    args = _repo(repo) + ["issue", "create", "--title", title]
    if body.strip():
        args.extend(["--body", body])
    raw = run_gh(workspace, args, timeout=60)
    if raw.startswith("error:"):
        return raw
    return raw.strip() or "ok"


def pr_create(
    workspace: Path, title: str, *, body: str = "", base: str = "", repo: str = ""
) -> str:
    title = (title or "").strip()
    if not title:
        return "error: title is required"
    args = _repo(repo) + ["pr", "create", "--title", title, "--body", body or title]
    if base.strip():
        args.extend(["--base", base.strip()])
    raw = run_gh(workspace, args, timeout=120)
    if raw.startswith("error:"):
        return raw
    return raw.strip() or "ok"


def _repo(repo: str) -> list[str]:
    repo = (repo or "").strip()
    if not repo:
        return []
    return ["-R", repo]


def _limit(value: int, default: int) -> int:
    try:
        return max(1, min(int(value or default), default))
    except (TypeError, ValueError):
        return default


def _clip(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    return text[:cap] + "\n...[truncated]"


def _login(value) -> str:
    if isinstance(value, dict):
        return str(value.get("login") or value.get("name") or "")
    return str(value or "")


def _fmt_items(raw: str, fmt) -> str:
    if raw.startswith("error:"):
        return raw
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        return _clip(raw, BODY_CAP)
    if not items:
        return "(none)"
    return "\n\n".join(fmt(item) for item in items)


def _fmt_pr(item: dict) -> str:
    return "\n".join(
        line
        for line in (
            f"#{item.get('number')} {item.get('state') or ''} {item.get('title') or ''}".strip(),
            f"author: {_login(item.get('author'))} head: {item.get('headRefName') or ''}",
            item.get("url") or "",
        )
        if line
    )


def _fmt_issue(item: dict) -> str:
    return "\n".join(
        line
        for line in (
            f"#{item.get('number')} {item.get('state') or ''} {item.get('title') or ''}".strip(),
            f"author: {_login(item.get('author'))}",
            item.get("url") or "",
        )
        if line
    )


def _fmt_run(item: dict) -> str:
    return "\n".join(
        line
        for line in (
            f"{item.get('databaseId')} {item.get('name') or item.get('displayTitle') or ''}".strip(),
            f"status: {item.get('status') or ''} conclusion: {item.get('conclusion') or ''}",
            f"branch: {item.get('headBranch') or ''} event: {item.get('event') or ''}",
            item.get("url") or "",
        )
        if line
    )
