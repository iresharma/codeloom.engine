from __future__ import annotations

import asyncio

from runtime.tools.edits import apply_edit, apply_patch_text, parse_unified_diff
from tests.conftest import seed


def test_parse_and_apply_simple_hunk():
    text = "a\nb\nc\n"
    patch = """\
--- a/file
+++ b/file
@@ -2,1 +2,1 @@
-b
+B
"""
    assert apply_patch_text(text, patch) == "a\nB\nc\n"


def test_fuzz_offset_drift():
    text = "a\nb\nc\nd\ne\n"
    patch = """\
@@ -1,1 +1,1 @@
-c
+C
"""
    # header claims line 1 but the context is actually line 3; fuzz should find it
    hunks = parse_unified_diff(patch)
    hunks[0].old_start = 1
    from runtime.tools.edits import hunks_to_text

    assert hunks_to_text(text, hunks, fuzz=5) == "a\nb\nC\nd\ne\n"


def test_multi_hunk_all_or_nothing(ctx):
    seed(ctx, "a.py", "a = 1\nb = 2\nc = 3\n")
    patch = """\
@@ -1,1 +1,1 @@
-a = 1
+a = 10
@@ -3,1 +3,1 @@
-c = NOPE
+c = 30
"""

    async def run():
        return await apply_edit(
            ctx, "a.py", lambda src: apply_patch_text(src.text, patch), "apply_patch"
        )

    result = asyncio.run(run())
    assert "did not match" in result
    assert (ctx.workspace / "a.py").read_text() == "a = 1\nb = 2\nc = 3\n"


def test_apply_patch_success(ctx):
    seed(ctx, "a.py", "a = 1\nb = 2\nc = 3\n")
    patch = """\
@@ -1,1 +1,1 @@
-a = 1
+a = 10
@@ -3,1 +3,1 @@
-c = 3
+c = 30
"""

    async def run():
        return await apply_edit(
            ctx, "a.py", lambda src: apply_patch_text(src.text, patch), "apply_patch"
        )

    result = asyncio.run(run())
    assert result.startswith("ok:")
    assert (ctx.workspace / "a.py").read_text() == "a = 10\nb = 2\nc = 30\n"
