"""Phase 5 (docs/impl-plans/jev-exp-1.md): intent routing and the
read-path resolver.

TypeSafe classifies; it never generates a search query, a file path, or an
answer. Keyword extraction is deterministic code, not a judge question --
"TypeSafe generating ... search queries" is explicitly out of scope. On
success the resolver hands back gathered *context*; the model still writes
the final prose, it just skips the exploration round trips.

Scope note: the full plan pseudocode chains search -> list_symbols ->
find_symbol -> an answers-check, compounding four judge calls. This
implementation stops after search + rerank + one answers-check -- chaining
the deeper symbol-expansion step adds real fragility (confidence compounds
multiplicatively, per the plan's own "Confidence compounding" section) for
marginal gain over what rerank + a wider read window already buys, and
list_symbols/find_symbol integration is explicitly lower priority than
search in Phase 3 too. Flagged here rather than resolved silently, per the
plan's own instruction for conflicts between the design and the codebase.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from runtime.judge_decisions import (
    RESOLVER_ANSWERS_MESSAGE_FLOOR,
    RESOLVER_CANDIDATE_FLOOR,
    RESOLVER_RANK_CONFIDENCE_FLOOR,
    SEARCH_RERANK_QUESTION_KEY,
    IntentRoute,
    classify_intent,
    classify_meta_action,
    intent_questions,
    intent_signals,
    meta_action_questions,
    rerank_order,
    rerank_question,
    resolver_answers_question,
)
from runtime.tools.fs import read_window
from runtime.tools.search import search_candidates

# Common English words that would otherwise dominate a keyword-extracted rg
# pattern without narrowing anything -- deliberately short and conservative;
# false negatives here just mean the resolver falls through to the normal
# loop, which is always safe.
STOPWORDS = frozenset(
    {
        "the", "is", "are", "was", "were", "be", "been", "being", "in", "on",
        "at", "to", "for", "of", "with", "and", "or", "but", "this", "that",
        "these", "those", "what", "where", "when", "why", "how", "who",
        "does", "do", "did", "has", "have", "had", "can", "could", "should",
        "would", "will", "shall", "may", "might", "it", "its", "there",
        "here", "codebase", "code", "repo", "repository", "project", "file",
        "files", "please", "about", "from", "into", "your", "you", "me",
        "my", "our", "we",
    }
)

MAX_KEYWORDS = 8
MIN_KEYWORDS = 2
CONTEXT_WINDOW_LINES = 40
MAX_FILES_READ = 2
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


@dataclass
class ToolCallRecord:
    name: str
    arguments: dict
    result: str


@dataclass
class Resolution:
    context: str
    trace: list[ToolCallRecord] = field(default_factory=list)
    # True: answers-check passed -- seed ask via run_with_context.
    # False: rerank gathered context but the check failed -- spawn ask
    # with the windows in the task string (soft-seed), not fake history.
    complete: bool = True


@dataclass
class ClassifiedTurn:
    route: IntentRoute
    signals: dict
    latency_ms: int
    meta_action: str = ""


async def classify_turn(judge, message: str) -> ClassifiedTurn | None:
    """One judge call: what kind of turn is `message`? Returns None
    whenever there is nothing to route on (judge disabled, no verdict) --
    the caller then falls through to today's unmodified loop."""
    if judge is None or not getattr(judge, "enabled", False):
        return None
    verdict = await judge.ask({"message": message}, intent_questions(), tag="intent_route")
    if verdict is None:
        return None
    route = classify_intent(verdict)
    meta_action = ""
    if route == "meta":
        meta_verdict = await judge.ask(
            {"message": message}, meta_action_questions(), tag="intent_route"
        )
        meta_action = classify_meta_action(meta_verdict)
        if meta_action == "other":
            route = "default"
    return ClassifiedTurn(
        route=route,
        signals=intent_signals(verdict),
        latency_ms=verdict.latency_ms,
        meta_action=meta_action,
    )


def extract_keywords(message: str, extra: list[str] | None = None) -> list[str]:
    tokens = _TOKEN.findall(message or "")
    seen: list[str] = []
    for token in list(tokens) + list(extra or []):
        if token.lower() in STOPWORDS or token in seen:
            continue
        seen.append(token)
    return seen[:MAX_KEYWORDS]


