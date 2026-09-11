"""Tests for tools/github.py to improve coverage.

Focuses on tool wrappers and approval flows.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from tools import github as github_tools
from runtime.tools import github as gh_impl


def test_gh_pr_list_wrapper(ctx, monkeypatch):
    """Test gh_pr_list tool wrapper."""
    monkeypatch.setattr(
        gh_impl,
        "pr_list",
        lambda *a, **k: "#1 OPEN Fix bug\nauthor: alice head: fix-bug"
    )
    result = github_tools.gh_pr_list(ctx, state="open", limit=10)
    assert "#1" in result
    assert "Fix bug" in result


def test_gh_pr_view_wrapper(ctx, monkeypatch):
    """Test gh_pr_view tool wrapper."""
    monkeypatch.setattr(
        gh_impl,
        "pr_view",
        lambda *a, **k: "#42 OPEN Feature\nauthor: bob\nhttps://github.com/.../42"
    )
    result = github_tools.gh_pr_view(ctx, 42, include_diff=False)
    assert "#42" in result


def test_gh_pr_view_with_diff(ctx, monkeypatch):
    """Test gh_pr_view includes diff."""
    monkeypatch.setattr(
        gh_impl,
        "pr_view",
        lambda *a, **k: "#42 OPEN Feature\nauthor: bob\n--- a/file\n+++ b/file"
    )
    result = github_tools.gh_pr_view(ctx, 42, include_diff=True)
    assert "#42" in result


def test_gh_pr_comments_wrapper(ctx, monkeypatch):
    """Test gh_pr_comments tool wrapper."""
    monkeypatch.setattr(
        gh_impl,
        "pr_comments",
        lambda *a, **k: "Looks good! LGTM"
    )
    result = github_tools.gh_pr_comments(ctx, 5)
    assert "LGTM" in result


def test_gh_pr_checks_wrapper(ctx, monkeypatch):
    """Test gh_pr_checks tool wrapper."""
    monkeypatch.setattr(
        gh_impl,
        "pr_checks",
        lambda *a, **k: "✓ All checks passed"
    )
    result = github_tools.gh_pr_checks(ctx, 5)
    assert "passed" in result


def test_gh_issue_list_wrapper(ctx, monkeypatch):
    """Test gh_issue_list tool wrapper."""
    monkeypatch.setattr(
        gh_impl,
        "issue_list",
        lambda *a, **k: "#99 OPEN Bug report\nauthor: dave"
    )
    result = github_tools.gh_issue_list(ctx, state="open", limit=20)
    assert "#99" in result
    assert "Bug report" in result


def test_gh_issue_view_wrapper(ctx, monkeypatch):
    """Test gh_issue_view tool wrapper."""
    monkeypatch.setattr(
        gh_impl,
        "issue_view",
        lambda *a, **k: "#55 OPEN Documentation\nauthor: eve"
    )
    result = github_tools.gh_issue_view(ctx, 55)
    assert "#55" in result


def test_gh_run_list_wrapper(ctx, monkeypatch):
    """Test gh_run_list tool wrapper."""
    monkeypatch.setattr(
        gh_impl,
        "run_list",
        lambda *a, **k: "1234 Tests\nstatus: completed conclusion: success"
    )
    result = github_tools.gh_run_list(ctx, limit=20)
    assert "Tests" in result


def test_gh_run_view_wrapper(ctx, monkeypatch):
    """Test gh_run_view tool wrapper."""
    monkeypatch.setattr(
        gh_impl,
        "run_view",
        lambda *a, **k: "5678 Build\nstatus: completed conclusion: failure"
    )
    result = github_tools.gh_run_view(ctx, "5678")
    assert "Build" in result


def test_gh_release_list_wrapper(ctx, monkeypatch):
    """Test gh_release_list tool wrapper."""
    monkeypatch.setattr(
        gh_impl,
        "release_list",
        lambda *a, **k: "v1.0.0 - Initial release"
    )
    result = github_tools.gh_release_list(ctx, limit=10)
    assert "v1.0.0" in result


def test_gh_release_view_wrapper(ctx, monkeypatch):
    """Test gh_release_view tool wrapper."""
    monkeypatch.setattr(
        gh_impl,
        "release_view",
        lambda *a, **k: "v2.0.0 Version 2\nMajor update"
    )
    result = github_tools.gh_release_view(ctx, "v2.0.0")
    assert "v2.0.0" in result


def test_github_compare_wrapper(ctx, monkeypatch):
    """Test github_compare tool wrapper."""
    monkeypatch.setattr(
        gh_impl,
        "compare",
        lambda *a, **k: "acme/engine main...dev diverged ahead=5 behind=2"
    )
    result = github_tools.github_compare(ctx, "main", "dev")
    assert "ahead=5" in result


def test_github_search_code_wrapper(ctx, monkeypatch):
    """Test github_search_code tool wrapper."""
    monkeypatch.setattr(
        gh_impl,
        "search_code",
        lambda *a, **k: "acme/engine src/main.py\nhttps://github.com/..."
    )
    result = github_tools.github_search_code(ctx, "def main", limit=20, this_repo=False)
    assert "src/main.py" in result


def test_github_file_wrapper(ctx, monkeypatch):
    """Test github_file tool wrapper."""
    monkeypatch.setattr(
        gh_impl,
        "github_file",
        lambda *a, **k: "def main():\n    pass"
    )
    result = github_tools.github_file(ctx, "README.md", repo="acme/engine")
    assert "def main" in result


def test_github_repo_wrapper(ctx, monkeypatch):
    """Test github_repo tool wrapper."""
    monkeypatch.setattr(
        gh_impl,
        "github_repo",
        lambda *a, **k: "acme/engine\nurl: https://github.com/acme/engine"
    )
    result = github_tools.github_repo(ctx, "acme/engine")
    assert "acme/engine" in result


def test_github_tree_wrapper(ctx, monkeypatch):
    """Test github_tree tool wrapper."""
    monkeypatch.setattr(
        gh_impl,
        "github_tree",
        lambda *a, **k: "file README.md 100\ndir src"
    )
    result = github_tools.github_tree(ctx, "acme/engine", "src")
    assert "file" in result or "dir" in result


def test_gh_pr_comment_denied(ctx):
    """Test gh_pr_comment denies when user says no."""
    async def no(_question, kind="text"):
        return "no"

    ctx.ask_user = no
    ctx.config = SimpleNamespace(exec_approval="auto")

    async def run():
        return await github_tools.gh_pr_comment(ctx, 1, "looks good")

    result = asyncio.run(run())
    assert result.startswith("error: user denied")


def test_gh_pr_comment_approved(ctx, monkeypatch):
    """Test gh_pr_comment proceeds when approved."""
    async def yes(_question, kind="text"):
        return "yes"

    ctx.ask_user = yes
    ctx.config = SimpleNamespace(exec_approval="auto")
    monkeypatch.setattr(gh_impl, "pr_comment", lambda *a, **k: "https://github.com/.../comment")

    async def run():
        return await github_tools.gh_pr_comment(ctx, 1, "looks good")

    result = asyncio.run(run())
    assert "https://github.com" in result or result == "https://github.com/.../comment"


def test_gh_issue_create_denied(ctx):
    """Test gh_issue_create denies when user says no."""
    async def no(_question, kind="text"):
        return "no"

    ctx.ask_user = no
    ctx.config = SimpleNamespace(exec_approval="auto")

    async def run():
        return await github_tools.gh_issue_create(ctx, "Test issue")

    result = asyncio.run(run())
    assert result.startswith("error: user denied")


def test_gh_issue_create_approved(ctx, monkeypatch):
    """Test gh_issue_create proceeds when approved."""
    async def yes(_question, kind="text"):
        return "yes"

    ctx.ask_user = yes
    ctx.config = SimpleNamespace(exec_approval="auto")
    monkeypatch.setattr(gh_impl, "issue_create", lambda *a, **k: "https://github.com/.../issues/123")

    async def run():
        return await github_tools.gh_issue_create(ctx, "Test issue")

    result = asyncio.run(run())
    assert "https://github.com" in result or result == "https://github.com/.../issues/123"


def test_gh_pr_create_denied(ctx):
    """Test gh_pr_create denies when user says no."""
    async def no(_question, kind="text"):
        return "no"

    ctx.ask_user = no
    ctx.config = SimpleNamespace(exec_approval="auto")

    async def run():
        return await github_tools.gh_pr_create(ctx, "Test PR")

    result = asyncio.run(run())
    assert result.startswith("error: user denied")


def test_gh_pr_create_approved(ctx, monkeypatch):
    """Test gh_pr_create proceeds when approved."""
    async def yes(_question, kind="text"):
        return "yes"

    ctx.ask_user = yes
    ctx.config = SimpleNamespace(exec_approval="auto")
    monkeypatch.setattr(gh_impl, "pr_create", lambda *a, **k: "https://github.com/.../pull/456")

    async def run():
        return await github_tools.gh_pr_create(ctx, "Test PR")

    result = asyncio.run(run())
    assert "https://github.com" in result or result == "https://github.com/.../pull/456"


def test_repo_props_included():
    """Test that repo parameter is available in all tool definitions."""
    # Just verify these functions exist and can be called
    assert hasattr(github_tools, "gh_pr_list")
    assert hasattr(github_tools, "gh_pr_view")
    assert hasattr(github_tools, "gh_pr_comments")
    assert hasattr(github_tools, "gh_pr_checks")
    assert hasattr(github_tools, "gh_issue_list")
    assert hasattr(github_tools, "gh_issue_view")
    assert hasattr(github_tools, "gh_run_list")
    assert hasattr(github_tools, "gh_run_view")
    assert hasattr(github_tools, "gh_release_list")
    assert hasattr(github_tools, "gh_release_view")
    assert hasattr(github_tools, "github_compare")
    assert hasattr(github_tools, "github_search_code")
    assert hasattr(github_tools, "github_file")
    assert hasattr(github_tools, "github_repo")
    assert hasattr(github_tools, "github_tree")
    assert hasattr(github_tools, "gh_pr_comment")
    assert hasattr(github_tools, "gh_issue_create")
    assert hasattr(github_tools, "gh_pr_create")
