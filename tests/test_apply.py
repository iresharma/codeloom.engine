from __future__ import annotations

import asyncio

from runtime.tools.edits import apply_edit, undo_last
from tests.conftest import seed


def test_staleness_refusal(ctx):
    seed(ctx, "a.py", "print(1)\n")
    (ctx.workspace / "a.py").write_text("print(2)\n")

    async def run():
        return await apply_edit(
            ctx, "a.py", lambda src: src.text.replace("print(1)", "print(3)"), "str_replace"
        )

    result = asyncio.run(run())
    assert result.startswith("error:")
    assert "changed on disk" in result
    assert (ctx.workspace / "a.py").read_text() == "print(2)\n"


def test_staleness_same_size_rewrite(ctx):
    seed(ctx, "a.py", "print(1)\n")
    (ctx.workspace / "a.py").write_text("print(9)\n")

    async def run():
        return await apply_edit(
            ctx, "a.py", lambda src: src.text.replace("print(1)", "print(8)"), "str_replace"
        )

    result = asyncio.run(run())
    assert "changed on disk" in result


def test_create_file_refuses_existing(ctx):
    seed(ctx, "a.py", "print(1)\n")

    async def run():
        return await apply_edit(
            ctx, "a.py", lambda src: "print(2)\n", "create_file", creating=True
        )

    result = asyncio.run(run())
    assert "already exists" in result
    assert (ctx.workspace / "a.py").read_text() == "print(1)\n"


def test_create_file_and_undo_prunes_dirs(ctx):
    async def run():
        created = await apply_edit(
            ctx, "pkg/sub/mod.py", lambda src: "x = 1\n", "create_file", creating=True
        )
        assert created.startswith("ok:")
        assert (ctx.workspace / "pkg/sub/mod.py").is_file()
        undone = await undo_last(ctx)
        assert undone.startswith("ok:")
        return undone

    asyncio.run(run())
    assert not (ctx.workspace / "pkg/sub/mod.py").exists()
    assert not (ctx.workspace / "pkg/sub").exists()
    assert not (ctx.workspace / "pkg").exists()


def test_undo_round_trip(ctx):
    seed(ctx, "a.py", "print(1)\n")

    async def run():
        edited = await apply_edit(
            ctx,
            "a.py",
            lambda src: src.text.replace("print(1)", "print(2)"),
            "str_replace",
        )
        assert edited.startswith("ok:")
        assert (ctx.workspace / "a.py").read_text() == "print(2)\n"
        undone = await undo_last(ctx)
        assert undone.startswith("ok:")

    asyncio.run(run())
    assert (ctx.workspace / "a.py").read_text() == "print(1)\n"


def test_must_read_before_edit(ctx):
    (ctx.workspace / "a.py").write_text("print(1)\n")

    async def run():
        return await apply_edit(
            ctx, "a.py", lambda src: src.text.replace("print(1)", "print(2)"), "str_replace"
        )

    result = asyncio.run(run())
    assert "read a.py before editing" in result
