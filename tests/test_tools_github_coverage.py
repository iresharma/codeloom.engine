"""Comprehensive tests for tools/github.py (tool wrappers) to improve coverage."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from runtime.tools import github as impl
from tools.github import (
    gh_pr_list,
    gh_pr_view,
    gh_pr_comments,
    gh_pr_checks,
    gh_issue_list,
    gh_issue_view,
    gh_run_list,
    gh_run_view,
    gh_release_list,
    gh_release_view,
    github_compare,
    github_search_code,
    github_file,
    github_repo,
    github_tree,
    gh_pr_comment,
    gh_issue_create,
    gh_pr_create,
)


class TestGhPrList:
    def test_pr_list_wrapper(self, ctx, monkeypatch):
        """PR list tool wrapper."""
        monkeypatch.setattr(impl, "pr_list", lambda *a, **k: "#1 open Test")
        result = gh_pr_list(ctx, state="open", limit=10)
        assert "#1" in result


class TestGhPrView:
    def test_pr_view_wrapper(self, ctx, monkeypatch):
        """PR view tool wrapper."""
        monkeypatch.setattr(impl, "pr_view", lambda *a, **k: "#1 open Test PR")
        result = gh_pr_view(ctx, 1, include_diff=False)
        assert "#1" in result

    def test_pr_view_with_diff(self, ctx, monkeypatch):
        """PR view with diff."""
        monkeypatch.setattr(
            impl, "pr_view", lambda *a, **k: "#1 diff content"
        )
        result = gh_pr_view(ctx, 1, include_diff=True)
        assert "#1" in result


class TestGhPrComments:
    def test_pr_comments_wrapper(self, ctx, monkeypatch):
        """PR comments wrapper."""
        monkeypatch.setattr(impl, "pr_comments", lambda *a, **k: "Great work!")
        result = gh_pr_comments(ctx, 1)
        assert "Great work!" in result


class TestGhPrChecks:
    def test_pr_checks_wrapper(self, ctx, monkeypatch):
        """PR checks wrapper."""
        monkeypatch.setattr(impl, "pr_checks", lambda *a, **k: "✓ all pass")
        result = gh_pr_checks(ctx, 1)
        assert "pass" in result


class TestGhIssueList:
    def test_issue_list_wrapper(self, ctx, monkeypatch):
        """Issue list wrapper."""
        monkeypatch.setattr(impl, "issue_list", lambda *a, **k: "#1 Bug report")
        result = gh_issue_list(ctx, state="open", limit=20)
        assert "#1" in result


class TestGhIssueView:
    def test_issue_view_wrapper(self, ctx, monkeypatch):
        """Issue view wrapper."""
        monkeypatch.setattr(impl, "issue_view", lambda *a, **k: "#1 open Bug")
        result = gh_issue_view(ctx, 1)
        assert "#1" in result


class TestGhRunList:
    def test_run_list_wrapper(self, ctx, monkeypatch):
        """Run list wrapper."""
        monkeypatch.setattr(impl, "run_list", lambda *a, **k: "123 test-run")
        result = gh_run_list(ctx, limit=20)
        assert "123" in result


class TestGhRunView:
    def test_run_view_wrapper(self, ctx, monkeypatch):
        """Run view wrapper."""
        monkeypatch.setattr(impl, "run_view", lambda *a, **k: "123 test run")
        result = gh_run_view(ctx, "123")
        assert "123" in result


class TestGhReleaseList:
    def test_release_list_wrapper(self, ctx, monkeypatch):
        """Release list wrapper."""
        monkeypatch.setattr(impl, "release_list", lambda *a, **k: "v1.0.0")
        result = gh_release_list(ctx, limit=10)
        assert "v1.0.0" in result


class TestGhReleaseView:
    def test_release_view_wrapper(self, ctx, monkeypatch):
        """Release view wrapper."""
        monkeypatch.setattr(impl, "release_view", lambda *a, **k: "v1.0.0 Release")
        result = gh_release_view(ctx, tag="v1.0.0")
        assert "v1.0.0" in result

    def test_release_view_latest(self, ctx, monkeypatch):
        """Release view for latest."""
        monkeypatch.setattr(impl, "release_view", lambda *a, **k: "v1.0.0 Latest")
        result = gh_release_view(ctx, tag="")
        assert "v1.0.0" in result


class TestGithubCompare:
    def test_compare_wrapper(self, ctx, monkeypatch):
        """Compare tool wrapper."""
        monkeypatch.setattr(impl, "compare", lambda *a, **k: "ahead=2")
        result = github_compare(ctx, base="main", head="dev")
        assert "ahead" in result


class TestGithubSearchCode:
    def test_search_code_wrapper(self, ctx, monkeypatch):
        """Search code wrapper."""
        monkeypatch.setattr(impl, "search_code", lambda *a, **k: "repo/file.py")
        result = github_search_code(ctx, query="retry", limit=20)
        assert "repo" in result

    def test_search_code_this_repo(self, ctx, monkeypatch):
        """Search code this repo."""
        monkeypatch.setattr(impl, "search_code", lambda *a, **k: "results")
        result = github_search_code(ctx, query="test", this_repo=True)
        assert "results" in result


class TestGithubFile:
    def test_file_wrapper(self, ctx, monkeypatch):
        """File tool wrapper."""
        monkeypatch.setattr(impl, "github_file", lambda *a, **k: "file content")
        result = github_file(
            ctx,
            path="README.md",
            repo="acme/engine",
            ref="main",
            offset=0,
            limit=1000,
        )
        assert "file" in result

    def test_file_default_limit(self, ctx, monkeypatch):
        """File with default limit."""
        monkeypatch.setattr(impl, "github_file", lambda *a, **k: "content")
        result = github_file(ctx, path="README.md")
        assert "content" in result


class TestGithubRepo:
    def test_repo_wrapper(self, ctx, monkeypatch):
        """Repo tool wrapper."""
        monkeypatch.setattr(impl, "github_repo", lambda *a, **k: "acme/engine")
        result = github_repo(ctx, repo="acme/engine")
        assert "acme/engine" in result


class TestGithubTree:
    def test_tree_wrapper(self, ctx, monkeypatch):
        """Tree tool wrapper."""
        monkeypatch.setattr(impl, "github_tree", lambda *a, **k: "file README.md 100")
        result = github_tree(ctx, repo="acme/engine", path="src", ref="main")
        assert "README.md" in result

    def test_tree_recursive(self, ctx, monkeypatch):
        """Tree recursive."""
        monkeypatch.setattr(impl, "github_tree", lambda *a, **k: "file app.py 200")
        result = github_tree(ctx, recursive=True)
        assert "app.py" in result


class TestGhPrCommentApproval:
    def test_pr_comment_denied(self, ctx):
        """PR comment should ask for approval and deny."""
        async def no(_question, kind="text"):
            return "no"

        ctx.ask_user = no
        ctx.config = SimpleNamespace(exec_approval="auto")

        async def run():
            return await gh_pr_comment(ctx, 1, "looks good")

        result = asyncio.run(run())
        assert result.startswith("error: user denied")

    def test_pr_comment_approved(self, ctx, monkeypatch):
        """PR comment should ask for approval and approve."""
        async def yes(_question, kind="text"):
            return "yes"

        ctx.ask_user = yes
        ctx.config = SimpleNamespace(exec_approval="auto")
        monkeypatch.setattr(impl, "pr_comment", lambda *a, **k: "https://example.com/comment")

        async def run():
            return await gh_pr_comment(ctx, 1, "looks good")

        result = asyncio.run(run())
        assert "https://example.com/comment" in result or result == "https://example.com/comment"

    def test_pr_comment_with_repo(self, ctx, monkeypatch):
        """PR comment with repo argument."""
        async def yes(_question, kind="text"):
            return "yes"

        ctx.ask_user = yes
        ctx.config = SimpleNamespace(exec_approval="auto")
        monkeypatch.setattr(impl, "pr_comment", lambda *a, **k: "ok")

        async def run():
            return await gh_pr_comment(ctx, 1, "comment text", repo="acme/engine")

        result = asyncio.run(run())
        assert result == "ok" or not result.startswith("error:")


class TestGhIssueCreateApproval:
    def test_issue_create_denied(self, ctx):
        """Issue create should ask for approval and deny."""
        async def no(_question, kind="text"):
            return "no"

        ctx.ask_user = no
        ctx.config = SimpleNamespace(exec_approval="auto")

        async def run():
            return await gh_issue_create(ctx, "Test issue")

        result = asyncio.run(run())
        assert result.startswith("error: user denied")

    def test_issue_create_approved(self, ctx, monkeypatch):
        """Issue create should ask for approval and approve."""
        async def yes(_question, kind="text"):
            return "yes"

        ctx.ask_user = yes
        ctx.config = SimpleNamespace(exec_approval="auto")
        monkeypatch.setattr(impl, "issue_create", lambda *a, **k: "https://example.com/issue/1")

        async def run():
            return await gh_issue_create(ctx, "Test issue", body="Description")

        result = asyncio.run(run())
        assert "https://example.com" in result or result == "ok"

    def test_issue_create_with_repo(self, ctx, monkeypatch):
        """Issue create with repo argument."""
        async def yes(_question, kind="text"):
            return "yes"

        ctx.ask_user = yes
        ctx.config = SimpleNamespace(exec_approval="auto")
        monkeypatch.setattr(impl, "issue_create", lambda *a, **k: "ok")

        async def run():
            return await gh_issue_create(ctx, "Title", body="Body", repo="acme/engine")

        result = asyncio.run(run())
        assert result == "ok" or not result.startswith("error:")


class TestGhPrCreateApproval:
    def test_pr_create_denied(self, ctx):
        """PR create should ask for approval and deny."""
        async def no(_question, kind="text"):
            return "no"

        ctx.ask_user = no
        ctx.config = SimpleNamespace(exec_approval="auto")

        async def run():
            return await gh_pr_create(ctx, "Test PR")

        result = asyncio.run(run())
        assert result.startswith("error: user denied")

    def test_pr_create_approved(self, ctx, monkeypatch):
        """PR create should ask for approval and approve."""
        async def yes(_question, kind="text"):
            return "yes"

        ctx.ask_user = yes
        ctx.config = SimpleNamespace(exec_approval="auto")
        monkeypatch.setattr(impl, "pr_create", lambda *a, **k: "https://example.com/pr/1")

        async def run():
            return await gh_pr_create(ctx, "Test PR", body="Description")

        result = asyncio.run(run())
        assert "https://example.com" in result or result == "ok"

    def test_pr_create_with_base(self, ctx, monkeypatch):
        """PR create with base branch."""
        async def yes(_question, kind="text"):
            return "yes"

        ctx.ask_user = yes
        ctx.config = SimpleNamespace(exec_approval="auto")
        monkeypatch.setattr(impl, "pr_create", lambda *a, **k: "ok")

        async def run():
            return await gh_pr_create(
                ctx, "Title", body="Body", base="develop", repo="acme/engine"
            )

        result = asyncio.run(run())
        assert result == "ok" or not result.startswith("error:")
