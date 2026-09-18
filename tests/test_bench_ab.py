from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load():
    path = Path(__file__).resolve().parents[1] / "scripts" / "bench_ab.py"
    spec = importlib.util.spec_from_file_location("bench_ab", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_require_empty_accepts_missing_and_blank(tmp_path):
    mod = _load()
    missing = tmp_path / "fresh"
    mod._require_empty(missing)
    assert missing.is_dir()
    mod._require_empty(missing)


def test_require_empty_rejects_files(tmp_path):
    mod = _load()
    (tmp_path / "leftover").write_text("x\n")
    with pytest.raises(mod.BenchError, match="not empty"):
        mod._require_empty(tmp_path)
