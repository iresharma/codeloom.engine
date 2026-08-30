from __future__ import annotations

from llm.openrouter import OpenRouterLLM

DEFAULT_SYSTEM = "You are a coding assistant. Be concise and direct."


class AgentLoop:
    """One model turn at a time. Tools and subagents come later."""

    def __init__(
        self,
        llm: OpenRouterLLM,
        system_prompt: str = DEFAULT_SYSTEM,
    ):
        self._llm = llm
        self._system_prompt = system_prompt
        self._history: list[dict] = []

    def hydrate(self, messages) -> None:
        self._history = []
        for message in messages:
            role = "user" if message.role == "user" else "assistant"
            self._history.append({"role": role, "content": message.text})

    def _build_messages(self) -> list[dict]:
        return [{"role": "system", "content": self._system_prompt}] + list(
            self._history
        )

    async def run(self, task: str) -> str:
        self._history.append({"role": "user", "content": task})
        try:
            text = await self._llm.complete(self._build_messages())
        except Exception:
            self._history.pop()
            raise
        self._history.append({"role": "assistant", "content": text})
        return text
