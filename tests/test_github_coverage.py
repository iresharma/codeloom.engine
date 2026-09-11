"""Tests for runtime/tools/github.py to improve coverage.

Focuses on missing lines from baseline coverage report.
"""
from __future__ import annotations

import json
import pytest
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from types import SimpleNamespace

from runtime.tools import github as gh


class TestRunGH:
    """Tests for run_gh function."""

    def test_run_gh_missing_binary(self, tmp_path, monkeypatch):
        """Test run_gh when gh binary is not installed."""
        monkeypatch.setattr(gh, "_which", lambda name: None)
        result = gh.run_gh(tmp_path, ["pr", "list"])
        assert result.startswith("error: gh not installed")

    def test_run_gh_returns_stdout(self, tmp_path, monkeypatch):
        """Test run_gh returns stdout on success."""
        def fake_exec(workspace, args, timeout=60):
            return SimpleNamespace(returncode=0, stdout="output here", stderr="")
        
        monkeypatch.setattr(gh, "_which", lambda name: "/usr/bin/gh")
        monkeypatch.setattr(gh, "exec_cmd", fake_exec)
        result = gh.run_gh(tmp_path, ["pr", "list"])
        assert result == "output here"


class TestCurrentRepo:
    """Tests for current_repo function."""

    def test_current_repo_success(self, tmp_path, monkeypatch):
        """Test current_repo returns repo name."""
        payload = {"nameWithOwner": "acme/engine"}
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: json.dumps(payload))
        result = gh.current_repo(tmp_path)
        assert result == "acme/engine"

    def test_current_repo_error_from_gh(self, tmp_path, monkeypatch):
        """Test current_repo propagates gh error."""
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: "error: not logged in")
        result = gh.current_repo(tmp_path)
        assert result == "error: not logged in"

    def test_current_repo_invalid_json(self, tmp_path, monkeypatch):
        """Test current_repo handles invalid JSON."""
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: "invalid json {")
        result = gh.current_repo(tmp_path)
        assert result.startswith("error: could not resolve repository")

    def test_current_repo_missing_field(self, tmp_path, monkeypatch):
        """Test current_repo handles missing nameWithOwner."""
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: json.dumps({}))
        result = gh.current_repo(tmp_path)
        assert result.startswith("error: could not resolve repository")


class TestPRList:
    """Tests for pr_list function."""

    def test_pr_list_formats_output(self, tmp_path, monkeypatch):
        """Test pr_list formats PR data."""
        payload = [
            {
                "number": 1,
                "title": "Fix bug",
                "author": {"login": "alice"},
                "state": "OPEN",
                "headRefName": "fix-bug",
                "url": "https://github.com/acme/engine/pull/1",
            }
        ]
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: json.dumps(payload))
        result = gh.pr_list(tmp_path)
        assert "#1" in result
        assert "Fix bug" in result
        assert "alice" in result


class TestPRView:
    """Tests for pr_view function."""

    def test_pr_view_basic_info(self, tmp_path, monkeypatch):
        """Test pr_view returns basic PR info."""
        payload = {
            "number": 42,
            "title": "New feature",
            "body": "This adds feature X",
            "author": {"login": "bob"},
            "state": "OPEN",
            "baseRefName": "main",
            "headRefName": "feature-x",
            "files": [],
            "url": "https://github.com/acme/engine/pull/42",
        }
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: json.dumps(payload))
        result = gh.pr_view(tmp_path, 42)
        assert "#42" in result
        assert "New feature" in result
        assert "bob" in result

    def test_pr_view_with_files(self, tmp_path, monkeypatch):
        """Test pr_view includes file list."""
        payload = {
            "number": 10,
            "title": "Changes",
            "body": "",
            "author": {"login": "carol"},
            "state": "OPEN",
            "baseRefName": "main",
            "headRefName": "changes",
            "files": [{"path": "src/main.py"}, {"path": "README.md"}],
            "url": "https://github.com/acme/engine/pull/10",
        }
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: json.dumps(payload))
        result = gh.pr_view(tmp_path, 10)
        assert "src/main.py" in result
        assert "README.md" in result

    def test_pr_view_with_diff(self, tmp_path, monkeypatch):
        """Test pr_view includes diff when requested."""
        called_args = []
        
        def fake_run_gh(workspace, args, timeout=60):
            called_args.append(args)
            if "diff" in args:
                return "--- a/file.py\n+++ b/file.py"
            return json.dumps({
                "number": 10,
                "title": "Changes",
                "body": "test",
                "author": {"login": "user"},
                "state": "OPEN",
                "baseRefName": "main",
                "headRefName": "changes",
                "files": [],
                "url": "https://example.com",
            })
        
        monkeypatch.setattr(gh, "run_gh", fake_run_gh)
        result = gh.pr_view(tmp_path, 10, include_diff=True)
        assert any("diff" in str(args) for args in called_args)


