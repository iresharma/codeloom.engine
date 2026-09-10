from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from runtime.tools import github as gh
from runtime.tools.approve import require_approval
from tools.github import gh_pr_comment


def _gh_entry(**extra):
    entry = {
        "type": "file",
        "name": "README.md",
        "path": "README.md",
        "sha": "a" * 40,
        "size": 12,
        "html_url": "https://github.com/acme/engine/blob/main/README.md",
    }
    entry.update(extra)
    return entry


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


def test_github_file_directory_hint(tmp_path, monkeypatch):
    listing = json.dumps([_gh_entry()])
    monkeypatch.setattr(gh, "run_gh", lambda *a, **k: listing)
    text = gh.github_file(tmp_path, "acme/engine", "docs")
    assert text.startswith("error:")
    assert "github_tree" in text


def test_github_repo_formats(tmp_path, monkeypatch):
    payload = {
        "nameWithOwner": "acme/engine",
        "description": "The engine",
        "url": "https://github.com/acme/engine",
        "homepageUrl": "https://example.com",
        "defaultBranchRef": {"name": "main"},
        "primaryLanguage": {"name": "Python"},
        "licenseInfo": {"name": "MIT"},
        "repositoryTopics": [{"name": "agents"}, {"name": "llm"}],
        "stargazerCount": 9,
        "forkCount": 2,
        "updatedAt": "2026-01-01T00:00:00Z",
        "isPrivate": False,
    }
    monkeypatch.setattr(gh, "run_gh", lambda *a, **k: json.dumps(payload))
    text = gh.github_repo(tmp_path, "acme/engine")
    assert "acme/engine" in text
    assert "The engine" in text
    assert "default_branch: main" in text
    assert "language: Python" in text
    assert "agents" in text
    assert "stars: 9" in text
    assert "homepage: https://example.com" in text
    assert "private: false" in text


def test_github_tree_lists_and_skips_vendor(tmp_path, monkeypatch):
    payload = [
        {"type": "file", "name": "README.md", "path": "README.md", "size": 40},
        {"type": "dir", "name": "src", "path": "src"},
        {"type": "dir", "name": "node_modules", "path": "node_modules"},
        {"type": "dir", "name": ".git", "path": ".git"},
    ]
    monkeypatch.setattr(gh, "run_gh", lambda *a, **k: json.dumps(payload))
    text = gh.github_tree(tmp_path, "acme/engine")
    assert "file README.md 40" in text
    assert "dir src" in text
    assert "node_modules" not in text
    assert ".git" not in text


def test_github_tree_file_path_hints(tmp_path, monkeypatch):
    payload = {
        "type": "file",
        "name": "README.md",
        "path": "README.md",
        "size": 12,
    }
    monkeypatch.setattr(gh, "run_gh", lambda *a, **k: json.dumps(payload))
    text = gh.github_tree(tmp_path, "acme/engine", "README.md")
    assert text.startswith("error:")
    assert "github_file" in text


def test_github_file_empty_path(tmp_path):
    assert gh.github_file(tmp_path, "acme/engine", "").startswith("error:")
    assert gh.github_file(tmp_path, "acme/engine", "/").startswith("error:")


def test_github_file_leaves_source_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(gh, "run_gh", lambda *a, **k: "def retry():\n    return 1\n")
    text = gh.github_file(tmp_path, "acme/engine", "src/app.py")
    assert "def retry" in text
    assert "github_tree" not in text


def test_github_file_dir_object_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(
        gh,
        "run_gh",
        lambda *a, **k: json.dumps(
            _gh_entry(
                type="dir",
                name="src",
                path="src",
                html_url="https://github.com/acme/engine/tree/main/src",
            )
        ),
    )
    text = gh.github_file(tmp_path, "acme/engine", "src")
    assert text.startswith("error:")
    assert "github_tree" in text


def test_github_file_keeps_json_config(tmp_path, monkeypatch):
    payload = json.dumps(
        [{"name": "en", "type": "string", "path": "locales/en.json"}]
    )
    monkeypatch.setattr(gh, "run_gh", lambda *a, **k: payload)
    text = gh.github_file(tmp_path, "acme/engine", "i18n.json")
    assert not text.startswith("error:")
    assert "locales/en.json" in text


def test_github_file_keeps_empty_json_array(tmp_path, monkeypatch):
    monkeypatch.setattr(gh, "run_gh", lambda *a, **k: "[]")
    text = gh.github_file(tmp_path, "acme/engine", "empty.json")
    assert text == "[]"


def test_github_file_keeps_type_dir_config(tmp_path, monkeypatch):
    payload = json.dumps({"type": "dir", "name": "output"})
    monkeypatch.setattr(gh, "run_gh", lambda *a, **k: payload)
    text = gh.github_file(tmp_path, "acme/engine", "config.json")
    assert not text.startswith("error:")
    assert '"type": "dir"' in text


def test_github_repo_omits_repo_arg_when_empty(tmp_path, monkeypatch):
    seen = []

    def fake_run(workspace, args, timeout=60):
        seen.append(args)
        return json.dumps({"nameWithOwner": "acme/engine", "description": "x"})

    monkeypatch.setattr(gh, "run_gh", fake_run)
    gh.github_repo(tmp_path, "")
    assert seen
    assert seen[0][:2] == ["repo", "view"]
    assert seen[0][2] == "--json"


