from __future__ import annotations

import json
from collections.abc import Callable

# Observed provider overflow phrasing. Each entry is a verbatim substring;
# the date is when it was recorded. A test pins these so a reword fails loudly.
# Markers are a fallback only — STRUCTURAL_RATIO against last prompt_tokens
# is the primary overflow signal. Providers reword these strings.
CONTEXT_ERROR_MARKERS: tuple[tuple[str, str, str], ...] = (
    ("openai", "2024-11", "context_length_exceeded"),
    ("openai", "2024-11", "maximum context length"),
    ("anthropic", "2025-03", "prompt is too long"),
    ("openrouter", "2025-06", "context length"),
    ("generic", "2024-01", "too many tokens"),
)

KEEP_FULL_TOOL_RESULTS = 3
TOKENS_PER_MESSAGE = 4
TOKENS_PER_TOOL_CALL = 8
CHARS_PER_TOKEN = 4
TRIGGER_RATIO = 0.7
STRUCTURAL_RATIO = 0.8
SUMMARY_CLIP = 400
CONTEXT_MD_CAP = 4000
TRIM_KEEP = 400
TRIM_NOTICE = "\n... (trimmed; re-run the tool if you need this again)"


def estimate_tokens(messages: list[dict]) -> int:
    # json.dumps already includes tool_call payloads. TOKENS_PER_TOOL_CALL is
    # extra framing overhead (role / name / id), not a second count of the JSON.
    raw = len(json.dumps(messages)) // CHARS_PER_TOKEN
    overhead = len(messages) * TOKENS_PER_MESSAGE
    tools = 0
    for message in messages:
        calls = message.get("tool_calls") or []
        tools += len(calls) * TOKENS_PER_TOOL_CALL
    return raw + overhead + tools


def _scaled_tokens(messages: list[dict], ratio: float, floor: int = 0) -> int:
    estimated = int(estimate_tokens(messages) * (ratio or 1.0))
    if floor:
        estimated = max(estimated, floor)
    return estimated


def _over_budget(messages: list[dict], budget: int, ratio: float, floor: int = 0) -> bool:
    return _scaled_tokens(messages, ratio, floor) >= int(budget * TRIGGER_RATIO)


def validate_history(messages: list[dict]) -> list[str]:
    errors: list[str] = []
    pending: dict[str, int] | None = None
    for index, message in enumerate(messages):
        role = message.get("role")
        calls = message.get("tool_calls") or []
        if role == "assistant" and calls:
            if pending is not None:
                errors.append(f"unclosed tool group before message {index}")
            pending = {}
            for call_index, call in enumerate(calls):
                call_id = str(call.get("id") or "")
                if not call_id:
                    call_id = f"missing:{index}:{call_index}"
                    errors.append(f"missing tool_call id at message {index} call {call_index}")
                elif call_id in pending:
                    errors.append(f"duplicate tool_call id {call_id} at message {index}")
                pending[call_id] = index
            continue
        if role == "tool":
            if pending is None:
                errors.append(f"orphan tool message at {index}")
                continue
            call_id = str(message.get("tool_call_id") or "")
            if call_id not in pending:
                errors.append(f"unexpected tool_call_id {call_id} at {index}")
            else:
                pending.pop(call_id, None)
            if not pending:
                pending = None
            continue
        if pending is not None:
            errors.append(f"tool group interrupted at message {index}")
            pending = None
    if pending is not None:
        errors.append("unclosed tool group at end of history")
    return errors


def looks_like_overflow(message: str, prompt_tokens: int, budget: int) -> bool:
    if prompt_tokens and budget and prompt_tokens >= int(budget * STRUCTURAL_RATIO):
        return True
    # Secondary: provider error phrasing. Used when we have no usage yet,
    # or the token check missed a sudden jump. Do not treat as a stable API.
    lowered = (message or "").lower()
    return any(marker.lower() in lowered for _, _, marker in CONTEXT_ERROR_MARKERS)


def _content_as_text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
                    continue
                inner = block.get("content")
                if isinstance(inner, str):
                    parts.append(inner)
                    continue
                parts.append(json.dumps(block, default=str))
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return json.dumps(content, default=str)


def trim_tool_results(
    messages: list[dict], keep: int = KEEP_FULL_TOOL_RESULTS
) -> tuple[list[dict], int]:
    tool_indexes = [i for i, item in enumerate(messages) if item.get("role") == "tool"]
    drop = set(tool_indexes[:-keep]) if keep else set(tool_indexes)
    saved = 0
    out = []
    for index, message in enumerate(messages):
        if index not in drop:
            out.append(message)
            continue
        text = _content_as_text(message.get("content"))
        if len(text) <= TRIM_KEEP:
            out.append(message)
            continue
        trimmed = dict(message)
        trimmed["content"] = text[:TRIM_KEEP] + TRIM_NOTICE
        saved += len(text) - len(trimmed["content"])
        out.append(trimmed)
    return out, saved


def group_boundary(messages: list[dict], cut: int) -> int:
    """Move cut forward so we never split an assistant tool_calls group."""
    if cut <= 1:
        return cut
    while cut < len(messages):
        prev = messages[cut - 1]
        if prev.get("role") == "assistant" and prev.get("tool_calls"):
            cut += 1
            continue
        if prev.get("role") == "tool":
            nxt = messages[cut] if cut < len(messages) else None
            if nxt and nxt.get("role") == "tool":
                cut += 1
                continue
        break
    return cut