class TestPRComments:
    """Tests for pr_comments function."""

    def test_pr_comments_basic(self, tmp_path, monkeypatch):
        """Test pr_comments returns comments."""
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: "Comment text here")
        result = gh.pr_comments(tmp_path, 5)
        assert "Comment text here" in result


class TestPRChecks:
    """Tests for pr_checks function."""

    def test_pr_checks_basic(self, tmp_path, monkeypatch):
        """Test pr_checks returns check results."""
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: "✓ all checks passed")
        result = gh.pr_checks(tmp_path, 5)
        assert "✓ all checks passed" in result


class TestIssueList:
    """Tests for issue_list function."""

    def test_issue_list_formats_output(self, tmp_path, monkeypatch):
        """Test issue_list formats issue data."""
        payload = [
            {
                "number": 99,
                "title": "Bug report",
                "author": {"login": "dave"},
                "state": "OPEN",
                "url": "https://github.com/acme/engine/issues/99",
            }
        ]
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: json.dumps(payload))
        result = gh.issue_list(tmp_path)
        assert "#99" in result
        assert "Bug report" in result
        assert "dave" in result


class TestIssueView:
    """Tests for issue_view function."""

    def test_issue_view_basic_info(self, tmp_path, monkeypatch):
        """Test issue_view returns issue info."""
        payload = {
            "number": 55,
            "title": "Documentation needed",
            "body": "Please document the API",
            "author": {"login": "eve"},
            "state": "OPEN",
            "url": "https://github.com/acme/engine/issues/55",
        }
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: json.dumps(payload))
        result = gh.issue_view(tmp_path, 55)
        assert "#55" in result
        assert "Documentation needed" in result
        assert "eve" in result


class TestRunList:
    """Tests for run_list function."""

    def test_run_list_formats_output(self, tmp_path, monkeypatch):
        """Test run_list formats workflow run data."""
        payload = [
            {
                "databaseId": 1234,
                "name": "Tests",
                "status": "completed",
                "conclusion": "success",
                "headBranch": "main",
                "url": "https://github.com/acme/engine/runs/1234",
                "event": "push",
            }
        ]
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: json.dumps(payload))
        result = gh.run_list(tmp_path)
        assert "Tests" in result
        assert "success" in result


class TestRunView:
    """Tests for run_view function."""

    def test_run_view_basic_info(self, tmp_path, monkeypatch):
        """Test run_view returns run info."""
        called_args = []
        
        def fake_run_gh(workspace, args, timeout=60):
            called_args.append(args)
            if "--log-failed" not in args:
                return json.dumps({
                    "databaseId": 5678,
                    "name": "Build",
                    "status": "completed",
                    "conclusion": "failure",
                    "headBranch": "dev",
                    "url": "https://github.com/acme/engine/runs/5678",
                    "event": "push",
                    "displayTitle": "Build #100",
                })
            return "Error details"
        
        monkeypatch.setattr(gh, "run_gh", fake_run_gh)
        result = gh.run_view(tmp_path, "5678")
        assert "Build" in result or "5678" in result

    def test_run_view_empty_run_id(self, tmp_path):
        """Test run_view rejects empty run_id."""
        result = gh.run_view(tmp_path, "")
        assert result.startswith("error:")


