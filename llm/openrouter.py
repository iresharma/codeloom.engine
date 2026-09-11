from __future__ import annotations

import asyncio
import copy
import json
import os
from collections.abc import Callable
from pathlib import Path

from openrouter import OpenRouter

from llm.provider import LLMError, LLMResult, ToolCall, Usage

try:
    from openrouter.utils.retries import BackoffStrategy, RetryConfig
except ImportError:  # pragma: no cover - older SDK
    RetryConfig = None  # type: ignore[misc, assignment]
    BackoffStrategy = None  # type: ignore[misc, assignment]

PLACEHOLDERS = {"", "...", "<OPENROUTER_API_KEY>", "your-key", "changeme"}
_PLACEHOLDERS = PLACEHOLDERS

# Re-exports so `from llm.openrouter import LLMResult` keeps working.
__all__ = [
    "LLMError",
    "LLMResult",
    "OpenRouterLLM",
    "ToolCall",
    "Usage",
    "load_env_sh",
    "with_cache_breakpoints",
]


class OpenRouterLLM:
    def __init__(self, api_key: str, model: str, config=None):
        self._api_key = api_key
        self._client = OpenRouter(api_key=api_key)
        self.model = model
        self._config = config

    @classmethod
    def from_env(cls, workspace: Path | None = None, config=None) -> OpenRouterLLM:
        if workspace is not None:
            load_env_sh(workspace / "env.sh")
        api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if api_key in PLACEHOLDERS:
            raise RuntimeError(
                "set OPENROUTER_API_KEY to a real key (env.sh or the environment)"
            )
        model = os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o-mini")
        return cls(api_key=api_key, model=model, config=config)

    async def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        *,
        on_delta: Callable[[str, str], None] | None = None,
        model: str | None = None,
    ) -> LLMResult:
        token = self._api_key
        authorization = (
            token if token.lower().startswith("bearer ") else f"Bearer {token}"
        )
        stream = True
        timeout_s = 600.0
        idle_s = 90.0
        if self._config is not None:
            stream = bool(self._config.llm_stream)
            timeout_s = float(self._config.llm_timeout_s)
            idle_s = float(self._config.stream_idle_s)
        cached_messages, cached_tools = with_cache_breakpoints(messages, tools)
        kwargs: dict = {
            "messages": cached_messages,
            "model": model or self.model,
            "stream": stream,
            "http_headers": {"Authorization": authorization},
            "timeout_ms": int(timeout_s * 1000),
        }
        if cached_tools:
            kwargs["tools"] = cached_tools
        retries = _retry_config()
        if retries is not None:
            # SDK retries happen inside send_async, before the stream is
            # handed back. Mid-stream failures are not retried — that would
            # duplicate text already forwarded via on_delta.
            kwargs["retries"] = retries
        response = await self._client.chat.send_async(**kwargs)
        if stream:
            result = await _result_from_stream(response, on_delta, idle_s)
        else:
            result = _result_from(response)
            if on_delta and result.text:
                on_delta("text", result.text)
        return await self._fill_missing_cost(result)

    async def _fill_missing_cost(self, result: LLMResult) -> LLMResult:
        usage = result.usage
        gen_id = result.generation_id
        if (
            usage is None
            or usage.cost
            or not (usage.total_tokens or usage.prompt_tokens)
            or not gen_id
        ):
            return result
        try:
            meta = await self._client.generations.get_generation_async(id=gen_id)
        except Exception:
            return result
        data = getattr(meta, "data", None) or meta
        cost = _cost_from(data)
        if not cost:
            cost = _float_or_zero(_field(data, "total_cost")) or _float_or_zero(
                _field(data, "usage")
            )
        if cost:
            usage.cost = cost
        return result


