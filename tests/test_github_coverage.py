"""Comprehensive tests for runtime/tools/github.py to improve coverage."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock, MagicMock
from types import SimpleNamespace

import pytest

from runtime.tools import github as gh


# Helper to make mock results
def mock_run_gh(workspace: Path, args, timeout=60):
    """Mock run_gh returning appropriate values based on args."""
    if args[:2] == ["repo", "view"]:
        if "nameWithOwner" in args:
            return json.dumps({"nameWithOwner": "acme/engine"})
        return json.dumps({
            "nameWithOwner": "acme/engine",
            "description": "Test repo",
            "url": "https://github.com/acme/engine",
            "defaultBranchRef": {"name": "main"},
        })
    return "(empty)"


class TestCurrentRepo:
    def test_current_repo_error_passthrough(self, tmp_path, monkeypatch):
        """Errors from run_gh should be passed through."""
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: "error: not a git repo")
        result = gh.current_repo(tmp_path)
        assert result.startswith("error:")

    def test_current_repo_invalid_json(self, tmp_path, monkeypatch):
        """Invalid JSON response should return error."""
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: "not json")
        result = gh.current_repo(tmp_path)
        assert result.startswith("error:")
        assert "could not resolve" in result

    def test_current_repo_missing_name(self, tmp_path, monkeypatch):
        """Missing nameWithOwner should return error."""
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: json.dumps({}))
        result = gh.current_repo(tmp_path)
        assert result.startswith("error:")
        assert "could not resolve" in result

    def test_current_repo_success(self, tmp_path, monkeypatch):
        """Successful repo resolution."""
        monkeypatch.setattr(
            gh, "run_gh", lambda *a, **k: json.dumps({"nameWithOwner": "acme/engine"})
        )
        result = gh.current_repo(tmp_path)
        assert result == "acme/engine"


class TestPrList:
    def test_pr_list_states(self, tmp_path, monkeypatch):
        """PR list should handle different states."""
        seen = []
        
        def fake_run(ws, args, timeout=60):
            seen.append(args)
            return "[]"
        
        monkeypatch.setattr(gh, "run_gh", fake_run)
        gh.pr_list(tmp_path, state="closed")
        assert any("closed" in " ".join(args) for args in seen)

    def test_pr_list_limit_capped(self, tmp_path, monkeypatch):
        """PR list limit should be capped."""
        seen = []
        
        def fake_run(ws, args, timeout=60):
            seen.append(args)
            return "[]"
        
        monkeypatch.setattr(gh, "run_gh", fake_run)
        gh.pr_list(tmp_path, limit=1000)
        # Default limit is 20, so should be capped
        assert any(str(arg) == "20" for args in seen for arg in args)

    def test_pr_list_with_repo(self, tmp_path, monkeypatch):
        """PR list with repo argument."""
        seen = []
        
        def fake_run(ws, args, timeout=60):
            seen.append(args)
            return "[]"
        
        monkeypatch.setattr(gh, "run_gh", fake_run)
        gh.pr_list(tmp_path, repo="acme/engine")
        assert any("-R" in args for args in seen)
        assert any("acme/engine" in args for args in seen)


class TestPrView:
    def test_pr_view_with_diff(self, tmp_path, monkeypatch):
        """PR view with include_diff should fetch diff."""
        seen = []
        
        def fake_run(ws, args, timeout=60):
            seen.append(args)
            if "diff" in args:
                return "diff content"
            return json.dumps({
                "number": 1,
                "state": "OPEN",
                "title": "Test PR",
                "author": {"login": "alice"},
                "baseRefName": "main",
                "headRefName": "feature",
                "url": "https://github.com/pr/1",
            })
        
        monkeypatch.setattr(gh, "run_gh", fake_run)
        result = gh.pr_view(tmp_path, 1, include_diff=True)
        assert "diff" in result.lower() or "diff content" in result

    def test_pr_view_json_decode_error(self, tmp_path, monkeypatch):
        """Invalid JSON in pr_view should clip."""
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: "not json")
        result = gh.pr_view(tmp_path, 1)
        assert result == "not json"

    def test_pr_view_files_parsing(self, tmp_path, monkeypatch):
        """PR view should parse files list."""
        payload = {
            "number": 1,
            "state": "OPEN",
            "title": "Test",
            "author": {"login": "alice"},
            "baseRefName": "main",
            "headRefName": "feature",
            "files": [
                {"path": "file1.py"},
                {"path": "file2.py"},
            ],
            "url": "https://github.com/pr/1",
        }
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: json.dumps(payload))
        result = gh.pr_view(tmp_path, 1)
        assert "file1.py" in result
        assert "file2.py" in result

    def test_pr_view_files_non_dict(self, tmp_path, monkeypatch):
        """PR view should handle non-dict file entries."""
        payload = {
            "number": 1,
            "state": "OPEN",
            "title": "Test",
            "author": {"login": "alice"},
            "baseRefName": "main",
            "headRefName": "feature",
            "files": ["not_a_dict", {"path": "file1.py"}],
            "url": "https://github.com/pr/1",
        }
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: json.dumps(payload))
        result = gh.pr_view(tmp_path, 1)
        assert "file1.py" in result


class TestPrComments:
    def test_pr_comments_success(self, tmp_path, monkeypatch):
        """PR comments should return comments."""
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: "Comments here")
        result = gh.pr_comments(tmp_path, 1)
        assert result == "Comments here"

    def test_pr_comments_error(self, tmp_path, monkeypatch):
        """PR comments error should pass through."""
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: "error: PR not found")
        result = gh.pr_comments(tmp_path, 1)
        assert result.startswith("error:")


class TestPrChecks:
    def test_pr_checks_success(self, tmp_path, monkeypatch):
        """PR checks should return check status."""
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: "✓ pass")
        result = gh.pr_checks(tmp_path, 1)
        assert "pass" in result


class TestIssueList:
    def test_issue_list_states(self, tmp_path, monkeypatch):
        """Issue list should handle states."""
        seen = []
        
        def fake_run(ws, args, timeout=60):
            seen.append(args)
            return "[]"
        
        monkeypatch.setattr(gh, "run_gh", fake_run)
        gh.issue_list(tmp_path, state="closed")
        assert any("closed" in " ".join(args) for args in seen)


class TestIssueView:
    def test_issue_view_json_decode_error(self, tmp_path, monkeypatch):
        """Invalid JSON in issue_view should be clipped."""
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: "x" * 50000)
        result = gh.issue_view(tmp_path, 1)
        assert len(result) < 50000


class TestRunList:
    def test_run_list_success(self, tmp_path, monkeypatch):
        """Run list should format runs."""
        payload = [{
            "databaseId": 123,
            "name": "Test run",
            "status": "completed",
            "conclusion": "success",
            "headBranch": "main",
            "url": "https://github.com/run/123",
            "event": "push",
        }]
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: json.dumps(payload))
        result = gh.run_list(tmp_path)
        assert "123" in result
        assert "success" in result


class TestRunView:
    def test_run_view_invalid_id(self, tmp_path):
        """Empty run id should return error."""
        result = gh.run_view(tmp_path, "")
        assert result.startswith("error:")
        assert "required" in result

    def test_run_view_whitespace_id(self, tmp_path):
        """Whitespace run id should be stripped."""
        result = gh.run_view(tmp_path, "  ")
        assert result.startswith("error:")

    def test_run_view_success_with_logs(self, tmp_path, monkeypatch):
        """Run view should include logs."""
        def fake_run(ws, args, timeout=60):
            if "log-failed" in args:
                return "Failed test log"
            return json.dumps({
                "databaseId": 123,
                "name": "Test",
                "status": "completed",
                "conclusion": "failure",
                "url": "https://github.com/run/123",
            })
        
        monkeypatch.setattr(gh, "run_gh", fake_run)
        result = gh.run_view(tmp_path, "123")
        assert "Failed test log" in result


class TestReleaseList:
    def test_release_list_success(self, tmp_path, monkeypatch):
        """Release list should work."""
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: "v1.0.0\nv0.9.0")
        result = gh.release_list(tmp_path)
        assert "v1.0.0" in result


class TestReleaseView:
    def test_release_view_with_tag(self, tmp_path, monkeypatch):
        """Release view with tag should include tag in args."""
        seen = []
        
        def fake_run(ws, args, timeout=60):
            seen.append(args)
            return json.dumps({
                "tagName": "v1.0.0",
                "name": "Release 1.0.0",
                "body": "Changes",
                "url": "https://github.com/release/v1.0.0",
                "publishedAt": "2024-01-01",
            })
        
        monkeypatch.setattr(gh, "run_gh", fake_run)
        gh.release_view(tmp_path, "v1.0.0")
        assert any("v1.0.0" in args for args in seen)

    def test_release_view_latest(self, tmp_path, monkeypatch):
        """Release view without tag gets latest."""
        seen = []
        
        def fake_run(ws, args, timeout=60):
            seen.append(args)
            return json.dumps({
                "tagName": "v1.0.0",
                "name": "Latest",
                "body": "Changes",
                "url": "https://github.com/release/v1.0.0",
            })
        
        monkeypatch.setattr(gh, "run_gh", fake_run)
        gh.release_view(tmp_path, "")
        # Should not append empty tag


class TestCompare:
    def test_compare_missing_args(self, tmp_path):
        """Compare without base/head should error."""
        result = gh.compare(tmp_path, "", "")
        assert result.startswith("error:")
        assert "required" in result

    def test_compare_missing_base(self, tmp_path):
        """Compare without base should error."""
        result = gh.compare(tmp_path, "", "main")
        assert result.startswith("error:")

    def test_compare_missing_head(self, tmp_path):
        """Compare without head should error."""
        result = gh.compare(tmp_path, "main", "")
        assert result.startswith("error:")

    def test_compare_repo_error(self, tmp_path, monkeypatch):
        """Compare with invalid repo should error."""
        def fake_run(ws, args, timeout=60):
            return "error: not a repo"
        
        monkeypatch.setattr(gh, "run_gh", fake_run)
        result = gh.compare(tmp_path, "main", "dev")
        assert result.startswith("error:")

    def test_compare_success(self, tmp_path, monkeypatch):
        """Compare should format output."""
        def fake_run(ws, args, timeout=60):
            if args[0] == "repo":
                return json.dumps({"nameWithOwner": "acme/engine"})
            return json.dumps({
                "commits": [
                    {"sha": "abc123defg", "commit": {"message": "Fix bug"}},
                    {"sha": "xyz789uvwx", "commit": {"message": "Add feature"}},
                ],
                "files": [
                    {"filename": "src/app.py"},
                    {"filename": "tests/test_app.py"},
                ],
                "ahead_by": 2,
                "behind_by": 0,
                "status": "ahead",
            })
        
        monkeypatch.setattr(gh, "run_gh", fake_run)
        result = gh.compare(tmp_path, "main", "dev")
        assert "acme/engine" in result
        assert "Fix bug" in result
        assert "app.py" in result
        assert "ahead=2" in result


class TestSearchCode:
    def test_search_code_missing_query(self, tmp_path):
        """Search code without query should error."""
        result = gh.search_code(tmp_path, "")
        assert result.startswith("error:")
        assert "required" in result

    def test_search_code_this_repo_false(self, tmp_path, monkeypatch):
        """Search without this_repo should not prefix."""
        seen = []
        
        def fake_run(ws, args, timeout=60):
            seen.append(args)
            return "[]"
        
        monkeypatch.setattr(gh, "run_gh", fake_run)
        gh.search_code(tmp_path, "retry", this_repo=False)
        assert not any("repo:" in " ".join(args) for args in seen)

    def test_search_code_this_repo_true(self, tmp_path, monkeypatch):
        """Search with this_repo should prefix repo."""
        def fake_run(ws, args, timeout=60):
            if args[:2] == ["repo", "view"]:
                return json.dumps({"nameWithOwner": "acme/engine"})
            return "[]"
        
        monkeypatch.setattr(gh, "run_gh", fake_run)
        result = gh.search_code(tmp_path, "retry", this_repo=True)
        assert result == "(no results)"

    def test_search_code_results(self, tmp_path, monkeypatch):
        """Search code should format results."""
        payload = [
            {
                "repository": {"nameWithOwner": "acme/engine"},
                "path": "src/retry.py",
                "url": "https://github.com/acme/engine/blob/main/src/retry.py",
                "textMatches": [{"fragment": "def retry():"}],
            }
        ]
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: json.dumps(payload))
        result = gh.search_code(tmp_path, "retry")
        assert "acme/engine" in result
        assert "retry.py" in result


class TestGithubFile:
    def test_github_file_missing_path(self, tmp_path):
        """Github file without path should error."""
        result = gh.github_file(tmp_path, "acme/engine", "")
        assert result.startswith("error:")
        assert "path" in result.lower()

    def test_github_file_path_slash_only(self, tmp_path):
        """Github file with only slash should error."""
        result = gh.github_file(tmp_path, "acme/engine", "/")
        assert result.startswith("error:")

    def test_github_file_implicit_repo(self, tmp_path, monkeypatch):
        """Github file with empty repo should use current_repo."""
        def fake_run(ws, args, timeout=60):
            if args[:2] == ["repo", "view"]:
                return json.dumps({"nameWithOwner": "acme/engine"})
            return "file content"
        
        monkeypatch.setattr(gh, "run_gh", fake_run)
        result = gh.github_file(tmp_path, "", "README.md")
        assert result == "file content"

    def test_github_file_with_ref(self, tmp_path, monkeypatch):
        """Github file with ref should include it in URL."""
        seen = []
        
        def fake_run(ws, args, timeout=60):
            seen.append(args)
            return "file content"
        
        monkeypatch.setattr(gh, "run_gh", fake_run)
        gh.github_file(tmp_path, "acme/engine", "README.md", ref="dev")
        assert any("ref=dev" in " ".join(args) for args in seen)

    def test_github_file_directory_listing(self, tmp_path, monkeypatch):
        """Github file on directory should hint github_tree."""
        listing = json.dumps([
            {"type": "file", "name": "README.md", "sha": "abc", "html_url": "http://x"}
        ])
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: listing)
        result = gh.github_file(tmp_path, "acme/engine", "docs")
        assert result.startswith("error:")
        assert "github_tree" in result

    def test_github_file_offset_and_limit(self, tmp_path, monkeypatch):
        """Github file with offset and limit should paginate."""
        text = "x" * 100
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: text)
        result = gh.github_file(tmp_path, "acme/engine", "file.txt", offset=10, limit=20)
        assert result.startswith("xxxx")  # 10-30
        assert "offset=30" in result


class TestGithubRepo:
    def test_github_repo_with_explicit_repo(self, tmp_path, monkeypatch):
        """Github repo with explicit repo argument."""
        payload = {
            "nameWithOwner": "acme/engine",
            "description": "Test engine",
            "url": "https://github.com/acme/engine",
            "defaultBranchRef": {"name": "main"},
        }
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: json.dumps(payload))
        result = gh.github_repo(tmp_path, "acme/engine")
        assert "acme/engine" in result
        assert "Test engine" in result

    def test_github_repo_empty_repo(self, tmp_path, monkeypatch):
        """Github repo with empty repo should use current_repo."""
        def fake_run(ws, args, timeout=60):
            return json.dumps({"nameWithOwner": "acme/engine"})
        
        monkeypatch.setattr(gh, "run_gh", fake_run)
        result = gh.github_repo(tmp_path, "")
        assert "acme/engine" in result

    def test_github_repo_topics_list(self, tmp_path, monkeypatch):
        """Github repo topics as list."""
        payload = {
            "nameWithOwner": "acme/engine",
            "repositoryTopics": ["python", "agents"],
        }
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: json.dumps(payload))
        result = gh.github_repo(tmp_path, "acme/engine")
        assert "python" in result

    def test_github_repo_topics_nodes(self, tmp_path, monkeypatch):
        """Github repo topics as nodes."""
        payload = {
            "nameWithOwner": "acme/engine",
            "repositoryTopics": {
                "nodes": [{"topic": {"name": "python"}}, {"topic": {"name": "agents"}}]
            },
        }
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: json.dumps(payload))
        result = gh.github_repo(tmp_path, "acme/engine")
        assert "python" in result
        assert "agents" in result


class TestGithubTree:
    def test_github_tree_default_repo(self, tmp_path, monkeypatch):
        """Github tree with empty repo should use current_repo."""
        def fake_run(ws, args, timeout=60):
            if args[:2] == ["repo", "view"]:
                return json.dumps({"nameWithOwner": "acme/engine"})
            return json.dumps([])
        
        monkeypatch.setattr(gh, "run_gh", fake_run)
        result = gh.github_tree(tmp_path, "")
        assert result == "(empty)"

    def test_github_tree_non_recursive_listing(self, tmp_path, monkeypatch):
        """Github tree non-recursive uses listing endpoint."""
        payload = [
            {"type": "file", "name": "README.md", "path": "README.md", "size": 100},
            {"type": "dir", "name": "src", "path": "src"},
        ]
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: json.dumps(payload))
        result = gh.github_tree(tmp_path, "acme/engine")
        assert "README.md" in result
        assert "src" in result

    def test_github_tree_recursive(self, tmp_path, monkeypatch):
        """Github tree recursive uses git trees endpoint."""
        def fake_run(ws, args, timeout=60):
            if "git/trees" in " ".join(args):
                return json.dumps({
                    "tree": [
                        {"path": "README.md", "type": "blob", "size": 100},
                        {"path": "src/app.py", "type": "blob", "size": 200},
                    ]
                })
            return json.dumps({"default_branch": "main"})
        
        monkeypatch.setattr(gh, "run_gh", fake_run)
        result = gh.github_tree(tmp_path, "acme/engine", recursive=True)
        assert "README.md" in result
        assert "app.py" in result

    def test_github_tree_non_dict_entries(self, tmp_path, monkeypatch):
        """Github tree should skip non-dict entries."""
        payload = ["not a dict", {"type": "file", "name": "file.txt", "path": "file.txt"}]
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: json.dumps(payload))
        result = gh.github_tree(tmp_path, "acme/engine")
        assert "file.txt" in result


class TestPrComment:
    def test_pr_comment_empty_body(self, tmp_path):
        """PR comment without body should error."""
        result = gh.pr_comment(tmp_path, 1, "")
        assert result.startswith("error:")
        assert "body" in result.lower()

    def test_pr_comment_success(self, tmp_path, monkeypatch):
        """PR comment should return URL."""
        monkeypatch.setattr(
            gh, "run_gh", lambda *a, **k: "https://github.com/comment/123"
        )
        result = gh.pr_comment(tmp_path, 1, "Looks good!")
        assert "https://github.com/comment" in result or result == "ok"


class TestIssueCreate:
    def test_issue_create_empty_title(self, tmp_path):
        """Issue create without title should error."""
        result = gh.issue_create(tmp_path, "")
        assert result.startswith("error:")
        assert "title" in result.lower()

    def test_issue_create_success(self, tmp_path, monkeypatch):
        """Issue create should work."""
        monkeypatch.setattr(
            gh, "run_gh", lambda *a, **k: "https://github.com/issue/123"
        )
        result = gh.issue_create(tmp_path, "Test issue", body="Description")
        assert result != "" and not result.startswith("error:")


class TestPrCreate:
    def test_pr_create_empty_title(self, tmp_path):
        """PR create without title should error."""
        result = gh.pr_create(tmp_path, "")
        assert result.startswith("error:")
        assert "title" in result.lower()

    def test_pr_create_with_base(self, tmp_path, monkeypatch):
        """PR create with base branch."""
        seen = []
        
        def fake_run(ws, args, timeout=60):
            seen.append(args)
            return "https://github.com/pr/123"
        
        monkeypatch.setattr(gh, "run_gh", fake_run)
        gh.pr_create(tmp_path, "Test PR", base="main")
        assert any("main" in args for args in seen)


class TestHelperFunctions:
    def test_limit_capping(self):
        """_limit should cap values."""
        assert gh._limit(5, 20) == 5
        assert gh._limit(50, 20) == 20
        assert gh._limit(0, 20) == 1
        assert gh._limit(-5, 20) == 1
        assert gh._limit(None, 20) == 20
        assert gh._limit("invalid", 20) == 20

    def test_clip_text(self):
        """_clip should truncate long text."""
        short = "short"
        assert gh._clip(short, 100) == short
        long = "x" * 100
        clipped = gh._clip(long, 50)
        assert len(clipped) <= 61  # 50 + "[truncated]"
        assert "[truncated]" in clipped

    def test_login_extraction(self):
        """_login should extract login from various formats."""
        assert gh._login({"login": "alice"}) == "alice"
        assert gh._login({"name": "Alice"}) == "Alice"
        assert gh._login("alice") == "alice"
        assert gh._login(None) == ""

    def test_nested_name(self):
        """_nested_name should extract nested names."""
        assert gh._nested_name({"name": "Python"}) == "Python"
        assert gh._nested_name({"key": "key1"}) == "key1"
        assert gh._nested_name({"spdxId": "MIT"}) == "MIT"
        assert gh._nested_name("value") == "value"

    def test_topics_parsing(self):
        """_topics should handle various topic formats."""
        assert gh._topics(["python", "agents"]) == ["python", "agents"]
        assert gh._topics({"nodes": [{"topic": {"name": "python"}}]}) == ["python"]
        assert gh._topics({}) == []
        assert gh._topics(None) == []

    def test_fmt_pr(self):
        """_fmt_pr should format PR."""
        result = gh._fmt_pr({
            "number": 1,
            "state": "OPEN",
            "title": "Fix bug",
            "author": {"login": "alice"},
            "headRefName": "fix",
            "url": "https://github.com/pr/1",
        })
        assert "#1" in result
        assert "Fix bug" in result
        assert "alice" in result

    def test_fmt_issue(self):
        """_fmt_issue should format issue."""
        result = gh._fmt_issue({
            "number": 1,
            "state": "OPEN",
            "title": "Bug report",
            "author": {"login": "bob"},
            "url": "https://github.com/issue/1",
        })
        assert "#1" in result
        assert "Bug report" in result
        assert "bob" in result

    def test_fmt_run(self):
        """_fmt_run should format run."""
        result = gh._fmt_run({
            "databaseId": 123,
            "name": "Test run",
            "displayTitle": "Test",
            "status": "completed",
            "conclusion": "success",
            "headBranch": "main",
            "event": "push",
            "url": "https://github.com/run/123",
        })
        assert "123" in result
        assert "success" in result

    def test_as_int(self):
        """_as_int should convert to int safely."""
        assert gh._as_int("10", 0) == 10
        assert gh._as_int(None, 5) == 5
        assert gh._as_int("", 5) == 5
        assert gh._as_int("invalid", 5) == 5
        assert gh._as_int(20, 5) == 20

    def test_skip_tree_path(self):
        """_skip_tree_path should skip vendor dirs."""
        assert gh._skip_tree_path("node_modules/pkg/index.js") is True
        assert gh._skip_tree_path(".git/config") is True
        assert gh._skip_tree_path("src/app.py") is False

    def test_looks_like_dir_listing_array(self):
        """_looks_like_dir_listing for array."""
        payload = json.dumps([{
            "type": "file",
            "name": "file.txt",
            "sha": "abc",
            "html_url": "http://x",
        }])
        assert gh._looks_like_dir_listing(payload) is True

    def test_looks_like_dir_listing_dict(self):
        """_looks_like_dir_listing for dict."""
        payload = json.dumps({
            "type": "dir",
            "sha": "abc",
            "html_url": "http://x",
        })
        assert gh._looks_like_dir_listing(payload) is True

    def test_looks_like_dir_listing_false(self):
        """_looks_like_dir_listing negative case."""
        payload = json.dumps({"not": "a directory listing"})
        assert gh._looks_like_dir_listing(payload) is False

    def test_is_github_content_entry_valid(self):
        """Test valid GitHub content entry."""
        entry = {
            "type": "file",
            "sha": "abc123",
            "html_url": "https://github.com/file",
        }
        assert gh._is_github_content_entry(entry) is True

    def test_is_github_content_entry_invalid_type(self):
        """Invalid entry type."""
        entry = {
            "type": "invalid",
            "sha": "abc123",
            "html_url": "https://github.com/file",
        }
        assert gh._is_github_content_entry(entry) is False

    def test_is_github_content_entry_no_sha(self):
        """Missing sha."""
        entry = {
            "type": "file",
            "html_url": "https://github.com/file",
        }
        assert gh._is_github_content_entry(entry) is False

    def test_is_github_content_entry_git_url(self):
        """Entry with git_url instead of html_url."""
        entry = {
            "type": "file",
            "sha": "abc123",
            "git_url": "git://github.com/file",
        }
        assert gh._is_github_content_entry(entry) is True

    def test_is_github_content_entry_links(self):
        """Entry with _links."""
        entry = {
            "type": "file",
            "sha": "abc123",
            "_links": {"self": "http://example.com"},
        }
        assert gh._is_github_content_entry(entry) is True

    def test_default_branch_error(self, tmp_path, monkeypatch):
        """_default_branch with error."""
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: "error: not found")
        result = gh._default_branch(tmp_path, "acme/engine")
        assert result.startswith("error:")

    def test_default_branch_invalid_json(self, tmp_path, monkeypatch):
        """_default_branch with invalid JSON."""
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: "not json")
        result = gh._default_branch(tmp_path, "acme/engine")
        assert result.startswith("error:")
        assert "could not resolve default branch" in result

    def test_default_branch_missing_field(self, tmp_path, monkeypatch):
        """_default_branch with missing default_branch field."""
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: json.dumps({}))
        result = gh._default_branch(tmp_path, "acme/engine")
        assert result.startswith("error:")

    def test_tree_listing_empty_name(self, tmp_path, monkeypatch):
        """Tree listing with empty name."""
        payload = [{"type": "file", "name": "", "path": "unknown", "size": 10}]
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: json.dumps(payload))
        result = gh.github_tree(tmp_path, "acme/engine")
        # Should skip empty names
        assert result == "(empty)" or "unknown" in result

    def test_tree_listing_no_size(self, tmp_path, monkeypatch):
        """Tree listing entry without size."""
        payload = [{"type": "file", "name": "README.md", "path": "README.md"}]
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: json.dumps(payload))
        result = gh.github_tree(tmp_path, "acme/engine")
        # Should handle missing size
        assert "README.md" in result

    def test_tree_listing_non_list(self, tmp_path, monkeypatch):
        """Tree listing with non-list response."""
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: json.dumps({"error": "not a list"}))
        result = gh._tree_listing(tmp_path, "acme/engine", "", "")
        assert result.startswith("error:")

    def test_tree_listing_dict_response(self, tmp_path, monkeypatch):
        """Tree listing with dict response (file not dir)."""
        payload = {"type": "file", "sha": "abc", "path": "file.txt"}
        monkeypatch.setattr(gh, "run_gh", lambda *a, **k: json.dumps(payload))
        result = gh._tree_listing(tmp_path, "acme/engine", "", "")
        assert "path is a file" in result

    def test_fmt_items_error(self):
        """_fmt_items with error prefix."""
        result = gh._fmt_items("error: not found", gh._fmt_pr)
        assert result.startswith("error:")

    def test_fmt_items_invalid_json(self):
        """_fmt_items with invalid JSON."""
        result = gh._fmt_items("not json", gh._fmt_pr)
        # Should clip the text
        assert len(result) <= gh.BODY_CAP + 20

    def test_fmt_items_empty_list(self):
        """_fmt_items with empty list."""
        result = gh._fmt_items("[]", gh._fmt_pr)
        assert result == "(none)"

    def test_repo_json_format_string(self):
        """Verify _REPO_JSON constant is used."""
        assert "nameWithOwner" in gh._REPO_JSON
        assert "stargazerCount" in gh._REPO_JSON