class TestReleaseList:
    """Tests for release_list function."""

    def test_release_list_basic(self, tmp_path, monkeypatch):
        """Test release_list returns releases."""
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: "v1.0.0 - Initial release")
        result = gh.release_list(tmp_path)
        assert "v1.0.0" in result


class TestReleaseView:
    """Tests for release_view function."""

    def test_release_view_latest(self, tmp_path, monkeypatch):
        """Test release_view gets latest release."""
        payload = {
            "tagName": "v2.0.0",
            "name": "Version 2",
            "body": "Major update",
            "url": "https://github.com/acme/engine/releases/tag/v2.0.0",
            "publishedAt": "2024-01-01T00:00:00Z",
        }
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: json.dumps(payload))
        result = gh.release_view(tmp_path)
        assert "v2.0.0" in result
        assert "Major update" in result

    def test_release_view_by_tag(self, tmp_path, monkeypatch):
        """Test release_view gets specific tag."""
        called_args = []
        
        def fake_run_gh(workspace, args, timeout=60):
            called_args.append(args)
            return json.dumps({
                "tagName": "v1.5.0",
                "name": "Version 1.5",
                "body": "Bug fixes",
                "url": "https://github.com/acme/engine/releases/tag/v1.5.0",
                "publishedAt": "2023-06-01T00:00:00Z",
            })
        
        monkeypatch.setattr(gh, "run_gh", fake_run_gh)
        result = gh.release_view(tmp_path, "v1.5.0")
        assert "v1.5.0" in result
        assert any("v1.5.0" in str(args) for args in called_args)


class TestCompare:
    """Tests for compare function."""

    def test_compare_refs(self, tmp_path, monkeypatch):
        """Test compare returns comparison info."""
        payload = {
            "ahead_by": 5,
            "behind_by": 2,
            "status": "diverged",
            "commits": [
                {
                    "sha": "abc123def456",
                    "commit": {"message": "Fix issue\n\nDetailed explanation"},
                }
            ],
            "files": [{"filename": "src/app.py"}],
        }
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: json.dumps(payload))
        result = gh.compare(tmp_path, "main", "dev")
        assert "ahead=5" in result
        assert "behind=2" in result

    def test_compare_missing_base_or_head(self, tmp_path):
        """Test compare requires base and head."""
        result = gh.compare(tmp_path, "", "head")
        assert result.startswith("error:")
        result = gh.compare(tmp_path, "base", "")
        assert result.startswith("error:")


class TestSearchCode:
    """Tests for search_code function."""

    def test_search_code_basic(self, tmp_path, monkeypatch):
        """Test search_code returns code search results."""
        payload = [
            {
                "repository": {"nameWithOwner": "acme/engine"},
                "path": "src/main.py",
                "url": "https://github.com/acme/engine/blob/main/src/main.py",
                "textMatches": [{"fragment": "def main():"}],
            }
        ]
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: json.dumps(payload))
        result = gh.search_code(tmp_path, "def main")
        assert "src/main.py" in result

    def test_search_code_empty_query(self, tmp_path):
        """Test search_code requires query."""
        result = gh.search_code(tmp_path, "")
        assert result.startswith("error:")

    def test_search_code_this_repo(self, tmp_path, monkeypatch):
        """Test search_code filters to current repo."""
        called_args = []
        
        def fake_run_gh(workspace, args, timeout=60):
            called_args.append(args)
            if args[:2] == ["repo", "view"]:
                return json.dumps({"nameWithOwner": "acme/engine"})
            return json.dumps([])
        
        monkeypatch.setattr(gh, "run_gh", fake_run_gh)
        gh.search_code(tmp_path, "test", this_repo=True)
        assert any("repo:acme/engine" in " ".join(str(x) for x in args) for args in called_args)