def load_env_sh(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if not key:
            continue
        existing = os.environ.get(key, "").strip()
        if existing and existing not in PLACEHOLDERS:
            continue
        os.environ[key] = value


_load_env_sh = load_env_sh

_CACHE = {"type": "ephemeral"}


def with_cache_breakpoints(
    messages: list[dict], tools: list[dict] | None = None
) -> tuple[list[dict], list[dict] | None]:
    """Copy messages/tools and mark Anthropic cache breakpoints.

    Breakpoints: last tool schema, system prompt, last stable history
    message (tool/user/assistant text — not an empty tool_calls stub).
    Callers keep the originals; compaction rewriting history is a miss
    on the next turn, which is intended.
    """
    msgs = copy.deepcopy(messages)
    tool_list = copy.deepcopy(tools) if tools else None
    if tool_list:
        last = tool_list[-1]
        if isinstance(last, dict):
            tool_list[-1] = {**last, "cache_control": dict(_CACHE)}
    if msgs and msgs[0].get("role") == "system":
        msgs[0] = _mark_message(msgs[0])
    for index in range(len(msgs) - 1, 0, -1):
        item = msgs[index]
        if item.get("role") == "assistant" and item.get("tool_calls"):
            text = item.get("content")
            if not (isinstance(text, str) and text.strip()):
                continue
        if item.get("role") in {"tool", "user", "assistant"}:
            msgs[index] = _mark_message(item)
            break
    return msgs, tool_list


def _mark_message(message: dict) -> dict:
    content = message.get("content")
    if isinstance(content, str):
        return {
            **message,
            "content": [
                {
                    "type": "text",
                    "text": content,
                    "cache_control": dict(_CACHE),
                }
            ],
        }
    if isinstance(content, list) and content:
        parts = list(content)
        last = parts[-1]
        if isinstance(last, dict):
            parts[-1] = {**last, "cache_control": dict(_CACHE)}
        return {**message, "content": parts}
    return {**message, "cache_control": dict(_CACHE)}


def _retry_config():
    if RetryConfig is None or BackoffStrategy is None:
        return None
    return RetryConfig(
        "backoff",
        BackoffStrategy(500, 8000, 1.5, 60000),
        True,
    )


def _field(raw, name, default=None):
    if isinstance(raw, dict):
        return raw.get(name, default)
    return getattr(raw, name, default)


def _is_missing(value) -> bool:
    if value is None:
        return True
    # Speakeasy UNSET is a pydantic model with __bool__ == False.
    if type(value).__name__ == "Unset":
        return True
    if value is getattr(value, "__class__", None):
        return True
    return False


def _int_or_zero(value) -> int:
    if _is_missing(value):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float_or_zero(value) -> float:
    if _is_missing(value):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _cost_from(raw) -> float:
    cost = _float_or_zero(_field(raw, "cost"))
    if cost:
        return cost
    details = _field(raw, "cost_details")
    if details is None or _is_missing(details):
        return 0.0
    total = _float_or_zero(_field(details, "upstream_inference_cost"))
    if total:
        return total
    return _float_or_zero(_field(details, "upstream_inference_prompt_cost")) + _float_or_zero(
        _field(details, "upstream_inference_completions_cost")
    )


def _usage_from(raw) -> Usage | None:
    if raw is None:
        return None
    details = _field(raw, "completion_tokens_details")
    prompt_details = _field(raw, "prompt_tokens_details")
    reasoning = 0
    cached = 0
    if details is not None:
        reasoning = _int_or_zero(_field(details, "reasoning_tokens"))
    if prompt_details is not None:
        cached = _int_or_zero(_field(prompt_details, "cached_tokens"))
    return Usage(
        prompt_tokens=_int_or_zero(_field(raw, "prompt_tokens", 0)),
        completion_tokens=_int_or_zero(_field(raw, "completion_tokens", 0)),
        total_tokens=_int_or_zero(_field(raw, "total_tokens", 0)),
        reasoning_tokens=reasoning,
        cached_tokens=cached,
        cost=_cost_from(raw),
        requests=1,
    )


def _prefer_usage(current: Usage | None, incoming: Usage | None) -> Usage | None:
    if incoming is None:
        return current
    if current is None:
        return incoming
    return Usage(
        prompt_tokens=incoming.prompt_tokens or current.prompt_tokens,
        completion_tokens=incoming.completion_tokens or current.completion_tokens,
        total_tokens=incoming.total_tokens or current.total_tokens,
        reasoning_tokens=incoming.reasoning_tokens or current.reasoning_tokens,
        cached_tokens=incoming.cached_tokens or current.cached_tokens,
        cost=incoming.cost or current.cost,
        requests=incoming.requests or current.requests or 1,
    )


def _result_from(response) -> LLMResult:
    choices = getattr(response, "choices", None)
    if not choices:
        inner = getattr(response, "object", None) or getattr(response, "result", None)
        if inner is not None and inner is not response:
            return _result_from(inner)
        raise RuntimeError("OpenRouter response had no choices")
    message = choices[0].message
    usage = _usage_from(getattr(response, "usage", None))
    finish = getattr(choices[0], "finish_reason", None)
    return LLMResult(
        text=_text_from(message),
        tool_calls=_tool_calls_from(message),
        usage=usage,
        finish_reason=str(finish) if finish else None,
        model=getattr(response, "model", None),
        generation_id=getattr(response, "id", None) or None,
    )


def _text_from(message) -> str:
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                parts.append(str(part.get("text") or ""))
            else:
                parts.append(str(getattr(part, "text", "") or ""))
        return "".join(parts)
    return ""


def _tool_calls_from(message) -> list[ToolCall]:
    raw = getattr(message, "tool_calls", None)
    if not isinstance(raw, list):
        return []
    calls: list[ToolCall] = []
    for item in raw:
        fn = getattr(item, "function", item)
        name = getattr(fn, "name", "") or ""
        arguments = getattr(fn, "arguments", "") or "{}"
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments)
        call_id = getattr(item, "id", "") or name
        if name:
            calls.append(ToolCall(id=call_id, name=name, arguments_json=arguments))
    return calls


