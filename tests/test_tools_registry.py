from __future__ import annotations

import asyncio

from runtime.tools.edits import TextEdit, apply_workspace_edit
from tools.registry import discover_tools


def test_discover_write_tools():
    registry = discover_tools()
    names = set(registry._tools)
    expected = {
        "str_replace",
        "replace_lines",
        "insert_at_line",
        "create_file",
        "replace_symbol",
        "insert_after_imports",
        "apply_patch",
        "rename_symbol",
        "undo_edit",
        "list_edits",
    }
    assert expected <= names
    assert not registry.errors


def test_workspace_edit_without_prior_read(ctx):
    (ctx.workspace / "a.py").write_text("alpha = 1\nbeta = 1\n")
    edits = [
        (
            "a.py",
            [
                TextEdit(
                    start_line=0, start_char=0, end_line=0, end_char=5, new_text="gamma"
                )
            ],
        )
    ]

    async def run():
        return await apply_workspace_edit(ctx, edits, "rename_symbol")

    result = asyncio.run(run())
    assert result.startswith("ok:")
    assert (ctx.workspace / "a.py").read_text().startswith("gamma")