class TestGithubFile:
    """Tests for github_file function."""

    def test_github_file_returns_content(self, tmp_path, monkeypatch):
        """Test github_file returns file content."""
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: "file content here")
        result = gh.github_file(tmp_path, "acme/engine", "README.md")
        assert "file content here" in result

    def test_github_file_empty_path(self, tmp_path):
        """Test github_file requires path."""
        result = gh.github_file(tmp_path, "acme/engine", "")
        assert result.startswith("error:")

    def test_github_file_directory_listing_hint(self, tmp_path, monkeypatch):
        """Test github_file detects directory listing."""
        listing = json.dumps([{"type": "file", "name": "README.md", "sha": "abc123", "size": 10, "html_url": "https://..."}])
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: listing)
        result = gh.github_file(tmp_path, "acme/engine", "docs")
        assert "error:" in result
        assert "github_tree" in result


class TestGithubRepo:
    """Tests for github_repo function."""

    def test_github_repo_returns_info(self, tmp_path, monkeypatch):
        """Test github_repo returns repo metadata."""
        payload = {
            "nameWithOwner": "acme/engine",
            "description": "The engine",
            "url": "https://github.com/acme/engine",
            "homepageUrl": "https://example.com",
            "defaultBranchRef": {"name": "main"},
            "primaryLanguage": {"name": "Python"},
            "licenseInfo": {"name": "MIT"},
            "repositoryTopics": [{"name": "agents"}],
            "stargazerCount": 100,
            "forkCount": 20,
            "updatedAt": "2024-01-01T00:00:00Z",
            "isPrivate": False,
        }
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: json.dumps(payload))
        result = gh.github_repo(tmp_path)
        assert "acme/engine" in result
        assert "Python" in result
        assert "100" in result


class TestGithubTree:
    """Tests for github_tree function."""

    def test_github_tree_lists_files(self, tmp_path, monkeypatch):
        """Test github_tree lists directory contents."""
        payload = [
            {"type": "file", "name": "README.md", "path": "README.md", "size": 100},
            {"type": "dir", "name": "src", "path": "src"},
        ]
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: json.dumps(payload))
        result = gh.github_tree(tmp_path, "acme/engine")
        assert "file README.md" in result
        assert "dir src" in result

    def test_github_tree_recursive(self, tmp_path, monkeypatch):
        """Test github_tree recursive option."""
        called_args = []
        
        def fake_run_gh(workspace, args, timeout=60):
            called_args.append(args)
            if args[:2] == ["repo", "view"]:
                return json.dumps({"default_branch": "main"})
            return json.dumps({
                "tree": [
                    {"path": "README.md", "type": "blob", "size": 100},
                    {"path": "src/app.py", "type": "blob", "size": 50},
                ]
            })
        
        monkeypatch.setattr(gh, "run_gh", fake_run_gh)
        result = gh.github_tree(tmp_path, "acme/engine", recursive=True)
        assert "README.md" in result


