from __future__ import annotations

from tools.base import ToolContext, tool
from runtime.tools import github as impl
from runtime.tools.approve import require_approval


def _repo_props() -> dict:
    return {
        "repo": {
            "type": "string",
            "description": "Optional owner/name. Defaults to the workspace remote.",
        }
    }


@tool(
    description="List pull requests. Default state=open.",
    parameters={
        "type": "object",
        "properties": {
            "state": {
                "type": "string",
                "description": "open, closed, merged, or all. Default open.",
            },
            "limit": {
                "type": "integer",
                "description": "Max PRs (default 20).",
            },
            **_repo_props(),
        },
    },
)
def gh_pr_list(
    ctx: ToolContext, state: str = "open", limit: int = 20, repo: str = ""
) -> str:
    return impl.pr_list(ctx.workspace, state=state, limit=limit, repo=repo)


@tool(
    description="View one pull request. Set include_diff for a clipped patch.",
    parameters={
        "type": "object",
        "properties": {
            "number": {"type": "integer", "description": "PR number."},
            "include_diff": {
                "type": "boolean",
                "description": "If true, append a clipped diff. Default false.",
            },
            **_repo_props(),
        },
        "required": ["number"],
    },
)
def gh_pr_view(
    ctx: ToolContext, number: int, include_diff: bool = False, repo: str = ""
) -> str:
    return impl.pr_view(ctx.workspace, number, include_diff=include_diff, repo=repo)


@tool(
    description="Issue and review comments on a pull request.",
    parameters={
        "type": "object",
        "properties": {
            "number": {"type": "integer", "description": "PR number."},
            **_repo_props(),
        },
        "required": ["number"],
    },
)
def gh_pr_comments(ctx: ToolContext, number: int, repo: str = "") -> str:
    return impl.pr_comments(ctx.workspace, number, repo=repo)


@tool(
    description="CI checks for a pull request.",
    parameters={
        "type": "object",
        "properties": {
            "number": {"type": "integer", "description": "PR number."},
            **_repo_props(),
        },
        "required": ["number"],
    },
)
def gh_pr_checks(ctx: ToolContext, number: int, repo: str = "") -> str:
    return impl.pr_checks(ctx.workspace, number, repo=repo)


@tool(
    description="List issues. Default state=open.",
    parameters={
        "type": "object",
        "properties": {
            "state": {
                "type": "string",
                "description": "open, closed, or all. Default open.",
            },
            "limit": {"type": "integer", "description": "Max issues (default 20)."},
            **_repo_props(),
        },
    },
)
def gh_issue_list(
    ctx: ToolContext, state: str = "open", limit: int = 20, repo: str = ""
) -> str:
    return impl.issue_list(ctx.workspace, state=state, limit=limit, repo=repo)


@tool(
    description="View one issue (title, body, author).",
    parameters={
        "type": "object",
        "properties": {
            "number": {"type": "integer", "description": "Issue number."},
            **_repo_props(),
        },
        "required": ["number"],
    },
)
def gh_issue_view(ctx: ToolContext, number: int, repo: str = "") -> str:
    return impl.issue_view(ctx.workspace, number, repo=repo)


@tool(
    description="List recent GitHub Actions runs.",
    parameters={
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "Max runs (default 20)."},
            **_repo_props(),
        },
    },
)
def gh_run_list(ctx: ToolContext, limit: int = 20, repo: str = "") -> str:
    return impl.run_list(ctx.workspace, limit=limit, repo=repo)


@tool(
    description="View one Actions run plus a clipped failed-job log.",
    parameters={
        "type": "object",
        "properties": {
            "run_id": {"type": "string", "description": "Run database id."},
            **_repo_props(),
        },
        "required": ["run_id"],
    },
)
def gh_run_view(ctx: ToolContext, run_id: str, repo: str = "") -> str:
    return impl.run_view(ctx.workspace, run_id, repo=repo)


@tool(
    description="List GitHub releases (changelogs).",
    parameters={
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "Max releases (default 10)."},
            **_repo_props(),
        },
    },
)
def gh_release_list(ctx: ToolContext, limit: int = 10, repo: str = "") -> str:
    return impl.release_list(ctx.workspace, limit=limit, repo=repo)


@tool(
    description="View one release. Empty tag is the latest.",
    parameters={
        "type": "object",
        "properties": {
            "tag": {
                "type": "string",
                "description": "Release tag. Empty means latest.",
            },
            **_repo_props(),
        },
    },
)
def gh_release_view(ctx: ToolContext, tag: str = "", repo: str = "") -> str:
    return impl.release_view(ctx.workspace, tag, repo=repo)


@tool(
    description="Compare two refs on GitHub: ahead/behind, commits, files.",
    parameters={
        "type": "object",
        "properties": {
            "base": {"type": "string", "description": "Base ref."},
            "head": {"type": "string", "description": "Head ref."},
            **_repo_props(),
        },
        "required": ["base", "head"],
    },
)
def github_compare(ctx: ToolContext, base: str, head: str, repo: str = "") -> str:
    return impl.compare(ctx.workspace, base, head, repo=repo)


