from __future__ import annotations

from pathlib import Path

import pytest

from runtime.language import (
    SUPPORTED,
    LanguageInfo,
    _language_for,
    _pick,
    _root_markers,
    detect,
)


def test_supported_languages():
    """Test that supported languages are correctly defined."""
    assert SUPPORTED == ("python", "go", "javascript")


def test_language_info_label():
    """Test LanguageInfo label property."""
    info = LanguageInfo(
        name="python",
        supported=True,
        file_counts={"python": 5},
        warning=None,
    )
    assert info.label == "python"

    info = LanguageInfo(
        name="javascript",
        supported=True,
        file_counts={"javascript": 3},
        warning=None,
    )
    assert info.label == "javascript/typescript"

    info = LanguageInfo(
        name=None,
        supported=False,
        file_counts={},
        warning="unknown language",
    )
    assert info.label == "unknown"

    info = LanguageInfo(
        name="rust",
        supported=False,
        file_counts={"rust": 2},
        warning="rust not supported",
    )
    assert info.label == "rust"


def test_language_for_python_files():
    """Test detection of Python files."""
    assert _language_for("module.py") == "python"
    assert _language_for("types.pyi") == "python"
    assert _language_for("path/to/script.py") == "python"


def test_language_for_javascript_files():
    """Test detection of JavaScript files."""
    assert _language_for("index.js") == "javascript"
    assert _language_for("app.jsx") == "javascript"
    assert _language_for("module.ts") == "javascript"
    assert _language_for("types.tsx") == "javascript"
    assert _language_for("worker.mjs") == "javascript"
    assert _language_for("compat.cjs") == "javascript"
    assert _language_for("module.mts") == "javascript"
    assert _language_for("module.cts") == "javascript"


def test_language_for_go_files():
    """Test detection of Go files."""
    assert _language_for("main.go") == "go"
    assert _language_for("path/to/handler.go") == "go"


def test_language_for_other_extensions():
    """Test detection of other file extensions."""
    assert _language_for("file.rs") == "rust"
    assert _language_for("file.rb") == "ruby"
    assert _language_for("file.java") == "java"
    assert _language_for("file.swift") == "swift"
    assert _language_for("file.cpp") == "cpp"
    assert _language_for("file.cs") == "csharp"


def test_language_for_unknown_extensions():
    """Test that unknown extensions return None."""
    assert _language_for("file.txt") is None
    assert _language_for("file.md") is None
    assert _language_for("README") is None


def test_language_for_case_insensitive():
    """Test that extension detection is case-insensitive."""
    assert _language_for("module.PY") == "python"
    assert _language_for("module.Go") == "go"
    assert _language_for("module.JS") == "javascript"


def test_root_markers_empty_workspace(tmp_path):
    """Test marker detection in empty workspace."""
    markers = _root_markers(tmp_path)
    assert markers == set()


def test_root_markers_python_project(tmp_path):
    """Test detection of Python project markers."""
    (tmp_path / "pyproject.toml").touch()
    markers = _root_markers(tmp_path)
    assert markers == {"python"}


def test_root_markers_multiple_languages(tmp_path):
    """Test detection of multiple language markers."""
    (tmp_path / "pyproject.toml").touch()
    (tmp_path / "package.json").touch()
    (tmp_path / "go.mod").touch()
    markers = _root_markers(tmp_path)
    assert markers == {"python", "javascript", "go"}


def test_root_markers_non_existent_workspace():
    """Test marker detection in non-existent workspace."""
    markers = _root_markers(Path("/nonexistent/path"))
    assert markers == set()


def test_pick_with_single_marker():
    """Test _pick selects marked language when markers are unambiguous."""
    from collections import Counter

    counts = Counter({"python": 10, "javascript": 2})
    markers = {"python"}
    assert _pick(counts, markers) == "python"


def test_pick_with_single_marker_but_higher_count():
    """Test _pick overrides marker if file count is significantly higher."""
    from collections import Counter

    counts = Counter({"javascript": 20, "python": 5})
    markers = {"python"}
    # javascript has 20, python has 5, 20 >= 2 * 5, so javascript is chosen
    assert _pick(counts, markers) == "javascript"


