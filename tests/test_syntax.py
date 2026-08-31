from __future__ import annotations

import asyncio

from runtime.tools.edits import apply_edit
from runtime.tools.sitter import check_syntax, language_for, syntax_gate
from tests.conftest import seed


def test_syntax_gate_rejects_truncated_function():
    err = syntax_gate("a.py", "def foo(\n", "def foo():\n    return 1\n")
    assert err is not None
    assert "syntax gate" in err


def test_syntax_gate_allows_edit_to_already_broken_file():
    broken = "def good():\n    return 1\n\ndef bad(\n"
    edited = "def good():\n    return 2\n\ndef bad(\n"
    assert language_for("a.py") == "python"
    pre = check_syntax("python", broken.encode())
    post = check_syntax("python", edited.encode())
    assert pre
    assert len(post) <= len(pre)
    err = syntax_gate("a.py", edited, broken)
    assert err is None


def test_syntax_gate_absolute_for_new_file():
    err = syntax_gate("a.py", "def foo(\n", None)
    assert err is not None


def test_apply_rejects_broken_new_file(ctx):
    async def run():
        return await apply_edit(
            ctx, "a.py", lambda src: "def foo(\n", "create_file", creating=True
        )

    result = asyncio.run(run())
    assert "syntax gate" in result
    assert not (ctx.workspace / "a.py").exists()


def test_apply_rejects_broken_edit(ctx):
    seed(ctx, "a.py", "def foo():\n    return 1\n")

    async def run():
        return await apply_edit(ctx, "a.py", lambda src: "def foo(\n", "str_replace")

    result = asyncio.run(run())
    assert "syntax gate" in result
    assert (ctx.workspace / "a.py").read_text() == "def foo():\n    return 1\n"
