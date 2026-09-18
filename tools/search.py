from __future__ import annotations

from runtime.judge_decisions import (
    SEARCH_RERANK_FLOOR,
    SEARCH_RERANK_QUESTION_KEY,
    SEARCH_RERANK_TOP_N,
    rerank_order,
    rerank_question,
)
from runtime.tools.search import DEFAULT_MAX_MATCHES, MAX_MATCHES, search_candidates
from tools.base import ToolContext, tool


def _as_int(value, default: int) -> int:
    if value is None or value == "":
        return default
    return int(value)


@tool(
    description=(
        "Search the workspace with ripgrep. "
        "Returns path:line:text. Prefer this over listing files and guessing. "
        "Skips caches, venvs, and vendor dirs."
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "ripgrep pattern (regex)",
            },
            "path": {
                "type": "string",
                "description": "Optional file or directory to limit the search",
            },
            "glob": {
                "type": "string",
                "description": "Optional glob, e.g. *.py",
            },
            "max_matches": {
                "type": "integer",
                "description": "Max matches to return (default 80, max 200)",
            },
        },
        "required": ["pattern"],
    },
)
async def search(
    ctx: ToolContext,
    pattern: str,
    path: str = "",
    glob: str = "",
    max_matches=DEFAULT_MAX_MATCHES,
) -> str:
    candidates = search_candidates(ctx.workspace, pattern, path=path or "", glob=glob or "")
    if not candidates:
        return "(no matches)"

    take = min(max(1, _as_int(max_matches, DEFAULT_MAX_MATCHES)), MAX_MATCHES)
    ordered = candidates
    ranked = await _rerank(ctx, pattern, candidates)
    if ranked is not None:
        ordered = ranked
        take = min(take, SEARCH_RERANK_TOP_N)

    extra = max(0, len(ordered) - take)
    body = "\n".join(ordered[:take])
    if extra:
        return f"{body}\n... ({extra} more matches; raise max_matches)"
    return body


async def _rerank(ctx: ToolContext, pattern: str, candidates: list[str]) -> list[str] | None:
    """Phase 3 (docs/impl-plans/jev-exp-1.md): rank ripgrep's candidates
    against the current turn's plain-language query. Returns None whenever
    reranking should not change what the caller sees (judge unusable, below
    the floor, no verdict, or advisory mode) -- the caller then keeps
    ripgrep's own order untouched."""
    judge = getattr(ctx, "judge", None)
    config = ctx.config
    site_mode = config.judge_mode_for("search") if config is not None else "off"
    if (
        judge is None
        or not getattr(judge, "enabled", False)
        or site_mode == "off"
        or len(candidates) <= SEARCH_RERANK_FLOOR
    ):
        return None

    query = ctx.user_request or pattern
    ids = {str(index + 1): line for index, line in enumerate(candidates)}
    questions = {SEARCH_RERANK_QUESTION_KEY: rerank_question(ids)}
    verdict = await judge.ask({"query": query, "candidates": ids}, questions, tag="search_rerank")
    if verdict is None:
        return None

    enforced = site_mode == "enforcing"
    if ctx.on_judgement is not None:
        ctx.on_judgement(
            tag="search_rerank",
            subject=query[:200],
            outcome="ranked",
            signals={"top_confidence": verdict.confidence(SEARCH_RERANK_QUESTION_KEY)},
            enforced=enforced,
            latency_ms=verdict.latency_ms,
            agent_id=ctx.agent_id,
        )
    if not enforced:
        return None
    return rerank_order(verdict, ids)
