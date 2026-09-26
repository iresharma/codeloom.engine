"""`run_verify` -- the reviewer's one allowlisted command.

The reviewer stays read-only and has no general shell. This runs exactly the
command the harness chose for this worktree, in a throwaway copy of it, so
the reviewer can confirm the verdict for itself and do a mutation
spot-check: flip one comparison, run verify, and see whether anything fails.
The copy is deleted afterwards; the real worktree is never touched.
"""

from __future__ import annotations

import asyncio

from runtime.verify import (
    DisposableWorktree,
    VerifyPlan,
    format_verify_block,
    run_verify as run_verify_impl,
)
from tools.base import ToolContext, tool

MUTATION_MAX = 4000


@tool(
    description=(
        "Run this worktree's verify command (the one the engine already ran) "
        "in a throwaway copy of the worktree, and report the structured "
        "result. Optionally apply one single-string substitution to one file "
        "in the copy first, so you can check that a test actually fails when "
        "the new logic is broken: flip a comparison or drop a branch, run "
        "this, and confirm the suite goes red. The copy is deleted "
        "afterwards and the real worktree is never modified. You cannot "
        "choose the command."
    ),
    parameters={
        "type": "object",
        "properties": {
            "mutate_path": {
                "type": "string",
                "description": (
                    "Optional workspace-relative file to mutate in the copy "
                    "only. Omit for a plain verify run."
                ),
            },
            "mutate_old": {
                "type": "string",
                "description": (
                    "Exact text to replace in that file (must appear exactly "
                    "once). Requires mutate_path."
                ),
            },
            "mutate_new": {
                "type": "string",
                "description": (
                    "Replacement text, e.g. a flipped comparison. May be "
                    "empty to delete the matched text."
                ),
            },
        },
    },
)
async def run_verify(
    ctx: ToolContext,
    mutate_path: str = "",
    mutate_old: str = "",
    mutate_new: str = "",
) -> str:
    command = (getattr(ctx, "verify_command", "") or "").strip()
    if not command:
        return (
            "error: no verify command is configured for this worktree; "
            "report the change as unverified"
        )
    if mutate_old and not mutate_path:
        return "error: mutate_old requires mutate_path"
    if mutate_path and not mutate_old:
        return "error: mutate_path requires mutate_old"
    if len(mutate_old) > MUTATION_MAX or len(mutate_new) > MUTATION_MAX:
        return f"error: a spot-check mutation must be under {MUTATION_MAX} characters"

    def prepare() -> tuple[DisposableWorktree, str]:
        holder = DisposableWorktree(ctx.workspace)
        copy = holder.__enter__()
        if mutate_path:
            err = _mutate(copy, mutate_path, mutate_old, mutate_new)
            if err:
                holder.__exit__(None, None, None)
                return holder, err
        return holder, ""

    holder, err = await asyncio.to_thread(prepare)
    if err:
        return err
    try:
        result = await run_verify_impl(
            holder.path, VerifyPlan([command], "config")
        )
    finally:
        await asyncio.to_thread(holder.__exit__, None, None, None)
    header = ""
    if mutate_path:
        header = (
            f"(spot-check: {mutate_path} mutated in the disposable copy only; "
            "a verify that still passes means nothing covers this logic)\n"
        )
    return header + format_verify_block(result)


def _mutate(copy_root, path: str, old: str, new: str) -> str:
    """Apply the one substitution inside the copy. Never touches the real
    worktree: `copy_root` is a tempdir and the path must stay inside it."""
    from pathlib import Path

    candidate = Path(path)
    if candidate.is_absolute():
        return "error: mutate_path must be workspace-relative"
    target = (copy_root / candidate).resolve()
    try:
        target.relative_to(Path(copy_root).resolve())
    except ValueError:
        return "error: mutate_path escapes the worktree copy"
    if not target.is_file():
        return f"error: {path} is not a file in this worktree"
    try:
        text = target.read_text(errors="replace")
    except OSError as exc:
        return f"error: reading {path}: {exc}"
    hits = text.count(old)
    if hits == 0:
        return f"error: mutate_old not found in {path}"
    if hits > 1:
        return f"error: mutate_old appears {hits} times in {path}; make it unique"
    try:
        target.write_text(text.replace(old, new, 1))
    except OSError as exc:
        return f"error: writing the copy of {path}: {exc}"
    return ""
