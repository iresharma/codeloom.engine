from __future__ import annotations

import asyncio
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
        kwargs: dict = {
            "messages": messages,
            "model": self.model,
            "stream": stream,
            "http_headers": {"Authorization": authorization},
            "timeout_ms": int(timeout_s * 1000),
        }
        if tools:
            kwargs["tools"] = tools
        retries = _retry_config()
        if retries is not None:
            # SDK retries happen inside send_async, before the stream is
            # handed back. Mid-stream failures are not retried — that would
            # duplicate text already forwarded via on_delta.
            kwargs["retries"] = retries
        response = await self._client.chat.send_async(**kwargs)
        if stream:
            return await _result_from_stream(response, on_delta, idle_s)
        result = _result_from(response)
        if on_delta and result.text:
            on_delta("text", result.text)
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


def _retry_config():
    if RetryConfig is None or BackoffStrategy is None:
        return None
    return RetryConfig(
        "backoff",
        BackoffStrategy(500, 8000, 1.5, 60000),
        True,
    )


def _int_or_zero(value) -> int:
    if value is None:
        return 0
    try:
        if value is getattr(value, "__class__", None):
            return 0
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float_or_zero(value) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _usage_from(raw) -> Usage | None:
    if raw is None:
        return None
    details = getattr(raw, "completion_tokens_details", None)
    prompt_details = getattr(raw, "prompt_tokens_details", None)
    reasoning = 0
    cached = 0
    if details is not None:
        reasoning = _int_or_zero(getattr(details, "reasoning_tokens", None))
    if prompt_details is not None:
        cached = _int_or_zero(getattr(prompt_details, "cached_tokens", None))
    return Usage(
        prompt_tokens=_int_or_zero(getattr(raw, "prompt_tokens", 0)),
        completion_tokens=_int_or_zero(getattr(raw, "completion_tokens", 0)),
        total_tokens=_int_or_zero(getattr(raw, "total_tokens", 0)),
        reasoning_tokens=reasoning,
        cached_tokens=cached,
        cost=_float_or_zero(getattr(raw, "cost", 0)),
        requests=1,
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
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage is not None:
                usage = _usage_from(chunk_usage)
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
        )
    finally:
        if closer is not None:
            await closer(None, None, None)
