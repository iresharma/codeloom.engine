from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field

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
OUTCOME_CLIP = 2000
TRANSCRIPT_BOUND = 20_000
TRIM_KEEP = 400
TRIM_NOTICE = "\n... (trimmed; re-run the tool if you need this again)"
_PATH_KEYS = ("path", "file", "target", "dest")
_LEFTOVER_LABELS = (
    "leftover_questions:",
    "leftover questions:",
    "leftover:",
)
_OMITTED_MIDDLE = {"role": "system", "content": "(middle omitted)"}


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


def _over_budget(
    messages: list[dict],
    budget: int,
    ratio: float,
    floor: int = 0,
    trigger: float = TRIGGER_RATIO,
) -> bool:
    return _scaled_tokens(messages, ratio, floor) >= int(budget * trigger)


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


def _bounded_transcript(items: list[dict], limit: int = TRANSCRIPT_BOUND) -> str:
    """JSON of items, dropping whole middle messages so the tail still fits."""
    raw = json.dumps(items, default=str)
    if len(raw) <= limit:
        return raw
    if not items:
        return "[]"
    n = len(items)
    for tail_len in range(n - 1, 0, -1):
        tail_start = n - tail_len
        if tail_start <= 1:
            continue
        dumped = json.dumps(
            [items[0], _OMITTED_MIDDLE, *items[tail_start:]], default=str
        )
        if len(dumped) <= limit:
            return dumped
    for tail_len in range(n, 0, -1):
        tail = items[-tail_len:]
        dumped = json.dumps([_OMITTED_MIDDLE, *tail], default=str)
        if len(dumped) <= limit:
            return dumped
        dumped = json.dumps(tail, default=str)
        if len(dumped) <= limit:
            return dumped
    stub = {"role": items[-1].get("role") or "user", "content": "(truncated)"}
    return json.dumps([stub], default=str)


def _clip_labeled(text: str, limit: int = SUMMARY_CLIP) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    nl = cut.rfind("\n")
    if nl >= limit // 2:
        return cut[:nl].rstrip()
    return cut


def _leftover_from_text(text: str) -> list[str]:
    leftover: list[str] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        lower = stripped.lower()
        matched = next(
            (label for label in _LEFTOVER_LABELS if lower.startswith(label)),
            None,
        )
        if matched is None:
            continue
        value = stripped[len(matched) :].strip()
        leftover.extend(part.strip() for part in value.split(";") if part.strip())
    return leftover


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
    messages: list[dict], budget: int, ratio: float, trigger: float = TRIGGER_RATIO
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
        if not _over_budget(candidate, budget, ratio, trigger=trigger):
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
    trigger_ratio: float = TRIGGER_RATIO,
    keep_full: int = KEEP_FULL_TOOL_RESULTS,
) -> tuple[list[dict], dict]:
    estimated = _scaled_tokens(messages, ratio, last_prompt_tokens)
    if estimated < int(budget * trigger_ratio):
        return messages, _info(
            "noop",
            before=len(messages),
            after=len(messages),
            saved=0,
            tokens_after=estimated,
        )
    before = len(messages)
    trimmed, saved = trim_tool_results(messages, keep=keep_full)
    estimated = _scaled_tokens(trimmed, ratio)
    strategy = "trim"
    summary = ""
    if _over_budget(trimmed, budget, ratio, trigger=trigger_ratio) and complete is not None:
        try:
            trimmed, extra, summary = await _summarize(trimmed, complete)
            saved += extra
            strategy = "summarize"
            estimated = _scaled_tokens(trimmed, ratio)
        except Exception as exc:  # noqa: BLE001
            # Keep the trim and fall through to harder reduction.
            summary = f"(summarize failed: {exc.__class__.__name__})"
    if _over_budget(trimmed, budget, ratio, trigger=trigger_ratio):
        harder, extra = trim_tool_results(trimmed, keep=0)
        if extra:
            trimmed = harder
            saved += extra
            strategy = "truncate"
            estimated = _scaled_tokens(trimmed, ratio)
    if _over_budget(trimmed, budget, ratio, trigger=trigger_ratio):
        dropped, extra = _drop_oldest(trimmed, budget, ratio, trigger=trigger_ratio)
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
        {"role": "user", "content": _bounded_transcript(to_summarize)},
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


