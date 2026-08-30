from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from protocol.commands import (
    CloseFile,
    Command,
    ListSessions,
    OpenFile,
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
    SessionEnded,
    SessionList,
    SnapshotReady,
)
from protocol.snapshot import ChatMessage, EngineSnapshot
from runtime.fs import WorkspacePathError, list_tree, read_text, relative_posix, resolve_in_workspace
from runtime.state import SessionState
from runtime.store import init as init_store
from runtime.store import list_sessions
from runtime.store import load as load_snapshot
from runtime.store import save as save_snapshot


class EngineSession:
    def __init__(self, workspace: Path, db_path: Path):
        self._workspace = workspace.resolve()
        self._db_path = db_path
        self._state = SessionState()
        self._subscribers: list[asyncio.Queue[Event]] = []

    async def start(self) -> None:
        init_store(self._db_path)

    async def handle(self, command: Command) -> None:
        if isinstance(command, StartSession):
            self._on_start_session(command.workspace, command.session_id)
        elif isinstance(command, ListSessions):
            self._emit(SessionList(sessions=list_sessions(self._db_path)))
        elif isinstance(command, SubmitUserMessage):
            self._on_user_message(command.text)
        elif isinstance(command, RequestSnapshot):
            if not self._require_session():
                return
            self._emit_snapshot()
        elif isinstance(command, OpenFile):
            self._on_open_file(command.path)
        elif isinstance(command, CloseFile):
            self._on_close_file(command.path)
        elif isinstance(command, Shutdown):
            self.shutdown()
        else:
            self._emit(
                ErrorOccurred(message=f"unknown command: {type(command).__name__}")
            )

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
        return self._state.snapshot(str(self._workspace), list_tree(self._workspace))

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
        return True

    def emit_error(self, message: str) -> None:
        self._emit(ErrorOccurred(message=message))

    def _on_start_session(self, workspace: str, session_id: str | None) -> None:
        incoming = Path(workspace).expanduser().resolve()
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
        if session_id:
            loaded = load_snapshot(self._db_path, session_id)
            if loaded is None:
                self._emit(
                    ErrorOccurred(message=f"unknown session: {session_id}")
                )
                return
            self._state = SessionState.from_snapshot(loaded)
        else:
            self._state = SessionState(session_id=uuid4().hex)
            self._persist()
        self._emit_snapshot()

    def _on_user_message(self, text: str) -> None:
        if not self._require_session():
            return
        self._add_message(role="user", text=text)
        self._add_message(role="engine", text=f"ack: {text}")

    def _on_open_file(self, path: str) -> None:
        if not self._require_session():
            return
        try:
            rel, content = read_text(self._workspace, path)
        except FileNotFoundError:
            self._emit(ErrorOccurred(message=f"file not found: {path}"))
            return
        except WorkspacePathError as exc:
            self._emit(ErrorOccurred(message=str(exc)))
            return
        if rel not in self._state.open_files:
            self._state.open_files.append(rel)
        self._persist()
        self._emit(FileContent(path=rel, content=content))

    def _on_close_file(self, path: str) -> None:
        if not self._require_session():
            return
        try:
            rel = relative_posix(
                self._workspace, resolve_in_workspace(self._workspace, path)
            )
        except WorkspacePathError as exc:
            self._emit(ErrorOccurred(message=str(exc)))
            return
        if rel not in self._state.open_files:
            self._emit(ErrorOccurred(message=f"file is not open: {path}"))
            return
        self._state.open_files.remove(rel)
        self._persist()
        self._emit(FileClosed(path=rel))

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
        save_snapshot(self._db_path, self.snapshot())

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
