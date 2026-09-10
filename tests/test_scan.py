from __future__ import annotations

from runtime.tools.scan import todo_scan


def test_todo_scan_hits_and_skips(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1  # TODO: fix later\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "lib.js").write_text("// TODO: ignored\n")
    (tmp_path / "venv").mkdir()
    (tmp_path / "venv" / "lib.py").write_text("# TODO: also ignored\n")
    text = todo_scan(tmp_path)
    assert "src/app.py:1:" in text
    assert "fix later" in text
    assert "node_modules" not in text
    assert "venv" not in text


def test_todo_scan_none(tmp_path):
    (tmp_path / "clean.py").write_text("print('hi')\n")
    assert todo_scan(tmp_path) == "(none)"