@tool(
    description=(
        "Search public GitHub code. Uses gh auth. "
        "this_repo=true prefixes repo:owner/name for the workspace."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "GitHub code search query (language:, path:, org:).",
            },
            "limit": {"type": "integer", "description": "Max hits (default 20)."},
            "this_repo": {
                "type": "boolean",
                "description": "Restrict to the workspace repo. Default false.",
            },
        },
        "required": ["query"],
    },
)
def github_search_code(
    ctx: ToolContext, query: str, limit: int = 20, this_repo: bool = False
) -> str:
    return impl.search_code(ctx.workspace, query, limit=limit, this_repo=this_repo)


@tool(
    description=(
        "Fetch a file from a GitHub repo (raw). Default window 12k chars; "
        "hard cap 50k. Use offset to page."
    ),
    parameters={
        "type": "object",
        "properties": {
            "repo": {
                "type": "string",
                "description": "owner/name. Empty uses the workspace remote.",
            },
            "path": {"type": "string", "description": "Path inside the repo."},
            "ref": {
                "type": "string",
                "description": "Branch, tag, or SHA. Empty is the default branch.",
            },
            "offset": {
                "type": "integer",
                "description": "0-based char offset (default 0).",
            },
            "limit": {
                "type": "integer",
                "description": "Max chars to return (default 12000, max 50000).",
            },
        },
        "required": ["path"],
    },
)
def github_file(
    ctx: ToolContext,
    path: str,
    repo: str = "",
    ref: str = "",
    offset: int = 0,
    limit: int = 0,
) -> str:
    return impl.github_file(
        ctx.workspace, repo, path, ref=ref, offset=offset, limit=limit
    )


@tool(
    description=(
        "Repo metadata: description, default branch, language, license, "
        "topics, stars. Empty repo uses the workspace remote."
    ),
    parameters={
        "type": "object",
        "properties": {
            **_repo_props(),
        },
    },
)
def github_repo(ctx: ToolContext, repo: str = "") -> str:
    return impl.github_repo(ctx.workspace, repo)


@tool(
    description=(
        "List files and dirs in a GitHub repo path (name, type, size). "
        "Empty path is the repo root. recursive=true walks the tree (capped). "
        "Skip caches and vendor dirs."
    ),
    parameters={
        "type": "object",
        "properties": {
            **_repo_props(),
            "path": {
                "type": "string",
                "description": "Directory inside the repo. Empty is root.",
            },
            "ref": {
                "type": "string",
                "description": "Branch, tag, or SHA. Empty is the default branch.",
            },
            "recursive": {
                "type": "boolean",
                "description": "Walk the whole tree. Default false.",
            },
        },
    },
)
def github_tree(
    ctx: ToolContext,
    repo: str = "",
    path: str = "",
    ref: str = "",
    recursive: bool = False,
) -> str:
    return impl.github_tree(
        ctx.workspace, repo, path, ref=ref, recursive=recursive
    )


@tool(
    description="Comment on a pull request. Asks the user for approval.",
    parameters={
        "type": "object",
        "properties": {
            "number": {"type": "integer", "description": "PR number."},
            "body": {"type": "string", "description": "Comment markdown."},
            **_repo_props(),
        },
        "required": ["number", "body"],
    },
)
async def gh_pr_comment(
    ctx: ToolContext, number: int, body: str, repo: str = ""
) -> str:
    denied = await require_approval(ctx, f"Allow posting a comment on PR #{number}?")
    if denied:
        return denied
    return impl.pr_comment(ctx.workspace, number, body, repo=repo)


@tool(
    description="Create a GitHub issue. Asks the user for approval.",
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Issue title."},
            "body": {"type": "string", "description": "Issue body markdown."},
            **_repo_props(),
        },
        "required": ["title"],
    },
)
async def gh_issue_create(
    ctx: ToolContext, title: str, body: str = "", repo: str = ""
) -> str:
    denied = await require_approval(ctx, f"Allow creating GitHub issue {title!r}?")
    if denied:
        return denied
    return impl.issue_create(ctx.workspace, title, body=body, repo=repo)


@tool(
    description=(
        "Create a pull request on the current branch. Asks for approval. "
        "Worktree settle still owns PRs for coder/tester writers."
    ),
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "PR title."},
            "body": {"type": "string", "description": "PR body markdown."},
            "base": {
                "type": "string",
                "description": "Base branch. Empty uses the repo default.",
            },
            **_repo_props(),
        },
        "required": ["title"],
    },
)
async def gh_pr_create(
    ctx: ToolContext, title: str, body: str = "", base: str = "", repo: str = ""
) -> str:
    denied = await require_approval(ctx, f"Allow creating pull request {title!r}?")
    if denied:
        return denied
    return impl.pr_create(ctx.workspace, title, body=body, base=base, repo=repo)
