from __future__ import annotations

import asyncio

from runtime.tools.edits import apply_edit, str_replace
from tests.conftest import seed


class FakeLsp:
    def cached_diagnostics(self, path: str) -> list:
        return []

    def diagnostics_after_change(self, rel_path: str, new_text: str, timeout: float = 5.0) -> list:
        # Called synchronously via asyncio.to_thread by _lsp_after; the sleep
        # only simulated LSP round-trip latency and isn't needed for this
        # test's concurrency assertions, so it's dropped rather than faked.
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


class SlowJudge:
    """A judge whose ask() genuinely yields to the event loop before
    resolving (unlike FakeJudge's instant return), so two concurrent
    apply_edit calls actually interleave during the write gate's await --
    the scenario invariant 5 exists to protect against."""

    enabled = True

    def __init__(self, delay: float = 0.05):
        self.delay = delay
        self.calls: list[dict] = []

    async def ask(self, state, questions, *, tag: str):
        self.calls.append({"state": state, "tag": tag})
        await asyncio.sleep(self.delay)


def test_concurrent_edits_with_slow_judge_still_exactly_one_winner(ctx):
    """Phase 7's write gate must never reopen the staleness-check-to-write
    race: even with a judge call that actively yields control mid-flight,
    exactly one of two conflicting concurrent edits wins and the other is
    cleanly refused -- never silent corruption, never two winners."""
    from runtime.config import EngineConfig

    seed(ctx, "a.py", "value = 1\n")
    seed(ctx, "b.py", "other = 1\n")
    ctx.lsp = FakeLsp()
    ctx.judge = SlowJudge()
    ctx.config = EngineConfig(judge_mode="enforcing")

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
    assert ctx.judge.calls  # the gate actually ran and actually awaited
    a_ok = sum(1 for item in results[:2] if item.startswith("ok:"))
    a_err = sum(1 for item in results[:2] if item.startswith("error:"))
    assert a_ok == 1
    assert a_err == 1
    assert results[2].startswith("ok:")
    text_a = (ctx.workspace / "a.py").read_text()
    assert text_a in ("value = 2\n", "value = 3\n")  # never corrupted/merged
    assert (ctx.workspace / "b.py").read_text() == "other = 9\n"