async def _result_from_stream(stream, on_delta, idle_s: float) -> LLMResult:
    text_parts: list[str] = []
    calls: dict[int, dict] = {}
    usage = None
    finish_reason = None
    model = None
    generation_id = None
    closer = getattr(stream, "__aexit__", None)
    try:
        if hasattr(stream, "__aenter__"):
            await stream.__aenter__()
        iterator = stream.__aiter__()
        while True:
            try:
                chunk = await asyncio.wait_for(iterator.__anext__(), timeout=idle_s)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError as exc:
                raise TimeoutError("LLM stream idle timeout") from exc
            error = getattr(chunk, "error", None)
            if error is not None:
                message = getattr(error, "message", None) or str(error)
                code = getattr(error, "code", None)
                raise LLMError(message, code=_int_or_zero(code) or None)
            if getattr(chunk, "model", None):
                model = chunk.model
            chunk_id = getattr(chunk, "id", None)
            if chunk_id:
                generation_id = chunk_id
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage is not None:
                usage = _prefer_usage(usage, _usage_from(chunk_usage))
            choices = getattr(chunk, "choices", None) or []
            for choice in choices:
                reason = getattr(choice, "finish_reason", None)
                if reason:
                    finish_reason = str(reason)
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue
                content = getattr(delta, "content", None)
                if content:
                    text_parts.append(content)
                    if on_delta:
                        on_delta("text", content)
                reasoning = getattr(delta, "reasoning", None)
                if reasoning:
                    # Displayed to the client; not fed back into history.
                    if on_delta:
                        on_delta("reasoning", reasoning)
                for item in getattr(delta, "tool_calls", None) or []:
                    index = int(getattr(item, "index", 0) or 0)
                    slot = calls.setdefault(
                        index, {"id": "", "name": "", "arguments": ""}
                    )
                    if getattr(item, "id", None):
                        slot["id"] = item.id
                    fn = getattr(item, "function", None)
                    if fn is not None:
                        if getattr(fn, "name", None):
                            slot["name"] = fn.name
                        fragment = getattr(fn, "arguments", None) or ""
                        if fragment:
                            slot["arguments"] += fragment
        assembled = []
        for index in sorted(calls):
            slot = calls[index]
            name = slot["name"]
            if not name:
                continue
            assembled.append(
                ToolCall(
                    id=slot["id"] or f"call_{index}",
                    name=name,
                    arguments_json=slot["arguments"] or "{}",
                )
            )
        return LLMResult(
            text="".join(text_parts),
            tool_calls=assembled,
            usage=usage,
            finish_reason=finish_reason,
            model=model,
            generation_id=generation_id,
        )
    finally:
        if closer is not None:
            await closer(None, None, None)
