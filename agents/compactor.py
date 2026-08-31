from __future__ import annotations

import json
from collections.abc import Callable

# Observed provider overflow phrasing. Each entry is a verbatim substring;
# the date is when it was recorded. A test pins these so a reword fails loudly.
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


def estimate_tokens(messages: list[dict]) -> int:
    raw = len(json.dumps(messages)) // CHARS_PER_TOKEN
    overhead = len(messages) * TOKENS_PER_MESSAGE
    tools = 0
    for message in messages:
        calls = message.get("tool_calls") or []
        tools += len(calls) * TOKENS_PER_TOOL_CALL
    return raw + overhead + tools


def validate_history(messages: list[dict]) -> list[str]:
    errors: list[str] = []
    pending: dict[str, int] | None = None
    for index, message in enumerate(messages):
        role = message.get("role")
        calls = message.get("tool_calls") or []
        if role == "assistant" and calls:
            if pending is not None:
                errors.append(f"unclosed tool group before message {index}")
            pending = {str(call.get("id") or ""): index for call in calls}
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
    lowered = (message or "").lower()
    return any(marker.lower() in lowered for _, _, marker in CONTEXT_ERROR_MARKERS)


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
        content = message.get("content") or ""
        if len(content) <= TRIM_KEEP:
            out.append(message)
            continue
        trimmed = dict(message)
        trimmed["content"] = (
            content[:TRIM_KEEP] + "\n... (trimmed; re-run the tool if you need this again)"
        )
        saved += len(content) - len(trimmed["content"])
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


async def compact(
    messages: list[dict],
    budget: int,
    *,
    complete: Callable | None = None,
    last_prompt_tokens: int = 0,
    ratio: float = 1.0,
) -> tuple[list[dict], dict]:
    estimated = int(estimate_tokens(messages) * (ratio or 1.0))
    if last_prompt_tokens:
        estimated = max(estimated, last_prompt_tokens)
    if estimated < int(budget * TRIGGER_RATIO):
        return messages, {
            "strategy": "noop",
            "messages_before": len(messages),
            "messages_after": len(messages),
            "chars_saved": 0,
            "summary": "",
        }
    before = len(messages)
    trimmed, saved = trim_tool_results(messages)
    estimated = int(estimate_tokens(trimmed) * (ratio or 1.0))
    strategy = "trim"
    summary = ""
    if estimated >= int(budget * TRIGGER_RATIO) and complete is not None:
        trimmed, extra, summary = await _summarize(trimmed, complete)
        saved += extra
        strategy = "summarize"
    errors = validate_history(trimmed)
    if errors:
        raise RuntimeError("compaction broke history: " + "; ".join(errors))
    return trimmed, {
        "strategy": strategy,
        "messages_before": before,
        "messages_after": len(trimmed),
        "chars_saved": saved,
        "summary": summary[:SUMMARY_CLIP],
    }


async def _summarize(messages: list[dict], complete) -> tuple[list[dict], int, str]:
    if len(messages) < 4:
        return messages, 0, ""
    # Keep system (0), optional existing summary (1), and the last exchange.
    last = _last_exchange_start(messages)
    cut = group_boundary(messages, max(2, last - 2))
    head = messages[:cut]
    tail = messages[cut:]
    if len(head) <= 1:
        return messages, 0, ""
    to_summarize = [item for item in head if item.get("role") != "system"]
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
    system = [item for item in messages if item.get("role") == "system"][:1]
    return system + [summary] + tail, max(0, saved), text


def _last_exchange_start(messages: list[dict]) -> int:
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "user":
            return index
    return max(1, len(messages) - 1)


def read_context_md(workspace, cap: int = CONTEXT_MD_CAP) -> str:
    path = workspace / ".engine" / "context.md"
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > cap:
        return text[:cap] + "\n... (truncated)"
    return text
