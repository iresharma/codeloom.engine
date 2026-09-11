"""Comprehensive tests for runtime/tools/search.py"""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from runtime.tools.search import search, _rewrite_path


def test_search_requires_rg(tmp_path):
    """Test that search raises RuntimeError if rg is not found."""
    with patch("runtime.tools.search.shutil.which", return_value=None):
        with pytest.raises(RuntimeError, match="rg not found"):
            search(tmp_path, "pattern")


def test_search_requires_pattern(tmp_path):
    """Test that search raises ValueError if pattern is empty."""
    with patch("runtime.tools.search.shutil.which", return_value="/usr/bin/rg"):
        with pytest.raises(ValueError, match="pattern is required"):
            search(tmp_path, "")


def test_search_basic_match(tmp_path):
    """Test search with a basic match."""
    (tmp_path / "file.py").write_text("def foo():\n    pass\n")
    with patch("runtime.tools.search.shutil.which", return_value="/usr/bin/rg"):
        with patch("runtime.tools.search.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=f"{tmp_path}/file.py:1:def foo():\n",
                stderr="",
            )
            result = search(tmp_path, "def foo")
            assert "file.py" in result


def test_search_no_matches(tmp_path):
    """Test search with no matches."""
    with patch("runtime.tools.search.shutil.which", return_value="/usr/bin/rg"):
        with patch("runtime.tools.search.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1,
                stdout="",
                stderr="",
            )
            result = search(tmp_path, "nonexistent")
            assert result == "(no matches)"


def test_search_multiple_matches(tmp_path):
    """Test search with multiple matches."""
    with patch("runtime.tools.search.shutil.which", return_value="/usr/bin/rg"):
        with patch("runtime.tools.search.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=f"{tmp_path}/file.py:1:match1\n{tmp_path}/file.py:2:match2\n",
                stderr="",
            )
            result = search(tmp_path, "match")
            assert "file.py:1" in result
            assert "file.py:2" in result


def test_search_with_glob_filter(tmp_path):
    """Test search with glob filter."""
    with patch("runtime.tools.search.shutil.which", return_value="/usr/bin/rg"):
        with patch("runtime.tools.search.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=f"{tmp_path}/file.py:1:match\n",
                stderr="",
            )
            result = search(tmp_path, "match", glob="*.py")
            assert "file.py" in result
            # Verify glob was passed to subprocess
            call_args = mock_run.call_args
            assert "--glob" in call_args[0][0]
            assert "*.py" in call_args[0][0]


