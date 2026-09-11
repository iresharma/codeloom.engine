"""Test coverage for tools/github.py - targeting 90%+ coverage."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from runtime.tools import github as gh_impl
from tools import github as github_tools
from tools.github import (
    gh_issue_create,
    gh_issue_list,
    gh_issue_view,
    gh_pr_checks,
    gh_pr_comments,
    gh_pr_create,
    gh_pr_list,
    gh_pr_view,
    gh_release_list,
    gh_release_view,
    gh_run_list,
    gh_run_view,
    github_compare,
    github_file,
    github_repo,
    github_search_code,
    github_tree,
)

# Import the async tool functions
gh_pr_comment = github_tools.gh_pr_comment


class TestPrList:
    """Test gh_pr_list tool wrapper."""

    def test_pr_list_default(self, ctx, monkeypatch):
        """Test PR list with defaults."""
        monkeypatch.setattr(
            gh_impl,
            "pr_list",
            lambda *a, **k: "test"
        )
        result = gh_pr_list(ctx)
        assert result == "test"

    def test_pr_list_custom_params(self, ctx, monkeypatch):
        """Test PR list with custom parameters."""
        seen = {}
        
        def fake_pr_list(workspace, state="open", limit=20, repo=""):
            seen.update({"state": state, "limit": limit, "repo": repo})
            return "ok"
        
        monkeypatch.setattr(gh_impl, "pr_list", fake_pr_list)
        result = gh_pr_list(ctx, state="closed", limit=50, repo="owner/repo")
        assert seen["state"] == "closed"
        assert seen["limit"] == 50
        assert seen["repo"] == "owner/repo"


class TestPrView:
    """Test gh_pr_view tool wrapper."""

    def test_pr_view_without_diff(self, ctx, monkeypatch):
        """Test PR view without diff."""
        monkeypatch.setattr(
            gh_impl,
            "pr_view",
            lambda *a, **k: "PR info"
        )
        result = gh_pr_view(ctx, 1)
        assert result == "PR info"

    def test_pr_view_with_diff(self, ctx, monkeypatch):
        """Test PR view with diff."""
        seen = {}
        
        def fake_pr_view(workspace, number, include_diff=False, repo=""):
            seen["include_diff"] = include_diff
            return "PR with diff" if include_diff else "PR"
        
        monkeypatch.setattr(gh_impl, "pr_view", fake_pr_view)
        result = gh_pr_view(ctx, 1, include_diff=True)
        assert seen["include_diff"] is True
        assert "diff" in result


class TestPrComments:
    """Test gh_pr_comments tool wrapper."""

    def test_pr_comments(self, ctx, monkeypatch):
        """Test PR comments retrieval."""
        monkeypatch.setattr(
            gh_impl,
            "pr_comments",
            lambda *a, **k: "comments"
        )
        result = gh_pr_comments(ctx, 1)
        assert result == "comments"


class TestPrChecks:
    """Test gh_pr_checks tool wrapper."""

    def test_pr_checks(self, ctx, monkeypatch):
        """Test PR checks retrieval."""
        monkeypatch.setattr(
            gh_impl,
            "pr_checks",
            lambda *a, **k: "checks"
        )
        result = gh_pr_checks(ctx, 1)
        assert result == "checks"


class TestIssueList:
    """Test gh_issue_list tool wrapper."""

    def test_issue_list_default(self, ctx, monkeypatch):
        """Test issue list with defaults."""
        monkeypatch.setattr(
            gh_impl,
            "issue_list",
            lambda *a, **k: "issues"
        )
        result = gh_issue_list(ctx)
        assert result == "issues"

    def test_issue_list_custom_state(self, ctx, monkeypatch):
        """Test issue list with custom state."""
        seen = {}
        
        def fake_issue_list(workspace, state="open", limit=20, repo=""):
            seen["state"] = state
            return "issues"
        
        monkeypatch.setattr(gh_impl, "issue_list", fake_issue_list)
        result = gh_issue_list(ctx, state="closed")
        assert seen["state"] == "closed"


class TestIssueView:
    """Test gh_issue_view tool wrapper."""

    def test_issue_view(self, ctx, monkeypatch):
        """Test issue view."""
        monkeypatch.setattr(
            gh_impl,
            "issue_view",
            lambda *a, **k: "issue"
        )
        result = gh_issue_view(ctx, 1)
        assert result == "issue"


class TestRunList:
    """Test gh_run_list tool wrapper."""

    def test_run_list(self, ctx, monkeypatch):
        """Test run list."""
        monkeypatch.setattr(
            gh_impl,
            "run_list",
            lambda *a, **k: "runs"
        )
        result = gh_run_list(ctx)
        assert result == "runs"


class TestRunView:
    """Test gh_run_view tool wrapper."""

    def test_run_view(self, ctx, monkeypatch):
        """Test run view."""
        monkeypatch.setattr(
            gh_impl,
            "run_view",
            lambda *a, **k: "run info"
        )
        result = gh_run_view(ctx, "123")
        assert result == "run info"


class TestReleaseList:
    """Test gh_release_list tool wrapper."""

    def test_release_list(self, ctx, monkeypatch):
        """Test release list."""
        monkeypatch.setattr(
            gh_impl,
            "release_list",
            lambda *a, **k: "releases"
        )
        result = gh_release_list(ctx)
        assert result == "releases"


class TestReleaseView:
    """Test gh_release_view tool wrapper."""

    def test_release_view_latest(self, ctx, monkeypatch):
        """Test release view - latest."""
        monkeypatch.setattr(
            gh_impl,
            "release_view",
            lambda *a, **k: "release"
        )
        result = gh_release_view(ctx)
        assert result == "release"

    def test_release_view_specific_tag(self, ctx, monkeypatch):
        """Test release view - specific tag."""
        seen = {}
        
        def fake_release_view(workspace, tag="", repo=""):
            seen["tag"] = tag
            return "release"
        
        monkeypatch.setattr(gh_impl, "release_view", fake_release_view)
        result = gh_release_view(ctx, tag="v1.0")
        assert seen["tag"] == "v1.0"


class TestCompare:
    """Test github_compare tool wrapper."""

    def test_compare(self, ctx, monkeypatch):
        """Test compare refs."""
        monkeypatch.setattr(
            gh_impl,
            "compare",
            lambda *a, **k: "comparison"
        )
        result = github_compare(ctx, "main", "feature")
        assert result == "comparison"

    def test_compare_with_repo(self, ctx, monkeypatch):
        """Test compare with specific repo."""
        seen = {}
        
        def fake_compare(workspace, base, head, repo=""):
            seen["repo"] = repo
            return "comparison"
        
        monkeypatch.setattr(gh_impl, "compare", fake_compare)
        result = github_compare(ctx, "main", "feature", repo="owner/repo")
        assert seen["repo"] == "owner/repo"


class TestSearchCode:
    """Test github_search_code tool wrapper."""

    def test_search_code_default(self, ctx, monkeypatch):
        """Test search code with defaults."""
        monkeypatch.setattr(
            gh_impl,
            "search_code",
            lambda *a, **k: "results"
        )
        result = github_search_code(ctx, "query")
        assert result == "results"

    def test_search_code_this_repo(self, ctx, monkeypatch):
        """Test search code in this repo."""
        seen = {}
        
        def fake_search(workspace, query, limit=20, this_repo=False):
            seen["this_repo"] = this_repo
            return "results"
        
        monkeypatch.setattr(gh_impl, "search_code", fake_search)
        result = github_search_code(ctx, "query", this_repo=True)
        assert seen["this_repo"] is True


class TestGithubFile:
    """Test github_file tool wrapper."""

    def test_github_file_required_path(self, ctx, monkeypatch):
        """Test file with required path parameter."""
        monkeypatch.setattr(
            gh_impl,
            "github_file",
            lambda *a, **k: "content"
        )
        result = github_file(ctx, "README.md")
        assert result == "content"

    def test_github_file_all_params(self, ctx, monkeypatch):
        """Test file with all parameters."""
        seen = {}
        
        def fake_github_file(workspace, repo, path, ref="", offset=0, limit=12000):
            seen.update({
                "repo": repo,
                "path": path,
                "ref": ref,
                "offset": offset,
                "limit": limit
            })
            return "content"
        
        monkeypatch.setattr(gh_impl, "github_file", fake_github_file)
        result = github_file(
            ctx,
            "src/app.py",
            repo="owner/repo",
            ref="main",
            offset=100,
            limit=5000
        )
        assert seen["path"] == "src/app.py"
        assert seen["repo"] == "owner/repo"
        assert seen["ref"] == "main"
        assert seen["offset"] == 100
        assert seen["limit"] == 5000


class TestGithubRepo:
    """Test github_repo tool wrapper."""

    def test_github_repo_default(self, ctx, monkeypatch):
        """Test repo info with default."""
        monkeypatch.setattr(
            gh_impl,
            "github_repo",
            lambda *a, **k: "repo info"
        )
        result = github_repo(ctx)
        assert result == "repo info"

    def test_github_repo_specific(self, ctx, monkeypatch):
        """Test repo info for specific repo."""
        seen = {}
        
        def fake_github_repo(workspace, repo=""):
            seen["repo"] = repo
            return "repo info"
        
        monkeypatch.setattr(gh_impl, "github_repo", fake_github_repo)
        result = github_repo(ctx, repo="owner/repo")
        assert seen["repo"] == "owner/repo"


class TestGithubTree:
    """Test github_tree tool wrapper."""

    def test_github_tree_default(self, ctx, monkeypatch):
        """Test tree listing with defaults."""
        monkeypatch.setattr(
            gh_impl,
            "github_tree",
            lambda *a, **k: "tree"
        )
        result = github_tree(ctx)
        assert result == "tree"

    def test_github_tree_recursive(self, ctx, monkeypatch):
        """Test tree recursive listing."""
        seen = {}
        
        def fake_github_tree(workspace, repo="", path="", ref="", recursive=False):
            seen["recursive"] = recursive
            return "tree"
        
        monkeypatch.setattr(gh_impl, "github_tree", fake_github_tree)
        result = github_tree(ctx, repo="owner/repo", path="src", ref="main", recursive=True)
        assert seen["recursive"] is True

    def test_github_tree_all_params(self, ctx, monkeypatch):
        """Test tree with all parameters."""
        seen = {}
        
        def fake_github_tree(workspace, repo="", path="", ref="", recursive=False):
            seen.update({
                "repo": repo,
                "path": path,
                "ref": ref,
                "recursive": recursive
            })
            return "tree"
        
        monkeypatch.setattr(gh_impl, "github_tree", fake_github_tree)
        result = github_tree(
            ctx,
            repo="owner/repo",
            path="src",
            ref="develop",
            recursive=True
        )
        assert seen["repo"] == "owner/repo"
        assert seen["path"] == "src"
        assert seen["ref"] == "develop"
        assert seen["recursive"] is True


class TestGhPrComment:
    """Test gh_pr_comment tool wrapper."""

    def test_pr_comment_approved(self, ctx, monkeypatch):
        """Test PR comment when approved."""
        async def yes(_question, kind="text"):
            return "yes"
        
        ctx.ask_user = yes
        ctx.config = SimpleNamespace(exec_approval="auto")
        monkeypatch.setattr(gh_impl, "pr_comment", lambda *a, **k: "ok")
        
        async def run():
            return await gh_pr_comment(ctx, 1, "looks good")
        
        result = asyncio.run(run())
        assert result == "ok"

    def test_pr_comment_denied(self, ctx):
        """Test PR comment when denied."""
        async def no(_question, kind="text"):
            return "no"
        
        ctx.ask_user = no
        ctx.config = SimpleNamespace(exec_approval="auto")
        
        async def run():
            return await gh_pr_comment(ctx, 1, "looks good")
        
        result = asyncio.run(run())
        assert result.startswith("error: user denied")


class TestGhIssueCreate:
    """Test gh_issue_create tool wrapper."""

    def test_issue_create_approved(self, ctx, monkeypatch):
        """Test issue create when approved."""
        async def yes(_question, kind="text"):
            return "yes"
        
        ctx.ask_user = yes
        ctx.config = SimpleNamespace(exec_approval="auto")
        monkeypatch.setattr(gh_impl, "issue_create", lambda *a, **k: "ok")
        
        async def run():
            return await gh_issue_create(ctx, "Fix bug", body="Description")
        
        result = asyncio.run(run())
        assert result == "ok"

    def test_issue_create_denied(self, ctx):
        """Test issue create when denied."""
        async def no(_question, kind="text"):
            return "no"
        
        ctx.ask_user = no
        ctx.config = SimpleNamespace(exec_approval="auto")
        
        async def run():
            return await gh_issue_create(ctx, "Fix bug")
        
        result = asyncio.run(run())
        assert result.startswith("error: user denied")

    def test_issue_create_with_body(self, ctx, monkeypatch):
        """Test issue create with body."""
        async def yes(_question, kind="text"):
            return "yes"
        
        ctx.ask_user = yes
        ctx.config = SimpleNamespace(exec_approval="auto")
        
        seen = {}
        
        def fake_issue_create(workspace, title, body="", repo=""):
            seen.update({"title": title, "body": body})
            return "ok"
        
        monkeypatch.setattr(gh_impl, "issue_create", fake_issue_create)
        
        async def run():
            return await gh_issue_create(ctx, "Title", body="Description")
        
        result = asyncio.run(run())
        assert seen["body"] == "Description"


class TestGhPrCreate:
    """Test gh_pr_create tool wrapper."""

    def test_pr_create_approved(self, ctx, monkeypatch):
        """Test PR create when approved."""
        async def yes(_question, kind="text"):
            return "yes"
        
        ctx.ask_user = yes
        ctx.config = SimpleNamespace(exec_approval="auto")
        monkeypatch.setattr(gh_impl, "pr_create", lambda *a, **k: "ok")
        
        async def run():
            return await gh_pr_create(ctx, "Add feature")
        
        result = asyncio.run(run())
        assert result == "ok"

    def test_pr_create_denied(self, ctx):
        """Test PR create when denied."""
        async def no(_question, kind="text"):
            return "no"
        
        ctx.ask_user = no
        ctx.config = SimpleNamespace(exec_approval="auto")
        
        async def run():
            return await gh_pr_create(ctx, "Add feature")
        
        result = asyncio.run(run())
        assert result.startswith("error: user denied")

    def test_pr_create_all_params(self, ctx, monkeypatch):
        """Test PR create with all parameters."""
        async def yes(_question, kind="text"):
            return "yes"
        
        ctx.ask_user = yes
        ctx.config = SimpleNamespace(exec_approval="auto")
        
        seen = {}
        
        def fake_pr_create(workspace, title, body="", base="", repo=""):
            seen.update({"title": title, "body": body, "base": base, "repo": repo})
            return "ok"
        
        monkeypatch.setattr(gh_impl, "pr_create", fake_pr_create)
        
        async def run():
            return await gh_pr_create(
                ctx,
                "Add feature",
                body="Adds feature X",
                base="develop",
                repo="owner/repo"
            )
        
        result = asyncio.run(run())
        assert seen["title"] == "Add feature"
        assert seen["body"] == "Adds feature X"
        assert seen["base"] == "develop"
        assert seen["repo"] == "owner/repo"