@dataclass
class AgentResult:
    status: str
    summary: str = ""
    outcome: str = ""
    files_touched: list[str] = field(default_factory=list)
    leftover_questions: list[str] = field(default_factory=list)
    missing_checks: list[str] = field(default_factory=list)

    def as_text(self) -> str:
        lines = [
            f"status: {self.status}",
            f"summary: {self.summary}",
        ]
        if self.outcome and self.outcome != self.summary:
            lines.append(f"outcome: {self.outcome}")
        if self.files_touched:
            lines.append("files_touched: " + ", ".join(self.files_touched))
        if self.leftover_questions:
            lines.append("leftover_questions: " + "; ".join(self.leftover_questions))
        if self.missing_checks:
            lines.append("missing_checks: " + ", ".join(self.missing_checks))
        return "\n".join(lines)


def _tools_from_history(messages: list[dict]) -> set[str]:
    names: set[str] = set()
    for message in messages:
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            name = fn.get("name") or call.get("name") or ""
            if name:
                names.add(str(name))
    return names


def _paths_from_history(messages: list[dict]) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for message in messages:
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            raw = fn.get("arguments") or "{}"
            try:
                data = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError:
                data = {}
            if not isinstance(data, dict):
                continue
            for key in _PATH_KEYS:
                value = data.get(key)
                if isinstance(value, str) and value and value not in seen:
                    seen.add(value)
                    paths.append(value)
    return paths


def _last_assistant_text(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") == "assistant" and not (message.get("tool_calls") or []):
            return _content_as_text(message.get("content"))
    return ""


async def compress_for_parent(
    messages: list[dict],
    *,
    complete: Callable | None = None,
    status: str = "ok",
    required_tools: list[str] | None = None,
    files_touched: list[str] | None = None,
    tools_called: set[str] | None = None,
) -> AgentResult:
    """Turn a child transcript into the orch-facing AgentResult.

    When ``files_touched`` is supplied (Subagent.finish always passes it), the
    list is successful edits via ``record_edit``, not reads. History-path
    fallback runs only when ``files_touched`` is None.
    """
    called = (
        tools_called if tools_called is not None else _tools_from_history(messages)
    )
    missing = [name for name in (required_tools or []) if name not in called]
    if status == "ok" and missing:
        status = "incomplete"
    closer = _last_assistant_text(messages)
    outcome = _clip_labeled(closer, OUTCOME_CLIP)
    files = (
        list(files_touched)
        if files_touched is not None
        else _paths_from_history(messages)
    )
    leftover = _leftover_from_text(closer)
    summary = _clip_labeled(outcome, SUMMARY_CLIP)
    work = [item for item in messages if item.get("role") != "system"]
    if complete is not None and len(work) > 4:
        prompt = [
            {
                "role": "system",
                "content": (
                    "This transcript is from a subagent. Report to the orchestrator, "
                    "not a human. No markdown, headings, bullets, or filler. "
                    "Labeled lines for what / paths / facts / verdict / leftover. "
                    "facts must be specific (paths, versions, quoted APIs). "
                    "Do not collapse a survey into a one-liner. "
                    "Omit empty fields. No preamble."
                ),
            },
            {"role": "user", "content": _bounded_transcript(work)},
        ]
        try:
            result = await complete(prompt)
            text = getattr(result, "text", "") or ""
            if text.strip():
                leftover = _leftover_from_text(text) or leftover
                summary = _clip_labeled(text, SUMMARY_CLIP)
        except Exception as exc:  # noqa: BLE001
            summary = f"summarize failed: {exc.__class__.__name__}"
    return AgentResult(
        status=status,
        summary=summary,
        outcome=outcome,
        files_touched=files,
        leftover_questions=leftover,
        missing_checks=missing,
    )

