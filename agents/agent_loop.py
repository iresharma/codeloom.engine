from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from llm.openrouter import OpenRouterLLM
from tools.base import ToolContext
from tools.registry import ToolRegistry

DEFAULT_SYSTEM = (
    "You are a coding assistant for this workspace. "
    "Use search to find code, then read_file with offset/limit windows. "
    "Do not guess file contents."
)
MAX_TURNS = 8


class AgentLoop:
    def __init__(
        self,
        llm: OpenRouterLLM,
        tools: ToolRegistry | None = None,
        workspace: Path | None = None,
        system_prompt: str = DEFAULT_SYSTEM,
        on_tool: Callable[[str, dict, str], None] | None = None,
    ):
        self._llm = llm
        self._tools = tools or ToolRegistry()
        self._ctx = ToolContext(workspace=workspace or Path("."))
        self._system_prompt = system_prompt
        self._on_tool = on_tool
        self._history: list[dict] = []

    def hydrate(self, messages) -> None:
        self._history = []
        for message in messages:
            if message.role == "user":
                self._history.append({"role": "user", "content": message.text})
            elif message.role in ("assistant", "engine"):
                self._history.append({"role": "assistant", "content": message.text})

    def _build_messages(self) -> list[dict]:
        return [{"role": "system", "content": self._system_prompt}] + list(
            self._history
        )

    async def run(self, task: str) -> str:
        marker = len(self._history)
        self._history.append({"role": "user", "content": task})
        schemas = self._tools.schemas()
        last_text = ""
        try:
            for _ in range(MAX_TURNS):
                result = await self._llm.complete(
                    self._build_messages(),
                    tools=schemas or None,
                )
                if result.tool_calls:
                    await self._dispatch(result)
                    continue
                last_text = result.text
                self._history.append({"role": "assistant", "content": last_text})
                return last_text
        except Exception:
            del self._history[marker:]
            raise
        return last_text or f"stopped after {MAX_TURNS} tool turns"

    async def _dispatch(self, result) -> None:
        self._history.append(
            {
                "role": "assistant",
                "content": result.text or None,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": call.arguments_json,
                        },
                    }
                    for call in result.tool_calls
                ],
            }
        )
        for call in result.tool_calls:
            arguments = call.arguments()
            output = await self._tools.execute(call.name, self._ctx, arguments)
            if self._on_tool is not None:
                self._on_tool(call.name, arguments, output)
            self._history.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": output,
                }
            )
