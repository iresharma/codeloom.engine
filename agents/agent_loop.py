from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from agents.compactor import compact, looks_like_overflow, read_context_md, validate_history
from agents.hooks import AgentHooks
from llm.openrouter import OpenRouterLLM
from llm.provider import Usage
from runtime.config import EngineConfig
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
    "wrong, call undo_edit. "
    "Use run_command for tests, builds, and linters; prefer running "
    "the test suite over asserting an edit is correct. A non-zero "
    "exit code is information, not a failure — read the output. "
    "Commands have no TTY and a 120s default timeout; keep them "
    "non-interactive. Some commands ask the user for approval; a "
    "denial is an answer, not a retry prompt. Old tool results get "
    "trimmed; re-run the tool rather than guessing at what it said."
)
MAX_TURNS = 16


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
        config: EngineConfig | None = None,
        hooks: AgentHooks | None = None,
        ask_user=None,
        on_output=None,
        on_proc=None,
    ):
        self._llm = llm
        self._tools = tools or ToolRegistry()
        self._config = config or EngineConfig()
        self._hooks = hooks or AgentHooks(on_tool=on_tool)
        if on_tool is not None and self._hooks.on_tool is None:
            self._hooks.on_tool = on_tool
        self._ctx = ToolContext(
            workspace=workspace or Path("."),
            language=language,
            lsp=lsp,
            files=files,
            journal=journal,
            session_id=session_id,
            on_edit=on_edit,
            config=self._config,
            ask_user=ask_user,
            on_output=on_output,
            on_proc=on_proc,
        )
        self._system_prompt = system_prompt
        self._on_tool = self._hooks.on_tool
        self._history: list[dict] = []
        self._usage = Usage()
        self._last_prompt_tokens = 0
        self._estimate_ratio = 1.0
        self._overflow_retried = False
        self._message_id = ""

    def hydrate(self, messages) -> None:
        self._history = []
        for message in messages:
            if message.role == "user":
                self._history.append({"role": "user", "content": message.text})
            elif message.role in ("assistant", "engine"):
                self._history.append({"role": "assistant", "content": message.text})

    def _build_messages(self) -> list[dict]:
        system = self._system_prompt
        notes = read_context_md(self._ctx.workspace)
        if notes:
            system = f"{system}\n\n## Workspace notes\n{notes}"
        return [{"role": "system", "content": system}] + list(self._history)

    def _state(self, state: str, turn: int = 0) -> None:
        if self._hooks.on_state is not None:
            self._hooks.on_state(state, turn, self._config.max_turns)

    async def run(self, task: str) -> str:
        marker = len(self._history)
        self._history.append({"role": "user", "content": task})
        schemas = self._tools.schemas()
        last_text = ""
        try:
            await self._maybe_compact()
            for turn in range(self._config.max_turns):
                self._state("thinking", turn + 1)
                result = await self._complete(self._build_messages(), schemas)
                if result.tool_calls:
                    await self._dispatch(result)
                    await self._maybe_compact()
                    continue
                last_text = result.text
                self._history.append({"role": "assistant", "content": last_text})
                return last_text
        except asyncio.CancelledError:
            # Truncate the in-flight exchange, then re-append the user
            # message and a single assistant abort notice so history never
            # holds an orphaned assistant tool_calls group.
            del self._history[marker:]
            self._history.append({"role": "user", "content": task})
            self._history.append(
                {"role": "assistant", "content": "(aborted by the user before completion)"}
            )
            raise
        except Exception:
            del self._history[marker:]
            raise
        return last_text or f"stopped after {self._config.max_turns} tool turns"

    async def _complete(self, messages: list[dict], schemas):
        self._message_id = uuid4().hex
        if self._hooks.on_message_start is not None:
            self._hooks.on_message_start(self._message_id)

        def on_delta(channel: str, text: str) -> None:
            if self._hooks.on_delta is not None:
                self._hooks.on_delta(self._message_id, channel, text)

        try:
            result = await self._llm.complete(
                messages, tools=schemas or None, on_delta=on_delta
            )
        except Exception as exc:
            if (
                not self._overflow_retried
                and looks_like_overflow(
                    str(exc), self._last_prompt_tokens, self._config.context_budget
                )
            ):
                self._overflow_retried = True
                self._config.context_budget = max(1, self._config.context_budget // 2)
                self._state("compacting")
                await self._maybe_compact(force=True)
                if self._hooks.on_compact is not None:
                    self._hooks.on_compact(
                        {
                            "strategy": "overflow-retry",
                            "messages_before": 0,
                            "messages_after": 0,
                            "chars_saved": 0,
                            "summary": f"budget halved to {self._config.context_budget}",
                        }
                    )
                return await self._complete(self._build_messages(), schemas)
            raise
        if result.usage is not None:
            if result.usage.prompt_tokens:
                estimated = max(1, len(str(messages)) // 4)
                self._estimate_ratio = result.usage.prompt_tokens / estimated
                self._last_prompt_tokens = result.usage.prompt_tokens
            self._usage += result.usage
            if self._hooks.on_usage is not None:
                self._hooks.on_usage(result.usage)
        return result

    async def _maybe_compact(self, force: bool = False) -> None:
        messages = self._build_messages()
        if not force:
            from agents.compactor import estimate_tokens, TRIGGER_RATIO

            estimated = int(estimate_tokens(messages) * (self._estimate_ratio or 1.0))
            if self._last_prompt_tokens:
                estimated = max(estimated, self._last_prompt_tokens)
            if estimated < int(self._config.context_budget * TRIGGER_RATIO):
                return
        self._state("compacting")

        async def complete(payload):
            return await self._llm.complete(payload)

        compacted, info = await compact(
            messages,
            self._config.context_budget,
            complete=complete,
            last_prompt_tokens=self._last_prompt_tokens,
            ratio=self._estimate_ratio,
        )
        if info.get("strategy") == "noop":
            return
        # compacted includes the system message; history is everything after.
        self._history = [item for item in compacted if item is not compacted[0]]
        if compacted and compacted[0].get("role") == "system":
            # Keep any injected summary as a second system message by
            # storing the full compacted list minus the first system prompt
            # that _build_messages will re-add.
            extras = [
                item
                for item in compacted[1:]
                if item.get("role") == "system"
            ]
            rest = [item for item in compacted[1:] if item.get("role") != "system"]
            self._history = extras + rest
        if self._hooks.on_compact is not None:
            self._hooks.on_compact(info)

    async def _dispatch(self, result) -> None:
        self._state("calling_tool")
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
            if self._hooks.on_tool_start is not None:
                self._hooks.on_tool_start(call.id, call.name, arguments)
            started = time.monotonic()
            try:
                output = await self._tools.execute(call.name, self._ctx, arguments)
                ok = not str(output).startswith("error:")
            except asyncio.CancelledError:
                if self._hooks.on_tool is not None:
                    self._hooks.on_tool(call.name, arguments, "cancelled")
                raise
            duration = int((time.monotonic() - started) * 1000)
            if self._hooks.on_tool is not None:
                self._hooks.on_tool(call.name, arguments, output)
            self._history.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": output,
                }
            )
            # duration/ok are observed by the session via on_tool; stored
            # here only so a future hook can read them if needed.
            _ = (ok, duration)
        errors = validate_history(self._history)
        if errors:
            raise RuntimeError("history pairing broken: " + "; ".join(errors))