def _tokens_overlap(left: str, right: str) -> bool:
    a, b = left.lower(), right.lower()
    if a == b:
        return True
    if len(a) < 3 or len(b) < 3:
        return False
    return a in b or b in a


def memory_keywords(workspace: Path, message: str) -> list[str]:
    """Deterministic aliases from workspace memory -- not a JEV query.

    If a file-note path/purpose/entry_points token overlaps a message
    token (substring, min 3 chars), union that note's identifier tokens.
    """
    try:
        from runtime.store.memory import load
        data = load(workspace)
    except OSError:
        return []
    files = data.get("files") or {}
    message_tokens = [
        token
        for token in _TOKEN.findall(message or "")
        if token.lower() not in STOPWORDS
    ]
    if not message_tokens:
        return []
    extras: list[str] = []
    for rel, entry in files.items():
        if not isinstance(entry, dict):
            continue
        hay = " ".join(
            [
                Path(rel).stem,
                str(entry.get("purpose") or ""),
                str(entry.get("entry_points") or ""),
                str(entry.get("note") or ""),
            ]
        )
        hay_tokens = [
            token
            for token in _TOKEN.findall(hay)
            if token.lower() not in STOPWORDS
        ]
        if not any(
            _tokens_overlap(msg, note) for msg in message_tokens for note in hay_tokens
        ):
            continue
        for token in hay_tokens:
            if token not in extras:
                extras.append(token)
    return extras


def _parse_candidate(line: str) -> tuple[str, int] | None:
    parts = line.split(":", 2)
    if len(parts) < 2:
        return None
    path = parts[0]
    try:
        line_no = int(parts[1])
    except ValueError:
        return None
    return path, line_no


async def resolve_locate(workspace: Path, judge, message: str) -> Resolution | None:
    """The read-path resolver: search -> judge rerank -> gather -> one
    answers-check gate. Any step returning nothing usable falls through to
    None, which the caller treats identically to the unmodified loop --
    this makes the resolver purely additive."""
    if judge is None or not getattr(judge, "enabled", False):
        return None
    extras = memory_keywords(workspace, message)
    keywords = extract_keywords(message, extra=extras)
    if len(keywords) < MIN_KEYWORDS:
        return None
    pattern = "|".join(re.escape(word) for word in keywords)
    try:
        candidates = search_candidates(workspace, pattern)
    except (RuntimeError, ValueError, TimeoutError):
        return None
    if len(candidates) < RESOLVER_CANDIDATE_FLOOR:
        return None

    ids = {str(index + 1): line for index, line in enumerate(candidates)}
    questions = {SEARCH_RERANK_QUESTION_KEY: rerank_question(ids)}
    rank_verdict = await judge.ask({"query": message, "candidates": ids}, questions, tag="search_rerank")
    if rank_verdict is None:
        return None
    if rank_verdict.confidence(SEARCH_RERANK_QUESTION_KEY) < RESOLVER_RANK_CONFIDENCE_FLOOR:
        return None
    ordered = rerank_order(rank_verdict, ids)

    trace = [ToolCallRecord("search", {"pattern": pattern}, "\n".join(ordered[:15]))]
    parts = [f"search results for {pattern!r}:\n" + "\n".join(ordered[:15])]
    seen_files: list[str] = []
    for line in ordered:
        parsed = _parse_candidate(line)
        if parsed is None:
            continue
        path, line_no = parsed
        if path in seen_files:
            continue
        seen_files.append(path)
        if len(seen_files) > MAX_FILES_READ:
            break
        offset = max(1, line_no - CONTEXT_WINDOW_LINES // 2)
        try:
            window = read_window(workspace, path, offset=offset, limit=CONTEXT_WINDOW_LINES)
        except (OSError, ValueError):
            continue
        trace.append(ToolCallRecord("read_file", {"path": path, "offset": offset}, window))
        parts.append(window)

    context = "\n\n".join(parts)
    answers_verdict = await judge.ask(
        {"message": message, "context": context[:6000]}, resolver_answers_question(), tag="intent_route"
    )
    if answers_verdict is None:
        return None
    complete = answers_verdict.noul("answers_message") >= RESOLVER_ANSWERS_MESSAGE_FLOOR
    return Resolution(context=context, trace=trace, complete=complete)
