from __future__ import annotations

import asyncio
import inspect
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from protocol.commands import (
    CloseFile,
    Command,
    ListSessions,
    OpenFile,
    RequestGit,
    RequestSnapshot,
    Shutdown,
    StartSession,
    SubmitUserMessage,
)
from protocol.events import (
    ChatMessageAdded,
    ErrorOccurred,
    Event,
    FileClosed,
    FileContent,
    GitStateUpdated,
    SessionEnded,
    SessionList,
    SnapshotReady,
)
from agents.agent_loop import AgentLoop
from llm.openrouter import OpenRouterLLM
from protocol.snapshot import ChatMessage, EngineSnapshot, GitState
from runtime.fs import WorkspacePathError, list_tree, read_text, relative_posix, resolve_in_workspace
from runtime.git import read_state as read_git
from runtime.state import SessionState
from runtime.store import init as init_store
from runtime.store import list_sessions
from runtime.store import load as load_snapshot
from runtime.store import save as save_snapshot
from tools.registry import discover_tools


def handles(command_type: type):
    def deco(fn):
        fn._engine_command = command_type
        return fn

    return deco


class EngineSession:
    def __init__(self, workspace: Path, db_path: Path):
        self._workspace = workspace.resolve()
        self._db_path = db_path
        self._state = SessionState()
        self._subscribers: list[asyncio.Queue[Event]] = []
        self._llm: OpenRouterLLM | None = None
        self._loop: AgentLoop | None = None
        try:
            self._llm = OpenRouterLLM.from_env(self._workspace)
        except RuntimeError:
            self._llm = None

    async def start(self) -> None:
        init_store(self._db_path)

    @classmethod
    def _handlers(cls) -> dict:
        cached = getattr(cls, "_handler_map", None)
        if cached is None:
            mapping = {}
            for name in dir(cls):
                value = getattr(cls, name)
                command_type = getattr(value, "_engine_command", None)
                if command_type is not None:
                    mapping[command_type] = value
            cls._handler_map = mapping
            cached = mapping
        return cached

    async def handle(self, command: Command) -> None:
        fn = type(self)._handlers().get(type(command))
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

    @handles(StartSession)
    def _on_start_session(self, command: StartSession) -> None:
        incoming = Path(command.workspace).expanduser().resolve()
        if incoming != self._workspace:
            self._emit(
                ErrorOccurred(
                    message=(
                        f"workspace mismatch: got {incoming}, "
                        f"expected {self._workspace}"
                    )
                )
            )
            return

        self._persist()
        if command.session_id:
            loaded = load_snapshot(self._db_path, command.session_id)
            if loaded is None:
                self._emit(
                    ErrorOccurred(message=f"unknown session: {command.session_id}")
                )
                return
            self._state = SessionState.from_snapshot(loaded)
        else:
            self._state = SessionState(session_id=uuid4().hex)
            self._persist()
        self._bind_loop()
        self._emit_snapshot()

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

    @handles(ListSessions)
    def _on_list_sessions(self, command: ListSessions) -> None:
        self._emit(SessionList(sessions=list_sessions(self._db_path)))

    @handles(SubmitUserMessage)
    async def _on_user_message(self, command: SubmitUserMessage) -> None:
        if not self._require_session():
            return
        if self._loop is None:
            self._emit(ErrorOccurred(message="set OPENROUTER_API_KEY"))
            return
        self._add_message(role="user", text=command.text)
        try:
            reply = await self._loop.run(command.text)
        except Exception as exc:
            self._emit(ErrorOccurred(message=f"llm error: {exc}"))
            return
        self._add_message(role="assistant", text=reply)
        self._persist()

    @handles(RequestSnapshot)
    def _on_request_snapshot(self, command: RequestSnapshot) -> None:
        if not self._require_session():
            return
        self._emit_snapshot()

    @handles(OpenFile)
    def _on_open_file(self, command: OpenFile) -> None:
        if not self._require_session():
            return
        try:
            rel, content = read_text(self._workspace, command.path)
        except FileNotFoundError:
            self._emit(ErrorOccurred(message=f"file not found: {command.path}"))
            return
        except WorkspacePathError as exc:
            self._emit(ErrorOccurred(message=str(exc)))
            return
        if rel not in self._state.open_files:
            self._state.open_files.append(rel)
        self._persist()
        self._emit(FileContent(path=rel, content=content))

    @handles(CloseFile)
    def _on_close_file(self, command: CloseFile) -> None:
        if not self._require_session():
            return
        try:
            rel = relative_posix(
                self._workspace, resolve_in_workspace(self._workspace, command.path)
            )
        except WorkspacePathError as exc:
            self._emit(ErrorOccurred(message=str(exc)))
            return
        if rel not in self._state.open_files:
            self._emit(ErrorOccurred(message=f"file is not open: {command.path}"))
            return
        self._state.open_files.remove(rel)
        self._persist()
        self._emit(FileClosed(path=rel))

    @handles(RequestGit)
    def _on_request_git(self, command: RequestGit) -> None:
        if not self._require_session():
            return
        self._emit(GitStateUpdated(git=read_git(self._workspace)))

    @handles(Shutdown)
    def _on_shutdown(self, command: Shutdown) -> None:
        self.shutdown()

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
