from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from runtime.tools import github as gh
from runtime.tools.approve import require_approval
from tools.github import gh_pr_comment


def test_run_gh_missing_binary(tmp_path, monkeypatch):
    monkeypatch.setattr(gh, "_which", lambda name: None)
    result = gh.run_gh(tmp_path, ["pr", "list"])
    assert result.startswith("error: gh not installed")
    assert "gh auth login" in result


def test_run_gh_nonzero(tmp_path, monkeypatch):
    monkeypatch.setattr(gh, "_which", lambda name: "/usr/bin/gh")

    def fake_exec(workspace, args, timeout=60):
        return SimpleNamespace(returncode=1, stdout="", stderr="not logged in")

    monkeypatch.setattr(gh, "exec_cmd", fake_exec)
    result = gh.run_gh(tmp_path, ["pr", "list"])
    assert result.startswith("error:")
    assert "not logged in" in result


def test_pr_list_formats_json(tmp_path, monkeypatch):
    payload = [
        {
            "number": 3,
            "title": "Fix retry",
            "author": {"login": "ada"},
            "state": "OPEN",
            "headRefName": "fix",
            "url": "https://example.com/pr/3",
        }
    ]
    monkeypatch.setattr(gh, "run_gh", lambda *a, **k: json.dumps(payload))
    text = gh.pr_list(tmp_path)
    assert "#3" in text
    assert "Fix retry" in text
    assert "ada" in text


def test_search_code_this_repo_prefix(tmp_path, monkeypatch):
    seen = []

    def fake_run(workspace, args, timeout=60):
        seen.append(args)
        if args[:2] == ["repo", "view"]:
            return json.dumps({"nameWithOwner": "acme/engine"})
        return json.dumps([])

    monkeypatch.setattr(gh, "run_gh", fake_run)
    result = gh.search_code(tmp_path, "retry", this_repo=True)
    assert result == "(no results)"
    assert any(
        args[:3] == ["search", "code", "repo:acme/engine retry"] for args in seen
    )


def test_github_file_caps(tmp_path, monkeypatch):
    monkeypatch.setattr(gh, "run_gh", lambda *a, **k: "x" * (gh.FILE_CAP + 50))
    text = gh.github_file(tmp_path, "acme/engine", "README.md")
    assert text.endswith("...[truncated]")
    assert len(text) < gh.FILE_CAP + 30


def test_pr_comment_denied(ctx):
    async def no(_question, kind="text"):
        return "no"

    ctx.ask_user = no
    ctx.config = SimpleNamespace(exec_approval="auto")

    async def run():
        return await gh_pr_comment(ctx, 1, "looks good")

    assert asyncio.run(run()).startswith("error: user denied")


def test_pr_comment_approved(ctx, monkeypatch):
    async def yes(_question, kind="text"):
        return "yes"

    ctx.ask_user = yes
    ctx.config = SimpleNamespace(exec_approval="auto")
    monkeypatch.setattr(gh, "pr_comment", lambda *a, **k: "https://example.com/comment")

    async def run():
        return await gh_pr_comment(ctx, 1, "looks good")

    assert asyncio.run(run()) == "https://example.com/comment"


def test_require_approval_never_skips():
    async def run():
        return await require_approval(
            SimpleNamespace(config=SimpleNamespace(exec_approval="never"), ask_user=None),
            "Allow?",
        )

    assert asyncio.run(run()) == ""


def test_require_approval_always_without_ask():
    async def run():
        return await require_approval(
            SimpleNamespace(
                config=SimpleNamespace(exec_approval="always"), ask_user=None
            ),
            "Allow?",
        )

    assert asyncio.run(run()) == "error: user denied"