def test_pick_with_multiple_markers():
    """Test _pick selects most common language with multiple markers."""
    from collections import Counter

    counts = Counter({"javascript": 15, "go": 5, "python": 3})
    markers = {"python", "go"}
    assert _pick(counts, markers) == "javascript"


def test_pick_no_counts_no_markers():
    """Test _pick returns None with no counts or markers."""
    from collections import Counter

    assert _pick(Counter(), set()) is None


def test_pick_no_counts_with_markers():
    """Test _pick returns minimum marker when no files found."""
    from collections import Counter

    markers = {"go", "python", "javascript"}
    result = _pick(Counter(), markers)
    assert result in markers


def test_pick_with_counts_no_markers():
    """Test _pick returns most common when no markers."""
    from collections import Counter

    counts = Counter({"go": 20, "python": 5})
    assert _pick(counts, set()) == "go"


def test_detect_python_project(tmp_path):
    """Test language detection for Python project."""
    (tmp_path / "pyproject.toml").touch()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hello')")

    info = detect(tmp_path)
    assert info.name == "python"
    assert info.supported is True
    assert info.warning is None
    assert "python" in info.file_counts


def test_detect_javascript_project(tmp_path):
    """Test language detection for JavaScript project."""
    (tmp_path / "package.json").touch()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "index.ts").write_text("console.log('hello')")

    info = detect(tmp_path)
    assert info.name == "javascript"
    assert info.supported is True
    assert info.warning is None


def test_detect_go_project(tmp_path):
    """Test language detection for Go project."""
    (tmp_path / "go.mod").write_text("module example.com")
    (tmp_path / "main.go").write_text("package main")

    info = detect(tmp_path)
    assert info.name == "go"
    assert info.supported is True
    assert info.warning is None


def test_detect_unsupported_language(tmp_path):
    """Test detection of unsupported language."""
    (tmp_path / "Gemfile").touch()
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "main.rb").write_text("puts 'hello'")

    info = detect(tmp_path)
    assert info.name == "ruby"
    assert info.supported is False
    assert "not available for this project" in info.warning
    assert "ruby" in info.warning


def test_detect_unknown_language(tmp_path):
    """Test detection when no language markers or files found."""
    (tmp_path / "README.md").write_text("# Project")

    info = detect(tmp_path)
    assert info.name is None
    assert info.supported is False
    assert "could not detect" in info.warning


def test_detect_relative_path_resolution(tmp_path):
    """Test that relative paths are resolved correctly."""
    (tmp_path / "pyproject.toml").touch()
    (tmp_path / "script.py").write_text("pass")

    # Create a relative path and convert to Path object
    import os

    original_cwd = os.getcwd()
    try:
        os.chdir(tmp_path.parent)
        info = detect(Path(tmp_path.name))
        assert info.name == "python"
        assert info.supported is True
    finally:
        os.chdir(original_cwd)


def test_detect_respects_git_tracked_files(tmp_path):
    """Test detection respects git tracked files when available."""
    import subprocess

    # Initialize git repo
    subprocess.run(
        ["git", "init"],
        cwd=tmp_path,
        capture_output=True,
        check=False,
    )

    (tmp_path / "pyproject.toml").touch()
    (tmp_path / "main.py").write_text("pass")

    # Stage files
    subprocess.run(
        ["git", "add", "."],
        cwd=tmp_path,
        capture_output=True,
        check=False,
    )

    info = detect(tmp_path)
    assert info.name == "python"
    assert info.supported is True


def test_file_counts_populated(tmp_path):
    """Test that file_counts dict is populated correctly."""
    (tmp_path / "pyproject.toml").touch()
    (tmp_path / "a.py").write_text("pass")
    (tmp_path / "b.py").write_text("pass")
    (tmp_path / "index.js").write_text("console.log('hi')")

    info = detect(tmp_path)
    assert info.file_counts.get("python", 0) >= 2
    assert info.file_counts.get("javascript", 0) >= 1