def _drop_oldest(
    messages: list[dict], budget: int, ratio: float
) -> tuple[list[dict], int]:
    """Drop oldest complete groups until under budget, keeping system + last exchange."""
    if len(messages) < 2:
        return messages, 0
    prefix = 1 if messages[0].get("role") == "system" else 0
    last = _last_exchange_start(messages)
    if last <= prefix:
        return messages, 0
    cut = prefix
    best = messages
    saved = 0
    while cut < last:
        nxt = group_boundary(messages, cut + 1)
        if nxt > last:
            break
        candidate = messages[:prefix] + messages[nxt:]
        if validate_history(candidate):
            cut = nxt
            continue
        saved = sum(len(json.dumps(item)) for item in messages[prefix:nxt])
        best = candidate
        if not _over_budget(candidate, budget, ratio):
            return candidate, saved
        cut = nxt
    return best, saved


def _info(
    strategy: str,
    *,
    before: int,
    after: int,
    saved: int,
    summary: str = "",
    tokens_after: int = 0,
) -> dict:
    return {
        "strategy": strategy,
        "messages_before": before,
        "messages_after": after,
        "chars_saved": saved,
        "summary": summary[:SUMMARY_CLIP],
        "tokens_after": tokens_after,
    }


async def compact(
    messages: list[dict],
    budget: int,
    *,
    complete: Callable | None = None,
    last_prompt_tokens: int = 0,
    ratio: float = 1.0,
) -> tuple[list[dict], dict]:
    estimated = _scaled_tokens(messages, ratio, last_prompt_tokens)
    if estimated < int(budget * TRIGGER_RATIO):
        return messages, _info(
            "noop",
            before=len(messages),
            after=len(messages),
            saved=0,
            tokens_after=estimated,
        )
    before = len(messages)
    trimmed, saved = trim_tool_results(messages)
    estimated = _scaled_tokens(trimmed, ratio)
    strategy = "trim"
    summary = ""
    if _over_budget(trimmed, budget, ratio) and complete is not None:
        try:
            trimmed, extra, summary = await _summarize(trimmed, complete)
            saved += extra
            strategy = "summarize"
            estimated = _scaled_tokens(trimmed, ratio)
        except Exception as exc:  # noqa: BLE001
            # Keep the trim and fall through to harder reduction.
            summary = f"(summarize failed: {exc.__class__.__name__})"
    if _over_budget(trimmed, budget, ratio):
        harder, extra = trim_tool_results(trimmed, keep=0)
        if extra:
            trimmed = harder
            saved += extra
            strategy = "truncate"
            estimated = _scaled_tokens(trimmed, ratio)
    if _over_budget(trimmed, budget, ratio):
        dropped, extra = _drop_oldest(trimmed, budget, ratio)
        if dropped is not trimmed:
            trimmed = dropped
            saved += extra
            strategy = "truncate"
            estimated = _scaled_tokens(trimmed, ratio)
    errors = validate_history(trimmed)
    if errors:
        raise RuntimeError("compaction broke history: " + "; ".join(errors))
    return trimmed, _info(
        strategy,
        before=before,
        after=len(trimmed),
        saved=saved,
        summary=summary,
        tokens_after=estimated,
    )


async def _summarize(messages: list[dict], complete) -> tuple[list[dict], int, str]:
    if len(messages) < 4:
        return messages, 0, ""
    # Keep first system and the last exchange; fold everything else — including
    # extra system messages — into the summary so they are not dropped.
    last = _last_exchange_start(messages)
    cut = group_boundary(messages, max(2, last - 2))
    head = messages[:cut]
    tail = messages[cut:]
    if len(head) <= 1:
        return messages, 0, ""
    first_system = next((item for item in messages if item.get("role") == "system"), None)
    to_summarize = [item for item in head if item is not first_system]
    if not to_summarize:
        return messages, 0, ""
    prompt = [
        {
            "role": "system",
            "content": (
                "Summarize this coding-agent transcript in a short paragraph. "
                "Keep file paths, decisions, and unfinished work. No preamble."
            ),
        },
        {"role": "user", "content": json.dumps(to_summarize)[:20_000]},
    ]
    result = await complete(prompt)
    text = getattr(result, "text", "") or ""
    summary = {
        "role": "system",
        "content": f"## Earlier conversation\n{text}",
    }
    saved = sum(len(json.dumps(item)) for item in to_summarize) - len(text)
    kept_system = [first_system] if first_system is not None else []
    return kept_system + [summary] + tail, max(0, saved), text


def _last_exchange_start(messages: list[dict]) -> int:
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "user":
            return index
    return max(1, len(messages) - 1)


def read_context_md(workspace, cap: int = CONTEXT_MD_CAP) -> str:
    path = workspace / ".engine" / "context.md"
    if not path.is_file():
        return ""
    raw = path.read_bytes()
    try:
        raw.decode("utf-8")
        replaced = False
    except UnicodeDecodeError:
        replaced = True
    text = raw.decode("utf-8", errors="replace")
    notes = []
    if len(text) > cap:
        text = text[:cap]
        notes.append("truncated")
    if replaced:
        notes.append("encoding errors replaced")
    if notes:
        text = text + "\n... (" + "; ".join(notes) + ")"
    return text
