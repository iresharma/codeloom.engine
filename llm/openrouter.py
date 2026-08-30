from __future__ import annotations

import os
from pathlib import Path

from openrouter import OpenRouter

_PLACEHOLDERS = {"", "...", "<OPENROUTER_API_KEY>", "your-key", "changeme"}


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

    async def complete(self, messages: list[dict]) -> str:
        token = self._api_key
        authorization = (
            token if token.lower().startswith("bearer ") else f"Bearer {token}"
        )
        response = await self._client.chat.send_async(
            messages=messages,
            model=self.model,
            stream=False,
            http_headers={"Authorization": authorization},
        )
        return _text_from(response)


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


def _text_from(response) -> str:
    choices = getattr(response, "choices", None)
    if not choices:
        inner = getattr(response, "object", None) or getattr(response, "result", None)
        if inner is not None and inner is not response:
            return _text_from(inner)
        raise RuntimeError("OpenRouter response had no choices")
    content = getattr(choices[0].message, "content", None)
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