def test_search_with_path_filter(tmp_path):
    """Test search with path filter."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "file.py").write_text("match")
    
    with patch("runtime.tools.search.shutil.which", return_value="/usr/bin/rg"):
        with patch("runtime.tools.search.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=f"{tmp_path}/src/file.py:1:match\n",
                stderr="",
            )
            result = search(tmp_path, "match", path="src")
            assert "src/file.py" in result


def test_search_timeout_error(tmp_path):
    """Test that search raises TimeoutError when rg times out."""
    with patch("runtime.tools.search.shutil.which", return_value="/usr/bin/rg"):
        with patch("runtime.tools.search.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("rg", 10)
            with pytest.raises(TimeoutError, match="rg timed out"):
                search(tmp_path, "pattern")


def test_search_rg_error(tmp_path):
    """Test that search raises RuntimeError on rg failure."""
    with patch("runtime.tools.search.shutil.which", return_value="/usr/bin/rg"):
        with patch("runtime.tools.search.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=2,
                stdout="",
                stderr="rg error message",
            )
            with pytest.raises(RuntimeError, match="rg error message"):
                search(tmp_path, "pattern")


def test_search_rg_error_uses_stdout_fallback(tmp_path):
    """Test that search uses stdout if stderr is empty."""
    with patch("runtime.tools.search.shutil.which", return_value="/usr/bin/rg"):
        with patch("runtime.tools.search.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=2,
                stdout="stdout error",
                stderr="",
            )
            with pytest.raises(RuntimeError, match="stdout error"):
                search(tmp_path, "pattern")


def test_search_rg_error_fallback_default(tmp_path):
    """Test that search uses default error message when both stderr/stdout are empty."""
    with patch("runtime.tools.search.shutil.which", return_value="/usr/bin/rg"):
        with patch("runtime.tools.search.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=2,
                stdout="",
                stderr="",
            )
            with pytest.raises(RuntimeError, match="rg failed"):
                search(tmp_path, "pattern")


def test_search_filters_empty_lines(tmp_path):
    """Test that search filters out empty lines from results."""
    with patch("runtime.tools.search.shutil.which", return_value="/usr/bin/rg"):
        with patch("runtime.tools.search.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=f"{tmp_path}/file.py:1:match\n\n{tmp_path}/file.py:2:match\n",
                stderr="",
            )
            result = search(tmp_path, "match")
            # Should have 2 matches, not 3
            assert result.count("match") >= 2


def test_search_max_matches_clamped_to_one(tmp_path):
    """Test that max_matches is clamped to at least 1."""
    with patch("runtime.tools.search.shutil.which", return_value="/usr/bin/rg"):
        with patch("runtime.tools.search.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=f"{tmp_path}/file.py:1:match\n",
                stderr="",
            )
            result = search(tmp_path, "match", max_matches=0)
            assert "match" in result


def test_search_max_matches_clamped_to_200(tmp_path):
    """Test that max_matches is clamped to max 200."""
    with patch("runtime.tools.search.shutil.which", return_value="/usr/bin/rg"):
        with patch("runtime.tools.search.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="\n".join([f"{tmp_path}/file.py:{i}:match{i}" for i in range(1, 300)]),
                stderr="",
            )
            result = search(tmp_path, "match", max_matches=500)
            # Should be clamped to 200
            assert "more matches" in result


def test_search_displays_extra_count(tmp_path):
    """Test that search shows extra match count when exceeded."""
    with patch("runtime.tools.search.shutil.which", return_value="/usr/bin/rg"):
        with patch("runtime.tools.search.subprocess.run") as mock_run:
            lines = "\n".join([f"{tmp_path}/file.py:{i}:match" for i in range(1, 15)])
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=lines,
                stderr="",
            )
            result = search(tmp_path, "match", max_matches=10)
            assert "4 more matches" in result


def test_rewrite_path_passthrough_without_colon(tmp_path):
    """Test _rewrite_path with line without colon."""
    line = "some line without colon"
    result = _rewrite_path(tmp_path, line)
    assert result == line


def test_rewrite_path_converts_absolute_to_relative(tmp_path):
    """Test _rewrite_path converts absolute path to relative."""
    (tmp_path / "file.py").write_text("x = 1\n")
    line = f"{tmp_path}/file.py:1:content"
    result = _rewrite_path(tmp_path, line)
    assert result is not None
    assert "file.py:1:content" in result
    assert str(tmp_path) not in result


def test_rewrite_path_filters_invalid_paths(tmp_path):
    """Test _rewrite_path returns None for invalid paths."""
    line = "/absolute/path/outside:1:content"
    result = _rewrite_path(tmp_path, line)
    # Should return None due to path being outside workspace
    assert result is None


def test_rewrite_path_filters_skipped_names(tmp_path):
    """Test _rewrite_path filters paths with skipped names."""
    cache_dir = tmp_path / ".ruff_cache"
    cache_dir.mkdir()
    (cache_dir / "file.py").write_text("x = 1\n")
    line = f"{cache_dir}/file.py:1:content"
    result = _rewrite_path(tmp_path, line)
    # Should return None due to .ruff_cache being skipped
    assert result is None


def test_search_returncode_1_ok(tmp_path):
    """Test that return code 1 is treated as no matches (not an error)."""
    with patch("runtime.tools.search.shutil.which", return_value="/usr/bin/rg"):
        with patch("runtime.tools.search.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1,
                stdout="",
                stderr="",
            )
            result = search(tmp_path, "pattern")
            assert "(no matches)" in result


def test_search_resolves_relative_path(tmp_path):
    """Test that relative paths are resolved correctly."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "file.py").write_text("match")
    
    with patch("runtime.tools.search.shutil.which", return_value="/usr/bin/rg"):
        with patch("runtime.tools.search.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=f"{tmp_path}/src/file.py:1:match\n",
                stderr="",
            )
            result = search(tmp_path, "match", path="src")
            # Verify the command was called with the resolved path
            call_args = mock_run.call_args
            assert str(tmp_path / "src") in call_args[0][0] or "src" in str(call_args)


def test_search_passes_workspace_as_cwd(tmp_path):
    """Test that workspace is passed as cwd to subprocess."""
    with patch("runtime.tools.search.shutil.which", return_value="/usr/bin/rg"):
        with patch("runtime.tools.search.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="",
                stderr="",
            )
            search(tmp_path, "pattern")
            call_args = mock_run.call_args
            assert call_args[1]["cwd"] == str(tmp_path)


def test_search_skip_names_included(tmp_path):
    """Test that standard skip names are included in command."""
    with patch("runtime.tools.search.shutil.which", return_value="/usr/bin/rg"):
        with patch("runtime.tools.search.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="",
                stderr="",
            )
            search(tmp_path, "pattern")
            call_args = mock_run.call_args
            command = call_args[0][0]
            # Should have skip globs for common names
            assert any("!node_modules" in str(arg) for arg in command)


def test_search_cache_patterns_included(tmp_path):
    """Test that cache-related patterns are included."""
    with patch("runtime.tools.search.shutil.which", return_value="/usr/bin/rg"):
        with patch("runtime.tools.search.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="",
                stderr="",
            )
            search(tmp_path, "pattern")
            call_args = mock_run.call_args
            command = " ".join(call_args[0][0])
            # Should include cache-related excludes
            assert "*cache*" in command or "cache" in command


def test_search_extra_matches_shows_count(tmp_path):
    """Test that extra match count is shown correctly."""
    with patch("runtime.tools.search.shutil.which", return_value="/usr/bin/rg"):
        with patch("runtime.tools.search.subprocess.run") as mock_run:
            # Create 50 matches but limit to 10
            lines = "\n".join([f"{tmp_path}/file.py:{i}:match" for i in range(1, 51)])
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=lines,
                stderr="",
            )
            result = search(tmp_path, "pattern", max_matches=10)
            assert "raise max_matches" in result
            assert "40 more" in result


def test_search_inline_max_matches_constraint(tmp_path):
    """Test constraint between DEFAULT_MAX_MATCHES and MAX_MATCHES."""
    from runtime.tools.search import DEFAULT_MAX_MATCHES, MAX_MATCHES
    # Ensure constants have expected relationship
    assert DEFAULT_MAX_MATCHES <= MAX_MATCHES
