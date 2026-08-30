from __future__ import annotations

import asyncio
import inspect
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from protocol.commands import Command
from protocol.events import ChatMessageAdded, ErrorOccurred, Event, FileContent, SessionEnded, SnapshotReady
from agents.agent_loop import AgentLoop
from llm.openrouter import OpenRouterLLM
from protocol.snapshot import ChatMessage, EngineSnapshot, GitState
from runtime.commands import HANDLERS
from runtime.language import LanguageInfo, detect as detect_language
from runtime.store import SessionState
from runtime.store.sqlite import init as init_store
from runtime.store.sqlite import save as save_snapshot
from runtime.tools.fs import WorkspacePathError, list_tree, read_text
from runtime.tools.git import read_state as read_git
from tools.registry import discover_tools


class EngineSession:
    def __init__(self, workspace: Path, db_path: Path):
        self._workspace = workspace.resolve()
        self._db_path = db_path
        self._state = SessionState()
        self._subscribers: list[asyncio.Queue[Event]] = []
        self._llm: OpenRouterLLM | None = None
        self._loop: AgentLoop | None = None
        self.language: LanguageInfo = detect_language(self._workspace)
        try:
            self._llm = OpenRouterLLM.from_env(self._workspace)
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

    def subscribe(self) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue()
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        try:
            self._subscribers.remove(queue)
        except ValueError:
            pass

    def snapshot(self) -> EngineSnapshot:
        return self._state.snapshot(
            str(self._workspace),
            list_tree(self._workspace),
            read_git(self._workspace),
            language=self.language.name,
            language_supported=self.language.supported,
        )

    def shutdown(self) -> None:
        if not self.close_session():
            self._emit(ErrorOccurred(message="no active session; start one first"))

    def close_session(self) -> bool:
        if self._state.session_id is None:
            return False
        self._state.ended = True
        self._persist()
        self._emit(SessionEnded(reason="shutdown"))
        self._state = SessionState()
        self._loop = None
        return True

    def emit_error(self, message: str) -> None:
        self._emit(ErrorOccurred(message=message))

    def _bind_loop(self) -> None:
        if self._llm is None:
            self._loop = None
            return
        registry = discover_tools()
        for message in registry.errors:
            self._emit(ErrorOccurred(message=message))
        self._loop = AgentLoop(
            self._llm,
            tools=registry,
            workspace=self._workspace,
            on_tool=self._on_tool,
        )
        self._loop.hydrate(self._state.messages)

    def _on_tool(self, name: str, arguments: dict, result: str) -> None:
        preview = result if len(result) <= 400 else result[:400] + "…"
        self._emit(
            ChatMessageAdded(
                id=uuid4().hex,
                role="tool",
                text=f"{name}({json.dumps(arguments)})\n{preview}",
                ts=datetime.now(timezone.utc).isoformat(),
            )
        )

    def _emit_snapshot(self) -> None:
        self._drop_missing_open_files()
        self._emit(SnapshotReady(snapshot=self.snapshot()))
        for path in list(self._state.open_files):
            try:
                rel, content = read_text(self._workspace, path)
            except (FileNotFoundError, WorkspacePathError) as exc:
                self._emit(ErrorOccurred(message=f"{path}: {exc}"))
                continue
            self._emit(FileContent(path=rel, content=content))

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

    def _add_message(self, role: str, text: str) -> None:
        message = ChatMessage(
            id=uuid4().hex,
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
            queue.put_nowait(event)
