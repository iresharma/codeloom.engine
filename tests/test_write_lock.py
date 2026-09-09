from __future__ import annotations

import asyncio

from runtime.tools import edits as edits_mod
from runtime.tools.edits import apply_edit
from tests.conftest import seed


def test_write_lock_blocks_second_sync(ctx):
    order: list[str] = []
    orig = edits_mod._apply_sync
    orig_thread = edits_mod.asyncio.to_thread

    def wrapped(*args, **kwargs):
        rel = args[1] if len(args) > 1 else kwargs.get("path")
        order.append(f"sync:{rel}")
        return orig(*args, **kwargs)

    class FakeLsp:
        def cached_diagnostics(self, path):
            return []

    ctx.lsp = FakeLsp()
    edits_mod._apply_sync = wrapped

    async def run():
        first_in_lsp = asyncio.Event()
        release = asyncio.Event()

        async def slow_thread(fn, *args):
            first_in_lsp.set()
            await release.wait()
            return ""

        edits_mod.asyncio.to_thread = slow_thread
        ctx.write_lock = asyncio.Lock()
        seed(ctx, "a.py", "a = 1\n")
        seed(ctx, "b.py", "b = 1\n")
        first = asyncio.create_task(
            apply_edit(ctx, "a.py", lambda s: "a = 2\n", "str_replace")
        )
        await first_in_lsp.wait()
        second = asyncio.create_task(
            apply_edit(ctx, "b.py", lambda s: "b = 2\n", "str_replace")
        )
        await asyncio.sleep(0.05)
        assert order == ["sync:a.py"]
        release.set()
        await asyncio.gather(first, second)
        assert order == ["sync:a.py", "sync:b.py"]

    try:
        asyncio.run(run())
    finally:
        edits_mod._apply_sync = orig
        edits_mod.asyncio.to_thread = orig_thread
