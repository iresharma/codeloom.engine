from __future__ import annotations

from runtime.tools.scan import todo_scan
from pathlib import Path
from unittest.mock import Mock
from runtime.tools.dep_why import dep_why
from runtime.tools.envinfo import runtime_info


def test_todo_scan_hits_and_skips(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1  # TODO: fix later\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "lib.js").write_text("// TODO: ignored\n")
    (tmp_path / "venv").mkdir()
    (tmp_path / "venv" / "lib.py").write_text("# TODO: also ignored\n")
    ruff = tmp_path / ".ruff_cache"
    ruff.mkdir()
    (ruff / "c12").mkdir()
    (ruff / "c12" / "data").write_text("# TODO: cache junk\n")
    text = todo_scan(tmp_path)
    assert "src/app.py:1:" in text
    assert "fix later" in text
    assert "node_modules" not in text
    assert "venv" not in text
    assert "ruff_cache" not in text


def test_todo_scan_none(tmp_path):
    (tmp_path / "clean.py").write_text("print('hi')\n")
    assert todo_scan(tmp_path) == "(none)"


def test_should_skip_cache_names():
    from runtime.tools.fs import should_skip_name

    assert should_skip_name(".ruff_cache")
    assert should_skip_name("ruff_cache")
    assert should_skip_name(".mypy_cache")
    assert should_skip_name(".foo_cache")
    assert should_skip_name("pkg.egg-info")
    assert not should_skip_name("cache.py")
    assert not should_skip_name("cache")
    assert not should_skip_name("src")


def test_list_tree_skips_ruff_cache(tmp_path):
    from runtime.tools.fs import list_tree

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n")
    cache = tmp_path / ".ruff_cache" / "c12"
    cache.mkdir(parents=True)
    (cache / "data").write_text("junk\n")
    paths = []

    def collect(nodes):
        for node in nodes:
            if node.is_dir:
                collect(node.children or [])
            else:
                paths.append(node.path)

    collect(list_tree(tmp_path))
    assert "src/app.py" in paths
    assert not any("ruff_cache" in path for path in paths)


# ============================================================================
# Tests for runtime/tools/envinfo.py
# ============================================================================

def test_runtime_info_found_tools(monkeypatch):
    """Test runtime_info with found tools."""
    def mock_which(tool):
        if tool == "python3":
            return "/usr/bin/python3"
        if tool == "git":
            return "/usr/bin/git"
        return None

    def mock_run(*args, **kwargs):
        result = Mock()
        if "python" in str(args):
            result.stdout = "Python 3.11.0"
            result.stderr = ""
        elif "git" in str(args):
            result.stdout = "git version 2.40.0"
            result.stderr = ""
        return result

    monkeypatch.setattr("runtime.tools.envinfo.shutil.which", mock_which)
    monkeypatch.setattr("runtime.tools.envinfo.subprocess.run", mock_run)
    
    result = runtime_info()
    assert "python:" in result.lower() or "git:" in result.lower()


def test_runtime_info_no_tools(monkeypatch):
    """Test runtime_info when no tools found."""
    monkeypatch.setattr("runtime.tools.envinfo.shutil.which", lambda x: None)
    result = runtime_info()
    assert "(not found)" in result


# ============================================================================
# Tests for runtime/tools/dep_why.py
# ============================================================================

def test_dep_why_invalid_ecosystem():
    """Test dep_why with invalid ecosystem."""
    result = dep_why(Path.cwd(), "bogus", "package")
    assert "error:" in result
    assert "ecosystem" in result


def test_dep_why_no_name():
    """Test dep_why with no package name."""
    result = dep_why(Path.cwd(), "npm", "")
    assert "error:" in result
    assert "required" in result


def test_dep_why_binary_not_found(monkeypatch):
    """Test dep_why when binary not found."""
    monkeypatch.setattr("runtime.tools.dep_why.shutil.which", lambda x: None)
    result = dep_why(Path.cwd(), "npm", "lodash")
    assert "error:" in result
    assert "not installed" in result


# ============================================================================
# Tests for runtime/tools/scan.py
# ============================================================================

def test_todo_scan_default(tmp_path):
    """Test todo_scan with default pattern."""
    test_file = tmp_path / "test.py"
    test_file.write_text("# TODO: fix this\ndef foo():\n    pass")
    
    result = todo_scan(tmp_path)
    assert "TODO" in result
    assert "fix this" in result


def test_todo_scan_invalid_pattern(tmp_path):
    """Test todo_scan with invalid regex pattern."""
    result = todo_scan(tmp_path, pattern="[invalid")
    assert "error:" in result


def test_todo_scan_custom_pattern(tmp_path):
    """Test todo_scan with custom pattern."""
    test_file = tmp_path / "test.py"
    test_file.write_text("# CUSTOM: something\n# TODO: other")
    
    result = todo_scan(tmp_path, pattern="CUSTOM")
    assert "CUSTOM" in result


def test_todo_scan_limit(tmp_path):
    """Test todo_scan respects limit."""
    for i in range(100):
        f = tmp_path / f"test{i}.py"
        f.write_text(f"# TODO item {i}")
    
    result = todo_scan(tmp_path, limit=5)
    lines = result.strip().split("\n")
    assert len(lines) <= 6  # 5 items + truncation marker


def test_todo_scan_nonexistent_path(tmp_path):
    """Test todo_scan with nonexistent path."""
    result = todo_scan(tmp_path, path="nonexistent")
    assert "error:" in result
