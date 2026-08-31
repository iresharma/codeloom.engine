from __future__ import annotations

import asyncio
import inspect
import threading
import time
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from agents.agent_loop import AgentLoop
from agents.hooks import AgentHooks
from llm.openrouter import OpenRouterLLM
from llm.provider import Usage
from protocol.commands import Command
from protocol.events import (
    AgentStateChanged,
    ChatHistoryAdded,
    ChatHistoryComplete,
    ChatMessageAdded,
    ChatMessageDelta,
    ChatMessageStarted,
    CommandOutputChunk,
    ContextCompacted,
    ErrorOccurred,
    Event,
    FileContent,
    FileEdited,
    FileTreeUpdated,
    SessionEnded,
    SnapshotReady,
    StatsUpdated,
    ToolCallFinished,
    ToolCallStarted,
    WarningOccurred,
)
from protocol.snapshot import ChatMessage, EngineSnapshot, GitState, Stats
from runtime.commands import HANDLERS
from runtime.config import EngineConfig
from runtime.language import LanguageInfo
from runtime.language import detect as detect_language
from runtime.prompts import PromptBroker
from runtime.store import SessionState
from runtime.store.sqlite import init as init_store
from runtime.store.sqlite import save as save_snapshot
from runtime.subscriber import EVENT_SOFT_LIMIT, Subscriber, clip_text
from runtime.tools.fs import WorkspacePathError, list_tree, read_text
from runtime.tools.git import read_state as read_git
from runtime.tools.lsp import LSPManager, LSPTimeoutError
from runtime.tools.tracker import FileTracker
from tools.registry import discover_tools


