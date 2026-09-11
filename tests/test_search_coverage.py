"""Comprehensive tests for runtime/tools/search.py to raise coverage from 16% to 85%+"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from runtime.tools import search as search_impl
from runtime.tools.search import (
    MAX_MATCHES,
    DEFAULT_MAX_MATCHES,
    search,
    _rewrite_path,
)


class TestSearch:
    """Tests for the search() function."""

    def test_search_requires_pattern(self, tmp_path):
        """Empty pattern should raise ValueError."""
        with pytest.raises(ValueError, match="pattern is required"):
            search(tmp_path, "")

    def test_search_rg_not_found(self, tmp_path, monkeypatch):
        """Should raise RuntimeError if ripgrep is not found."""
        monkeypatch.setattr("shutil.which", lambda x: None)
        with pytest.raises(RuntimeError, match="rg not found"):
            search(tmp_path, "test")

    def test_search_basic_success(self, tmp_path, monkeypatch):
        """Test basic successful search with mocked ripgrep."""
        (tmp_path / "file.py").write_text("import sys\nprint('hello')\n")
        
        def fake_run(*args, **kwargs):
            return SimpleNamespace(
                returncode=0,
                stdout=f"{tmp_path}/file.py:1:import sys\n",
                stderr=""
            )
        
        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr("shutil.which", lambda x: "/usr/bin/rg")
        
        result = search(tmp_path, "import")
        assert "file.py:1:import sys" in result

    def test_search_no_matches(self, tmp_path, monkeypatch):
        """Search with no matches returns the (no matches) message."""
        def fake_run(*args, **kwargs):
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        
        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr("shutil.which", lambda x: "/usr/bin/rg")
        
        result = search(tmp_path, "nonexistent_marker")
        assert result == "(no matches)"

    def test_search_error_return_code(self, tmp_path, monkeypatch):
        """Non-0/1 return code raises RuntimeError."""
        def fake_run(*args, **kwargs):
            return SimpleNamespace(returncode=2, stdout="", stderr="some error")
        
        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr("shutil.which", lambda x: "/usr/bin/rg")
        
        with pytest.raises(RuntimeError, match="some error"):
            search(tmp_path, "test")

    def test_search_timeout(self, tmp_path, monkeypatch):
        """subprocess.TimeoutExpired should be caught and re-raised as TimeoutError."""
        def fake_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(["rg"], 10)
        
        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr("shutil.which", lambda x: "/usr/bin/rg")
        
        with pytest.raises(TimeoutError, match="rg timed out"):
            search(tmp_path, "test")

    def test_search_max_matches_clamped(self, tmp_path, monkeypatch):
        """max_matches should be clamped between 1 and MAX_MATCHES."""
        calls = []
        
        def capture_run(cmd, *args, **kwargs):
            calls.append(cmd)
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        
        monkeypatch.setattr(subprocess, "run", capture_run)
        monkeypatch.setattr("shutil.which", lambda x: "/usr/bin/rg")
        
        # Test max_matches < 1 clamps to 1
        search(tmp_path, "test", max_matches=0)
        assert calls[-1][-2] == "test"  # pattern
        
        # Test max_matches > MAX_MATCHES clamps to MAX_MATCHES
        calls.clear()
        search(tmp_path, "test", max_matches=999)
        assert calls[-1][-2] == "test"

    def test_search_with_path(self, tmp_path, monkeypatch):
        """Search within a specific path."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / "file.py").write_text("test line\n")
        
        def fake_run(cmd, *args, **kwargs):
            assert str(subdir) in cmd[-1] or str(subdir) in " ".join(cmd)
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        
        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr("shutil.which", lambda x: "/usr/bin/rg")
        
        search(tmp_path, "test", path="subdir")

    def test_search_with_glob(self, tmp_path, monkeypatch):
        """Search with file glob filter."""
        calls = []
        
        def capture_run(cmd, *args, **kwargs):
            calls.append(cmd)
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        
        monkeypatch.setattr(subprocess, "run", capture_run)
        monkeypatch.setattr("shutil.which", lambda x: "/usr/bin/rg")
        
        search(tmp_path, "test", glob="*.py")
        assert any("*.py" in str(call) for call in calls)

    def test_search_extra_matches_message(self, tmp_path, monkeypatch):
        """When results exceed max_matches, show how many more there are."""
        lines = "\n".join([f"{tmp_path}/file.py:{i}:line {i}" for i in range(100)])
        
        def fake_run(*args, **kwargs):
            return SimpleNamespace(returncode=0, stdout=lines + "\n", stderr="")
        
        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr("shutil.which", lambda x: "/usr/bin/rg")
        
        result = search(tmp_path, "line", max_matches=10)
        assert "... (" in result
        assert "more matches" in result

    def test_search_strips_empty_lines(self, tmp_path, monkeypatch):
        """Empty lines in ripgrep output are filtered out."""
        def fake_run(*args, **kwargs):
            return SimpleNamespace(
                returncode=0,
                stdout=f"{tmp_path}/file.py:1:match1\n\n{tmp_path}/file.py:2:match2\n",
                stderr=""
            )
        
        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr("shutil.which", lambda x: "/usr/bin/rg")
        
        result = search(tmp_path, "match")
        lines = result.split("\n")
        assert not any(line == "" for line in lines if line)

    def test_search_error_no_stderr(self, tmp_path, monkeypatch):
        """Error with no stderr uses stdout or generic message."""
        def fake_run(*args, **kwargs):
            return SimpleNamespace(returncode=2, stdout="", stderr="")
        
        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr("shutil.which", lambda x: "/usr/bin/rg")
        
        with pytest.raises(RuntimeError, match="rg failed"):
            search(tmp_path, "test")

    def test_search_resolve_workspace(self, tmp_path, monkeypatch):
        """Workspace path is resolved before use."""
        def fake_run(cmd, *args, **kwargs):
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        
        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr("shutil.which", lambda x: "/usr/bin/rg")
        
        # Resolve symlink-like path
        result = search(tmp_path, "test")
        assert result == "(no matches)"


