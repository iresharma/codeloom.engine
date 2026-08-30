from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from openrouter import OpenRouter

_PLACEHOLDERS = {"", "...", "<OPENROUTER_API_KEY>", "your-key", "changeme"}


@dataclass
class ToolCall:
    id: str
    name: str
    arguments_json: str

    def arguments(self) -> dict:
        try:
            data = json.loads(self.arguments_json or "{}")
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}


@dataclass
class LLMResult:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


class OpenRouterLLM:
    def __init__(self, api_key: str, model: str):
        self._api_key = api_key
        self._client = OpenRouter(api_key=api_key)
        self.model = model

    @classmethod
    def from_env(cls, workspace: Path | None = None) -> OpenRouterLLM:
        if workspace is not None:
            _load_env_sh(workspace / "env.sh")
        api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if api_key in _PLACEHOLDERS:
            raise RuntimeError(
                "set OPENROUTER_API_KEY to a real key (env.sh or the environment)"
            )
        model = os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o-mini")
        return cls(api_key=api_key, model=model)

    async def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> LLMResult:
        token = self._api_key
        authorization = (
            token if token.lower().startswith("bearer ") else f"Bearer {token}"
        )
        kwargs: dict = {
            "messages": messages,
            "model": self.model,
            "stream": False,
            "http_headers": {"Authorization": authorization},
        }
        if tools:
            kwargs["tools"] = tools
        response = await self._client.chat.send_async(**kwargs)
        return _result_from(response)


def _load_env_sh(path: Path) -> None:
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
        if existing and existing not in _PLACEHOLDERS:
            continue
        os.environ[key] = value


def _result_from(response) -> LLMResult:
    choices = getattr(response, "choices", None)
    if not choices:
        inner = getattr(response, "object", None) or getattr(response, "result", None)
        if inner is not None and inner is not response:
            return _result_from(inner)
        raise RuntimeError("OpenRouter response had no choices")
    message = choices[0].message
    return LLMResult(text=_text_from(message), tool_calls=_tool_calls_from(message))


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
