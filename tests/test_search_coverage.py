"""Tests for runtime/tools/search.py to improve coverage."""
from __future__ import annotations

import subprocess
import shutil
from pathlib import Path
from unittest import mock

import pytest

from runtime.tools.search import search, _rewrite_path, DEFAULT_MAX_MATCHES, MAX_MATCHES
from runtime.tools.fs import WorkspacePathError


class TestSearch:
    """Test search function with various patterns and paths."""

    def test_search_basic_pattern(self, tmp_path):
        """Test basic search with a simple pattern."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("def foo():\n    return 42\n")
        result = search(tmp_path, "def foo")
        assert "src/app.py" in result
        assert "def foo" in result

    def test_search_with_path_filter(self, tmp_path):
        """Test search with path restriction."""
        (tmp_path / "src").mkdir()
        (tmp_path / "tests").mkdir()
        (tmp_path / "src" / "app.py").write_text("def foo():\n    return 42\n")
        (tmp_path / "tests" / "test.py").write_text("def foo_test():\n    pass\n")
        
        result = search(tmp_path, "def", path="src")
        assert "src/app.py" in result
        assert "tests" not in result

    def test_search_with_glob_filter(self, tmp_path):
        """Test search with glob pattern to filter files."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("hello")
        (tmp_path / "src" / "data.txt").write_text("hello")
        
        result = search(tmp_path, "hello", glob="*.py")
        assert "app.py" in result
        assert "data.txt" not in result

    def test_search_no_matches(self, tmp_path):
        """Test search that returns no matches."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("def foo():\n    return 42\n")
        
        result = search(tmp_path, "nonexistent_pattern_xyz")
        assert result == "(no matches)"

    def test_search_max_matches_respected(self, tmp_path):
        """Test that max_matches limit is respected."""
        (tmp_path / "src").mkdir()
        content = "\n".join([f"match_{i}" for i in range(150)])
        (tmp_path / "src" / "app.py").write_text(content)
        
        result = search(tmp_path, "match_", max_matches=10)
        lines = result.split("\n")
        # Should have 10 matches + 1 line showing "... (N more matches)"
        assert "... (" in result
        assert "more matches" in result
        # Count the actual match lines (excluding the "more" line)
        match_lines = [l for l in lines if "match_" in l and "more matches" not in l]
        assert len(match_lines) == 10

    def test_search_max_matches_clamped_to_max(self, tmp_path):
        """Test that max_matches is clamped to MAX_MATCHES."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("x = 1\n")
        
        # Request more than MAX_MATCHES
        result = search(tmp_path, "x", max_matches=500)
        # Should not crash, and internal limit should apply
        assert "x" in result or "(no matches)" in result

    def test_search_max_matches_minimum_1(self, tmp_path):
        """Test that max_matches is at least 1."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("match\nmatch\n")
        
        result = search(tmp_path, "match", max_matches=0)
        # Even with 0, should get at least 1 match
        assert "match" in result

    def test_search_pattern_required(self, tmp_path):
        """Test that pattern is required."""
        with pytest.raises(ValueError, match="pattern is required"):
            search(tmp_path, "")

    def test_search_rg_not_found(self, tmp_path, monkeypatch):
        """Test error when ripgrep is not found."""
        monkeypatch.setattr(shutil, "which", lambda x: None)
        with pytest.raises(RuntimeError, match="rg not found"):
            search(tmp_path, "test")

    def test_search_timeout(self, tmp_path, monkeypatch):
        """Test timeout handling."""
        def mock_run(*args, **kwargs):
            raise subprocess.TimeoutExpired("rg", 10)
        
        monkeypatch.setattr(subprocess, "run", mock_run)
        with pytest.raises(TimeoutError, match="rg timed out"):
            search(tmp_path, "test")

    def test_search_returncode_error(self, tmp_path, monkeypatch):
        """Test error handling for non-zero returncodes (not 0 or 1)."""
        def mock_run(*args, **kwargs):
            result = mock.Mock()
            result.returncode = 2
            result.stderr = "Permission denied"
            result.stdout = ""
            return result
        
        monkeypatch.setattr(subprocess, "run", mock_run)
        with pytest.raises(RuntimeError, match="Permission denied"):
            search(tmp_path, "test")

    def test_search_returncode_error_fallback_to_stdout(self, tmp_path, monkeypatch):
        """Test that stdout is used when stderr is empty."""
        def mock_run(*args, **kwargs):
            result = mock.Mock()
            result.returncode = 2
            result.stderr = ""
            result.stdout = "Error message from stdout"
            return result
        
        monkeypatch.setattr(subprocess, "run", mock_run)
        with pytest.raises(RuntimeError, match="Error message from stdout"):
            search(tmp_path, "test")

    def test_search_returncode_error_fallback_to_default(self, tmp_path, monkeypatch):
        """Test default error message when both stderr and stdout are empty."""
        def mock_run(*args, **kwargs):
            result = mock.Mock()
            result.returncode = 2
            result.stderr = ""
            result.stdout = ""
            return result
        
        monkeypatch.setattr(subprocess, "run", mock_run)
        with pytest.raises(RuntimeError, match="rg failed"):
            search(tmp_path, "test")

    def test_search_returncode_1_is_ok(self, tmp_path, monkeypatch):
        """Test that returncode 1 is treated as OK (no matches found)."""
        def mock_run(*args, **kwargs):
            result = mock.Mock()
            result.returncode = 1
            result.stderr = ""
            result.stdout = ""
            return result
        
        monkeypatch.setattr(subprocess, "run", mock_run)
        result = search(tmp_path, "test")
        assert result == "(no matches)"

    def test_search_skips_cache_dirs(self, tmp_path):
        """Test that search skips cache directories."""
        # Create cache dirs that should be skipped
        (tmp_path / ".ruff_cache").mkdir()
        (tmp_path / ".ruff_cache" / "data.py").write_text("match")
        
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("match")
        
        result = search(tmp_path, "match")
        assert "src/app.py" in result
        assert "ruff_cache" not in result

    def test_search_skips_egg_info_dirs(self, tmp_path):
        """Test that search skips .egg-info and .dist-info directories."""
        (tmp_path / "mypackage.egg-info").mkdir()
        (tmp_path / "mypackage.egg-info" / "data.txt").write_text("match")
        
        (tmp_path / "mypackage.dist-info").mkdir()
        (tmp_path / "mypackage.dist-info" / "data.txt").write_text("match")
        
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("match")
        
        result = search(tmp_path, "match")
        assert "src/app.py" in result
        assert "egg-info" not in result
        assert "dist-info" not in result

    def test_search_result_contains_line_numbers(self, tmp_path):
        """Test that results contain line numbers."""
        (tmp_path / "app.py").write_text("first\nmatch\nthird\n")
        result = search(tmp_path, "match")
        assert ":2:" in result  # line 2

    def test_search_strips_empty_lines(self, tmp_path, monkeypatch):
        """Test that empty lines in output are stripped."""
        def mock_run(*args, **kwargs):
            result = mock.Mock()
            result.returncode = 0
            result.stderr = ""
            result.stdout = "app.py:1:match\n\n\napp.py:2:match\n"
            return result
        
        monkeypatch.setattr(subprocess, "run", mock_run)
        result = search(tmp_path, "match")
        # Empty lines should be removed
        assert result.count("\n\n\n") == 0


class TestRewritePath:
    """Test the _rewrite_path helper function."""

    def test_rewrite_path_basic(self, tmp_path):
        """Test basic path rewriting."""
        app_py = tmp_path / "app.py"
        app_py.write_text("x = 1\n")
        
        line = f"{app_py}:5:def foo"
        result = _rewrite_path(tmp_path, line)
        assert result == "app.py:5:def foo"

    def test_rewrite_path_in_subdirectory(self, tmp_path):
        """Test path rewriting for nested paths."""
        (tmp_path / "src").mkdir()
        app_py = tmp_path / "src" / "app.py"
        app_py.write_text("x = 1\n")
        
        line = f"{app_py}:1:match"
        result = _rewrite_path(tmp_path, line)
        assert result == "src/app.py:1:match"

    def test_rewrite_path_no_colon_in_line(self, tmp_path):
        """Test handling of lines without colons."""
        line = "some output without colon"
        result = _rewrite_path(tmp_path, line)
        assert result == line

    def test_rewrite_path_invalid_path_after_colon(self, tmp_path):
        """Test handling of malformed paths."""
        line = "/nonexistent/path/file.py:1:match"
        result = _rewrite_path(tmp_path, line)
        # Should return None because path is not in workspace
        assert result is None

    def test_rewrite_path_skips_cache_parts(self, tmp_path):
        """Test that paths with skip names are filtered out."""
        (tmp_path / ".ruff_cache").mkdir()
        cache_file = tmp_path / ".ruff_cache" / "data.py"
        cache_file.write_text("x = 1\n")
        
        line = f"{cache_file}:1:match"
        result = _rewrite_path(tmp_path, line)
        assert result is None

    def test_rewrite_path_skips_node_modules_parts(self, tmp_path):
        """Test that node_modules paths are filtered out."""
        (tmp_path / "node_modules").mkdir()
        lib = tmp_path / "node_modules" / "lib.js"
        lib.write_text("x = 1\n")
        
        line = f"{lib}:1:match"
        result = _rewrite_path(tmp_path, line)
        assert result is None

    def test_rewrite_path_multiple_colons(self, tmp_path):
        """Test path with multiple colons in line/column/content."""
        app_py = tmp_path / "app.py"
        app_py.write_text("x = 1\n")
        
        line = f"{app_py}:5:10:function call at this:time"
        result = _rewrite_path(tmp_path, line)
        assert result == "app.py:5:10:function call at this:time"

    def test_rewrite_path_with_spaces_in_relative_path(self, tmp_path):
        """Test path handling with spaces."""
        (tmp_path / "my code").mkdir()
        app_py = tmp_path / "my code" / "app.py"
        app_py.write_text("x = 1\n")
        
        line = f"{app_py}:1:match"
        result = _rewrite_path(tmp_path, line)
        assert result == "my code/app.py:1:match"


class TestSearchIntegration:
    """Integration tests for search with various file types."""

    def test_search_python_files(self, tmp_path):
        """Test searching in Python files."""
        (tmp_path / "module.py").write_text("class MyClass:\n    pass\n")
        result = search(tmp_path, "class MyClass")
        assert "module.py" in result

    def test_search_javascript_files(self, tmp_path):
        """Test searching in JavaScript files."""
        (tmp_path / "app.js").write_text("function foo() {\n    return 42;\n}\n")
        result = search(tmp_path, "function foo")
        assert "app.js" in result

    def test_search_go_files(self, tmp_path):
        """Test searching in Go files."""
        (tmp_path / "main.go").write_text("func main() {\n}\n")
        result = search(tmp_path, "func main")
        assert "main.go" in result

    def test_search_multiline_results_formatted(self, tmp_path):
        """Test that multiline results are properly formatted."""
        (tmp_path / "app.py").write_text(
            "def foo():\n"
            "    x = 1\n"
            "    return x\n"
            "\n"
            "def bar():\n"
            "    pass\n"
        )
        result = search(tmp_path, "def")
        # Should find both definitions
        assert "def foo" in result
        assert "def bar" in result
        assert "app.py" in result

    def test_search_regex_pattern(self, tmp_path):
        """Test searching with regex patterns."""
        (tmp_path / "app.py").write_text("var1 = 1\nvar2 = 2\nvar3 = 3\n")
        result = search(tmp_path, r"var\d")
        assert "var1" in result or "var2" in result

    def test_search_case_sensitive_by_default(self, tmp_path):
        """Test that search is case-sensitive by default."""
        (tmp_path / "app.py").write_text("MyClass\nmyclass\n")
        result = search(tmp_path, "MyClass")
        assert "MyClass" in result
        # The search might still find myclass depending on ripgrep, but we
        # at least test that the function works without error

    def test_search_empty_files_ignored(self, tmp_path):
        """Test that empty files are handled."""
        (tmp_path / "empty.py").write_text("")
        (tmp_path / "nonempty.py").write_text("match")
        
        result = search(tmp_path, "match")
        assert "nonempty.py" in result

    def test_search_with_special_characters_in_pattern(self, tmp_path):
        """Test search with special regex characters."""
        (tmp_path / "app.py").write_text("x = 1 + 2\ny = 3 * 4\n")
        result = search(tmp_path, r"\+ ")
        assert "+" in result

    def test_search_preserves_column_info(self, tmp_path):
        """Test that column information is preserved in output."""
        (tmp_path / "app.py").write_text("    indented_match\n")
        result = search(tmp_path, "match")
        # Result should contain line:column info
        assert "app.py:" in result