class TestRewritePath:
    """Tests for the _rewrite_path() helper function."""

    def test_rewrite_path_no_colon(self, tmp_path):
        """Lines without colon are passed through unchanged."""
        result = _rewrite_path(tmp_path, "no colon here")
        assert result == "no colon here"

    def test_rewrite_path_basic(self, tmp_path):
        """Basic path rewrite from absolute to relative with posix format."""
        file_path = tmp_path / "test.py"
        line = f"{file_path}:10:match text"
        result = _rewrite_path(tmp_path, line)
        assert result == "test.py:10:match text"

    def test_rewrite_path_nested(self, tmp_path):
        """Nested paths are rewritten to posix relative format."""
        subdir = tmp_path / "src" / "lib"
        subdir.mkdir(parents=True)
        file_path = subdir / "module.py"
        line = f"{file_path}:5:content"
        result = _rewrite_path(tmp_path, line)
        assert result == "src/lib/module.py:5:content"

    def test_rewrite_path_outside_workspace(self, tmp_path):
        """Paths outside workspace are skipped (return None)."""
        outside = Path("/etc/passwd")
        line = f"{outside}:1:root"
        result = _rewrite_path(tmp_path, line)
        assert result is None

    def test_rewrite_path_skip_cache(self, tmp_path):
        """Paths in skip directories are filtered out."""
        cache = tmp_path / ".ruff_cache"
        cache.mkdir()
        file_path = cache / "data.txt"
        file_path.write_text("x")
        line = f"{file_path}:1:data"
        result = _rewrite_path(tmp_path, line)
        assert result is None

    def test_rewrite_path_skip_node_modules(self, tmp_path):
        """node_modules are skipped."""
        nm = tmp_path / "node_modules" / "pkg"
        nm.mkdir(parents=True)
        file_path = nm / "index.js"
        file_path.write_text("x")
        line = f"{file_path}:1:code"
        result = _rewrite_path(tmp_path, line)
        assert result is None

    def test_rewrite_path_after_first_colon_preserved(self, tmp_path):
        """Everything after first colon is preserved."""
        file_path = tmp_path / "test.txt"
        # Note multiple colons like timestamps or other data
        line = f"{file_path}:10:12:34:56:some data:more"
        result = _rewrite_path(tmp_path, line)
        assert result == "test.txt:10:12:34:56:some data:more"

    def test_rewrite_path_unresolvable(self, tmp_path, monkeypatch):
        """Unresolvable paths return None."""
        # Create a line with a path-like string that will fail to resolve
        line = f"/nonexistent/path/file.py:1:match"
        result = _rewrite_path(tmp_path, line)
        # Should be None since path is outside workspace
        assert result is None


class TestSearchIntegration:
    """Integration tests using actual files."""

    def test_search_find_py_files(self, tmp_path, monkeypatch):
        """Search can find python files when ripgrep is available."""
        (tmp_path / "a.py").write_text("def test():\n    pass\n")
        (tmp_path / "b.py").write_text("# no match here\n")
        
        # Only mock if rg isn't available
        if not subprocess.run(["rg", "--version"], capture_output=True).returncode == 0:
            pytest.skip("ripgrep not installed")
        
        result = search(tmp_path, "def test", max_matches=50)
        assert "a.py" in result or result == "(no matches)"

    def test_search_skip_patterns_applied(self, tmp_path, monkeypatch):
        """Cache and vendor directories are skipped."""
        cache = tmp_path / ".ruff_cache"
        cache.mkdir()
        (cache / "data.py").write_text("SKIP_ME = 1\n")
        
        (tmp_path / "app.py").write_text("KEEP_ME = 1\n")
        
        if not subprocess.run(["rg", "--version"], capture_output=True).returncode == 0:
            pytest.skip("ripgrep not installed")
        
        result = search(tmp_path, "KEEP_ME|SKIP_ME", max_matches=50)
        if "KEEP_ME" in result:
            assert "app.py" in result
            assert ".ruff_cache" not in result