def test_github_repo_passthrough_error(tmp_path, monkeypatch):
    monkeypatch.setattr(gh, "run_gh", lambda *a, **k: "error: gh not logged in")
    assert gh.github_repo(tmp_path, "acme/engine").startswith("error:")


def test_github_repo_topics_nodes(tmp_path, monkeypatch):
    payload = {
        "nameWithOwner": "acme/engine",
        "repositoryTopics": {
            "nodes": [{"topic": {"name": "agents"}}, {"name": "llm"}]
        },
    }
    monkeypatch.setattr(gh, "run_gh", lambda *a, **k: json.dumps(payload))
    text = gh.github_repo(tmp_path, "acme/engine")
    assert "agents" in text
    assert "llm" in text


def test_github_tree_uses_workspace_remote(tmp_path, monkeypatch):
    seen = []

    def fake_run(workspace, args, timeout=60):
        seen.append(args)
        if args[:2] == ["repo", "view"]:
            return json.dumps({"nameWithOwner": "acme/engine"})
        return json.dumps(
            [{"type": "file", "name": "README.md", "path": "README.md", "size": 1}]
        )

    monkeypatch.setattr(gh, "run_gh", fake_run)
    text = gh.github_tree(tmp_path, "")
    assert "file README.md 1" in text
    assert any(
        len(args) >= 2 and args[1].startswith("repos/acme/engine/contents")
        for args in seen
    )


def test_github_tree_path_and_ref(tmp_path, monkeypatch):
    seen = []

    def fake_run(workspace, args, timeout=60):
        seen.append(args)
        return json.dumps([])

    monkeypatch.setattr(gh, "run_gh", fake_run)
    assert gh.github_tree(tmp_path, "acme/engine", "src", ref="dev") == "(empty)"
    assert any(
        args[:2] == ["api", "repos/acme/engine/contents/src?ref=dev"] for args in seen
    )


def test_github_tree_recursive_path_filter(tmp_path, monkeypatch):
    def fake_run(workspace, args, timeout=60):
        return json.dumps(
            {
                "tree": [
                    {"path": "src/app.py", "type": "blob", "size": 20},
                    {"path": "src/pkg/mod.py", "type": "blob", "size": 8},
                    {"path": "docs/guide.md", "type": "blob", "size": 4},
                    {"path": "src", "type": "tree"},
                ]
            }
        )

    monkeypatch.setattr(gh, "run_gh", fake_run)
    text = gh.github_tree(
        tmp_path, "acme/engine", "src", ref="main", recursive=True
    )
    assert "file src/app.py 20" in text
    assert "file src/pkg/mod.py 8" in text
    assert "docs/guide.md" not in text
    assert "dir src\n" not in text + "\n"


def test_github_tree_caps(tmp_path, monkeypatch):
    payload = [
        {"type": "file", "name": f"f{i}.py", "path": f"f{i}.py", "size": 1}
        for i in range(gh.TREE_CAP + 5)
    ]
    monkeypatch.setattr(gh, "run_gh", lambda *a, **k: json.dumps(payload))
    text = gh.github_tree(tmp_path, "acme/engine")
    assert text.endswith("...[truncated]")
    assert text.count("\n") + 1 == gh.TREE_CAP + 1


def test_github_tree_recursive(tmp_path, monkeypatch):
    seen = []

    def fake_run(workspace, args, timeout=60):
        seen.append(args)
        if args[:2] == ["api", "repos/acme/engine"]:
            return json.dumps({"default_branch": "main"})
        return json.dumps(
            {
                "tree": [
                    {"path": "README.md", "type": "blob", "size": 10},
                    {"path": "src/app.py", "type": "blob", "size": 20},
                    {"path": "node_modules/pkg/index.js", "type": "blob", "size": 1},
                    {"path": "src", "type": "tree"},
                ]
            }
        )

    monkeypatch.setattr(gh, "run_gh", fake_run)
    text = gh.github_tree(tmp_path, "acme/engine", recursive=True)
    assert "file README.md 10" in text
    assert "file src/app.py 20" in text
    assert "dir src" in text
    assert "node_modules" not in text
    assert any("git/trees/main" in " ".join(args) for args in seen)


def test_github_repo_tool_wrapper(ctx, monkeypatch):
    from tools.github import github_repo

    monkeypatch.setattr(gh, "github_repo", lambda *a, **k: "ok-repo")
    assert github_repo(ctx, "acme/engine") == "ok-repo"


def test_github_tree_tool_wrapper(ctx, monkeypatch):
    from tools.github import github_tree

    seen = {}

    def fake(workspace, repo, path, *, ref="", recursive=False):
        seen["repo"] = repo
        seen["path"] = path
        seen["ref"] = ref
        seen["recursive"] = recursive
        return "ok-tree"

    monkeypatch.setattr(gh, "github_tree", fake)
    assert github_tree(ctx, "acme/engine", "src", "main", True) == "ok-tree"
    assert seen == {
        "repo": "acme/engine",
        "path": "src",
        "ref": "main",
        "recursive": True,
    }


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


def test_require_approval_unknown_prompts():
    called = []

    async def ask(question, kind="text"):
        called.append(question)
        return "yes"

    async def run():
        return await require_approval(
            SimpleNamespace(
                config=SimpleNamespace(exec_approval="nver"), ask_user=ask
            ),
            "Allow?",
        )

    assert asyncio.run(run()) == ""
    assert called == ["Allow?"]