class EngineSession:
    def __init__(self, workspace: Path, db_path: Path):
        self._workspace = workspace.resolve()
        self._db_path = db_path
        self._state = SessionState()
        self._subscribers: list[Subscriber] = []
        self._config = EngineConfig.from_env(self._workspace)
        self._llm: OpenRouterLLM | None = None
        self._loop: AgentLoop | None = None
        self._lsp: LSPManager | None = None
        self._files = FileTracker()
        self._history_task: asyncio.Task | None = None
        self._history_generation = 0
        self._turn_task: asyncio.Task | None = None
        self._aborting = False
        self._turn_started = 0.0
        self._live_procs: set = set()
        self._prompts = PromptBroker(
            self._emit, self._on_prompt_state, self._set_pending_prompt
        )
        self._last_call_id = ""
        self._tool_started: dict[str, float] = {}
        self._stream_id = ""
        self._streamed_ids: set[str] = set()
        self.language: LanguageInfo = detect_language(self._workspace)
        try:
            self._llm = OpenRouterLLM.from_env(self._workspace, config=self._config)
        except RuntimeError:
            self._llm = None

    async def start(self) -> None:
        init_store(self._db_path)

    async def handle(self, command: Command) -> None:
        fn = HANDLERS.get(type(command))
        if fn is None:
            self._emit(
                ErrorOccurred(message=f"unknown command: {type(command).__name__}")
            )
            return
        result = fn(self, command)
        if inspect.isawaitable(result):
            await result

    def subscribe(self) -> Subscriber:
        queue = Subscriber(
            capacity=self._config.subscriber_capacity,
            max_bytes=self._config.subscriber_bytes,
        )
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: Subscriber) -> None:
        try:
            self._subscribers.remove(queue)
        except ValueError:
            pass

    def snapshot(self) -> EngineSnapshot:
        tree = list_tree(self._workspace)
        snap = self._state.snapshot(
            str(self._workspace),
            tree,
            read_git(self._workspace, diffs=False),
            language=self.language.name,
            language_supported=self.language.supported,
        )
        return snap

    def shutdown(self) -> None:
        if not self.close_session():
            self._emit(ErrorOccurred(message="no active session; start one first"))

    async def aclose(self) -> None:
        self.abort_turn()
        task = self._turn_task
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        self.close_session()

    def close_session(self) -> bool:
        if self._state.session_id is None:
            return False
        self.abort_turn()
        self._cancel_history_replay()
        self._state.ended = True
        self._persist()
        self._emit(SessionEnded(reason="shutdown"))
        self._state = SessionState()
        self._loop = None
        self._stop_lsp()
        return True

    def emit_error(self, message: str) -> None:
        self._emit(ErrorOccurred(message=message))

    def start_turn(self, text: str) -> None:
        if self._turn_task is not None and not self._turn_task.done():
            self._emit(ErrorOccurred(message="agent is busy; send AbortAgent first"))
            return
        self._add_message(role="user", text=text)
        self._persist()
        self._state.stats.last_turn_tokens = 0
        self._state.stats.last_turn_cost = 0.0
        self._turn_started = time.monotonic()
        self._aborting = False
        self._turn_task = asyncio.get_running_loop().create_task(self._run_turn(text))

    async def _run_turn(self, text: str) -> None:
        # CancelledError is swallowed: this task is the cancellation
        # boundary. After an abort, task.cancelled() is False and
        # exception() is None. Use self._aborting, not the task flags.
        try:
            reply = await self._loop.run(text)
            if not self._aborting:
                # Same id as ChatMessageStarted/Delta so clients that
                # already rendered the stream do not reprint the text.
                self._add_message(
                    role="assistant",
                    text=reply,
                    message_id=self._stream_id or None,
                )
        except asyncio.CancelledError:
            self._add_message(role="assistant", text="(aborted by the user)")
        except Exception as exc:  # noqa: BLE001
            self._emit(ErrorOccurred(message=f"llm error: {exc}"))
        finally:
            self._state.stats.elapsed_s += max(0.0, time.monotonic() - self._turn_started)
            self._persist()
            self._turn_task = None
            self._aborting = False
            self._emit_stats()
            self._on_state("idle", 0)

    def abort_turn(self) -> bool:
        task = self._turn_task
        if task is None or task.done():
            return False
        self._aborting = True
        self._on_state("aborting", 0)
        self._prompts.cancel_all()
        for proc in list(self._live_procs):
            with suppress(ProcessLookupError, OSError):
                import os
                import signal

                pid = getattr(proc, "pid", None)
                if pid is not None:
                    os.killpg(os.getpgid(pid), signal.SIGTERM)
        # abort_turn returning True means cancellation was requested, not
        # that the turn has already stopped.
        task.cancel()
        return True

    def _bind_loop(self) -> None:
        if self._llm is None:
            self._loop = None
            return
        registry = discover_tools()
        for message in registry.errors:
            self._emit(ErrorOccurred(message=message))
        for warning in self._config.warnings:
            self._emit(WarningOccurred(message=warning))
        self._start_lsp()
        self._files = FileTracker()
        hooks = AgentHooks(
            on_tool=self._on_tool,
            on_tool_start=self._on_tool_start,
            on_delta=self._on_delta,
            on_message_start=self._on_message_start,
            on_usage=self._on_usage,
            on_state=self._on_state,
            on_compact=self._on_compact,
        )
        self._loop = AgentLoop(
            self._llm,
            tools=registry,
            workspace=self._workspace,
            hooks=hooks,
            language=self.language,
            lsp=self._lsp,
            files=self._files,
            journal=self._db_path,
            session_id=self._state.session_id,
            on_edit=self._on_edit,
            config=self._config,
            ask_user=self._prompts.ask,
            on_output=self._on_command_output,
            on_proc=self._on_proc,
        )
        self._loop.hydrate(self._state.messages)

    def _set_pending_prompt(self, prompt) -> None:
        self._state.pending_prompt = prompt

    def _on_prompt_state(self, state: str) -> None:
        self._state.pending_prompt = self._prompts.pending()
        self._on_state(state, 0)

    def _on_proc(self, proc, register: bool) -> None:
        if register:
            self._live_procs.add(proc)
        else:
            self._live_procs.discard(proc)

    def _on_command_output(self, call_id: str, stream: str, text: str) -> None:
        self._emit(CommandOutputChunk(call_id=call_id or "", stream=stream, text=text))

    def _start_lsp(self) -> None:
        self._stop_lsp()
        if not self.language.supported:
            return
        self._lsp = LSPManager(self._workspace)
        threading.Thread(
            target=self._warm_lsp,
            daemon=True,
            name="lsp-warm-start",
        ).start()

    def _warm_lsp(self) -> None:
        manager = self._lsp
        name = self.language.name
        if manager is None or not name:
            return
        with suppress(OSError, RuntimeError, ValueError, LSPTimeoutError):
            manager.warm_start(name)

    def _stop_lsp(self) -> None:
        if self._lsp is None:
            return
        with suppress(OSError, RuntimeError, LSPTimeoutError):
            self._lsp.shutdown_all()
        self._lsp = None

    def _on_edit(self, path: str, diff: str, tool: str, edit_id) -> None:
        clipped, _ = clip_text(diff, EVENT_SOFT_LIMIT)
        self._emit(
            FileEdited(
                path=path,
                diff=clipped,
                tool=tool,
                edit_id=str(edit_id) if edit_id is not None else "",
            )
        )
        if path in self._state.open_files:
            self._emit_file_content(path)
        if tool in {"create_file", "undo_edit"}:
            self._emit_tree()

    def _on_tool_start(self, call_id: str, name: str, arguments: dict) -> None:
        self._tool_started[call_id] = time.monotonic()
        self._last_call_id = call_id
        import json

        self._emit(
            ToolCallStarted(
                call_id=call_id,
                name=name,
                arguments_json=json.dumps(arguments),
            )
        )

    def _on_tool(self, name: str, arguments: dict, result: str) -> None:
        preview = result if len(result) <= 400 else result[:400] + "…"
        call_id = getattr(self, "_last_call_id", "")
        started = self._tool_started.pop(call_id, 0)
        duration = int((time.monotonic() - started) * 1000) if started else 0
        self._emit(
            ToolCallFinished(
                call_id=call_id,
                name=name,
                preview=preview,
                ok=not str(result).startswith("error:"),
                duration_ms=duration,
            )
        )
        self._state.stats.tool_calls += 1

    def _on_message_start(self, message_id: str) -> None:
        self._stream_id = message_id
        self._emit(
            ChatMessageStarted(
                id=message_id,
                role="assistant",
                ts=datetime.now(timezone.utc).isoformat(),
            )
        )

    def _on_delta(self, message_id: str, channel: str, text: str) -> None:
        self._streamed_ids.add(message_id)
        self._emit(ChatMessageDelta(id=message_id, channel=channel, text=text))

    def _on_usage(self, usage: Usage) -> None:
        stats = self._state.stats
        stats.prompt_tokens += usage.prompt_tokens
        stats.completion_tokens += usage.completion_tokens
        stats.reasoning_tokens += usage.reasoning_tokens
        stats.cached_tokens += usage.cached_tokens
        stats.total_tokens += usage.total_tokens
        stats.cost += usage.cost
        stats.requests += usage.requests or 1
        stats.turns += 1
        stats.last_turn_tokens += usage.total_tokens
        stats.last_turn_cost += usage.cost
        self._emit_stats()

    def _on_state(self, state: str, turn: int = 0, max_turns: int | None = None) -> None:
        self._emit(
            AgentStateChanged(
                state=state,
                turn=turn,
                max_turns=self._config.max_turns if max_turns is None else max_turns,
            )
        )

    def _on_compact(self, info: dict) -> None:
        self._emit(
            ContextCompacted(
                strategy=str(info.get("strategy") or ""),
                messages_before=int(info.get("messages_before") or 0),
                messages_after=int(info.get("messages_after") or 0),
                chars_saved=int(info.get("chars_saved") or 0),
                summary=str(info.get("summary") or "")[:400],
            )
        )
        if info.get("strategy") == "overflow-retry":
            self._emit(
                WarningOccurred(
                    message=f"context budget reduced to {self._config.context_budget}"
                )
            )

    def _emit_stats(self) -> None:
        self._emit(StatsUpdated(stats=self._state.stats))

    def _cancel_history_replay(self) -> None:
        self._history_generation += 1
        task = self._history_task
        self._history_task = None
        if task is not None and not task.done():
            task.cancel()

    def _emit_snapshot(self) -> None:
        self._drop_missing_open_files()
        self._cancel_history_replay()
        snap = self.snapshot()
        history = list(snap.messages)
        tree = list(snap.file_tree)
        snap.messages = []
        snap.message_count = len(history)
        snap.file_tree_count = snap.file_tree_count or _count_tree(tree)
        snap.file_tree = []
        self._emit(SnapshotReady(snapshot=snap))
        self._emit_tree(tree)
        for path in list(self._state.open_files):
            self._emit_file_content(path)
        if not history:
            self._emit(ChatHistoryComplete(count=0))
            return
        generation = self._history_generation
        self._history_task = asyncio.get_running_loop().create_task(
            self._replay_history(history, generation)
        )

    def _emit_tree(self, tree=None) -> None:
        nodes = list(tree) if tree is not None else list_tree(self._workspace)
        if _approx_tree(nodes) > EVENT_SOFT_LIMIT:
            shallow = [
                type(node)(name=node.name, path=node.path, is_dir=node.is_dir, children=None)
                for node in nodes
            ]
            omitted = _count_tree(nodes) - len(shallow)
            self._emit(FileTreeUpdated(file_tree=shallow))
            self._emit(
                WarningOccurred(
                    message=f"file tree truncated; {omitted} entries omitted"
                )
            )
            return
        self._emit(FileTreeUpdated(file_tree=nodes))

    def _emit_file_content(self, path: str) -> None:
        try:
            rel, content = read_text(self._workspace, path)
        except (FileNotFoundError, WorkspacePathError) as exc:
            self._emit(ErrorOccurred(message=f"{path}: {exc}"))
            return
        clipped, omitted = clip_text(content, EVENT_SOFT_LIMIT)
        if omitted:
            clipped += "; use read_file"
            self._emit(
                WarningOccurred(
                    message=f"{rel} truncated; {omitted} bytes omitted"
                )
            )
        self._emit(FileContent(path=rel, content=clipped))

    async def _replay_history(self, history, generation: int) -> None:
        total = len(history)
        try:
            for index, message in enumerate(history):
                if generation != self._history_generation:
                    return
                self._emit(
                    ChatHistoryAdded(
                        id=message.id,
                        role=message.role,
                        text=message.text,
                        ts=message.ts,
                        index=index,
                        total=total,
                    )
                )
                await asyncio.sleep(0)
            if generation == self._history_generation:
                self._emit(ChatHistoryComplete(count=total))
        except asyncio.CancelledError:
            return

    def _drop_missing_open_files(self) -> None:
        kept = []
        for path in self._state.open_files:
            try:
                rel, _content = read_text(self._workspace, path)
            except (FileNotFoundError, WorkspacePathError):
                continue
            kept.append(rel)
        self._state.open_files = kept

    def _require_session(self) -> bool:
        if self._state.session_id is None:
            self._emit(ErrorOccurred(message="no active session; start one first"))
            return False
        return True

    def _persist(self) -> None:
        if self._state.session_id is None:
            return
        save_snapshot(
            self._db_path,
            self._state.snapshot(str(self._workspace), [], GitState.empty()),
        )

    def _add_message(
        self, role: str, text: str, *, message_id: str | None = None
    ) -> None:
        message = ChatMessage(
            id=message_id or uuid4().hex,
            role=role,
            text=text,
            ts=datetime.now(timezone.utc).isoformat(),
        )
        self._state.messages.append(message)
        self._emit(
            ChatMessageAdded(
                id=message.id,
                role=message.role,
                text=message.text,
                ts=message.ts,
            )
        )

    def _emit(self, event: Event) -> None:
        for queue in list(self._subscribers):
            queue.put(event)


def _count_tree(nodes) -> int:
    total = 0
    for node in nodes:
        total += 1
        if node.children:
            total += _count_tree(node.children)
    return total


def _approx_tree(nodes) -> int:
    total = 0
    for node in nodes:
        total += len(node.name) + len(node.path)
        if node.children:
            total += _approx_tree(node.children)
    return total
