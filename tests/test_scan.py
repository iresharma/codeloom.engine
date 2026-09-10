from __future__ import annotations

from runtime.tools.scan import todo_scan


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