class TestHelperFunctions:
    """Tests for helper functions."""

    def test_as_int_valid(self):
        """Test _as_int with valid integer."""
        result = gh._as_int(42, 0)
        assert result == 42

    def test_as_int_string(self):
        """Test _as_int converts string."""
        result = gh._as_int("100", 0)
        assert result == 100

    def test_as_int_none_uses_default(self):
        """Test _as_int uses default for None."""
        result = gh._as_int(None, 99)
        assert result == 99

    def test_as_int_invalid_uses_default(self):
        """Test _as_int uses default for invalid."""
        result = gh._as_int("not_a_number", 77)
        assert result == 77

    def test_clip_short_text(self):
        """Test _clip doesn't modify short text."""
        result = gh._clip("short", 100)
        assert result == "short"

    def test_clip_long_text(self):
        """Test _clip truncates long text."""
        result = gh._clip("x" * 200, 100)
        assert "...[truncated]" in result
        assert len(result) <= 115

    def test_login_from_dict(self):
        """Test _login extracts login from dict."""
        result = gh._login({"login": "alice"})
        assert result == "alice"

    def test_login_from_string(self):
        """Test _login handles string."""
        result = gh._login("bob")
        assert result == "bob"

    def test_nested_name_from_dict(self):
        """Test _nested_name extracts name."""
        result = gh._nested_name({"name": "Python"})
        assert result == "Python"

    def test_nested_name_fallback_key(self):
        """Test _nested_name fallback to key."""
        result = gh._nested_name({"key": "MIT"})
        assert result == "MIT"

    def test_nested_name_fallback_spdxid(self):
        """Test _nested_name fallback to spdxId."""
        result = gh._nested_name({"spdxId": "MIT"})
        assert result == "MIT"

    def test_topics_from_list(self):
        """Test _topics extracts from list."""
        result = gh._topics(["python", "ai"])
        assert "python" in result
        assert "ai" in result

    def test_topics_from_dict_nodes(self):
        """Test _topics extracts from nodes."""
        result = gh._topics({"nodes": [{"name": "test"}]})
        assert "test" in result

    def test_topics_empty(self):
        """Test _topics returns empty list for None."""
        result = gh._topics(None)
        assert result == []

    def test_limit_enforces_bounds(self):
        """Test _limit enforces min and max."""
        result = gh._limit(1000, 20)
        assert result == 20
        result = gh._limit(0, 20)
        assert result == 1
        result = gh._limit(10, 20)
        assert result == 10


class TestPRComment:
    """Tests for pr_comment function."""

    def test_pr_comment_success(self, tmp_path, monkeypatch):
        """Test pr_comment posts comment."""
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: "https://github.com/.../comments/123")
        result = gh.pr_comment(tmp_path, 10, "Great work!")
        assert "https://github.com" in result or "ok" in result

    def test_pr_comment_empty_body(self, tmp_path):
        """Test pr_comment requires body."""
        result = gh.pr_comment(tmp_path, 10, "")
        assert result.startswith("error:")


class TestIssueCreate:
    """Tests for issue_create function."""

    def test_issue_create_with_title_only(self, tmp_path, monkeypatch):
        """Test issue_create with title."""
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: "https://github.com/.../issues/456")
        result = gh.issue_create(tmp_path, "Bug found")
        assert "https://github.com" in result or "ok" in result

    def test_issue_create_with_body(self, tmp_path, monkeypatch):
        """Test issue_create with body."""
        called_args = []
        
        def fake_run_gh(workspace, args, timeout=60):
            called_args.append(args)
            return "ok"
        
        monkeypatch.setattr(gh, "run_gh", fake_run_gh)
        gh.issue_create(tmp_path, "Bug", body="Description here")
        assert any("--body" in str(args) for args in called_args)

    def test_issue_create_empty_title(self, tmp_path):
        """Test issue_create requires title."""
        result = gh.issue_create(tmp_path, "")
        assert result.startswith("error:")


class TestPRCreate:
    """Tests for pr_create function."""

    def test_pr_create_with_title(self, tmp_path, monkeypatch):
        """Test pr_create with title."""
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: "https://github.com/.../pull/789")
        result = gh.pr_create(tmp_path, "New feature")
        assert "https://github.com" in result or "ok" in result

    def test_pr_create_with_base(self, tmp_path, monkeypatch):
        """Test pr_create with base branch."""
        called_args = []
        
        def fake_run_gh(workspace, args, timeout=60):
            called_args.append(args)
            return "ok"
        
        monkeypatch.setattr(gh, "run_gh", fake_run_gh)
        gh.pr_create(tmp_path, "Feature", base="develop")
        assert any("--base" in str(args) for args in called_args)

    def test_pr_create_empty_title(self, tmp_path):
        """Test pr_create requires title."""
        result = gh.pr_create(tmp_path, "")
        assert result.startswith("error:")


class TestRepo:
    """Tests for _repo function."""

    def test_repo_with_value(self):
        """Test _repo returns -R flag with repo."""
        result = gh._repo("acme/engine")
        assert result == ["-R", "acme/engine"]

    def test_repo_empty(self):
        """Test _repo returns empty for empty repo."""
        result = gh._repo("")
        assert result == []
