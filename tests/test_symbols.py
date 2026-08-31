from __future__ import annotations

import asyncio

from runtime.tools.edits import apply_edit
from runtime.tools.sitter import insert_after_imports_in_text, replace_symbol_in_text
from tests.conftest import seed


def test_replace_symbol_in_text():
    src = "def foo():\n    return 1\n\ndef bar():\n    return 2\n"
    out = replace_symbol_in_text("a.py", src, "foo", "def foo():\n    return 9\n")
    assert "return 9" in out
    assert "def bar" in out


def test_insert_after_imports():
    src = "import os\nimport sys\n\ndef foo():\n    return 1\n"
    out = insert_after_imports_in_text("a.py", src, "from pathlib import Path\n")
    assert out.index("pathlib") < out.index("def foo")


def test_replace_symbol_via_funnel(ctx):
    seed(ctx, "a.py", "def foo():\n    return 1\n")

    async def run():
        return await apply_edit(
            ctx,
            "a.py",
            lambda src: replace_symbol_in_text(
                src.rel, src.text, "foo", "def foo():\n    return 2\n"
            ),
            "replace_symbol",
        )

    result = asyncio.run(run())
    assert result.startswith("ok:")
    assert "return 2" in (ctx.workspace / "a.py").read_text()
