from __future__ import annotations

import asyncio
import time

from runtime.tools.edits import apply_edit, str_replace
from tests.conftest import seed


class FakeLsp:
    def cached_diagnostics(self, path: str) -> list:
        return []

    def diagnostics_after_change(self, rel_path: str, new_text: str, timeout: float = 5.0) -> list:
        time.sleep(0.05)
        return []


def test_concurrent_edits_do_not_hang(ctx):
    seed(ctx, "a.py", "value = 1\n")
    seed(ctx, "b.py", "other = 1\n")
    ctx.lsp = FakeLsp()

    async def one(path: str, old: str, new: str) -> str:
        return await apply_edit(
            ctx,
            path,
            lambda src, o=old, n=new: str_replace(src.text, o, n),
            "str_replace",
        )

    async def run():
        return await asyncio.wait_for(
            asyncio.gather(
                one("a.py", "value = 1", "value = 2"),
                one("a.py", "value = 1", "value = 3"),
                one("b.py", "other = 1", "other = 9"),
            ),
            timeout=2.0,
        )

    results = asyncio.run(run())
    assert all(isinstance(item, str) for item in results)
    a_ok = sum(1 for item in results[:2] if item.startswith("ok:"))
    a_err = sum(1 for item in results[:2] if item.startswith("error:"))
    assert a_ok == 1
    assert a_err == 1
    assert results[2].startswith("ok:")
    text_a = (ctx.workspace / "a.py").read_text()
    assert text_a in ("value = 2\n", "value = 3\n")
    assert (ctx.workspace / "b.py").read_text() == "other = 9\n"
