from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from llm.openrouter import OpenRouterLLM
from tools.base import ToolContext
from tools.registry import ToolRegistry

DEFAULT_SYSTEM = (
    "You are a coding assistant for this workspace. "
    "Do not guess file contents. Cheaper-first: search or list_files "
    "to locate a file, list_symbols to see what is in it, find_symbol "
    "for one definition's source. "
    "Use get_node_at and query_tree (presets: imports, functions, "
    "classes, methods, calls) for local syntax without waiting on LSP. "
    "Use parse_file only when nesting/shape matters and the outline "
    "is not enough. "
    "Feed a 1-based name position from find_symbol into "
    "goto_definition, find_references, or hover for cross-file and "
    "type questions. "
    "Use document_symbols when the sitter outline looks incomplete "
    "(interfaces, enums). "
    "Use get_diagnostics for type/lint issues. "
    "LSP tools work for python, go, and javascript/typescript; if a "
    "server is missing, fall back to sitter tools and read_file. "
    "read_file with offset/limit windows for surrounding context. "
    "Always read_file a path before editing it. Prefer str_replace "
    "with enough surrounding context that the match is unique; the "
    "tool refuses ambiguous matches instead of guessing. Use "
    "replace_lines for a window you already have open, apply_patch "
    "for larger structural changes, and replace_symbol / "
    "insert_after_imports for AST-scoped edits. Use rename_symbol "
    "instead of search-and-replace on identifiers. If an edit goes "
    "wrong, call undo_edit."
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
        language=None,
        lsp=None,
        files=None,
        journal=None,
        session_id: str | None = None,
        on_edit=None,
    ):
        self._llm = llm
        self._tools = tools or ToolRegistry()
        self._ctx = ToolContext(
            workspace=workspace or Path("."),
            language=language,
            lsp=lsp,
            files=files,
            journal=journal,
            session_id=session_id,
            on_edit=on_edit,
        )
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
