from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from protocol.commands import (
    ListSessions,
    RequestSnapshot,
    Shutdown,
    StartSession,
    SubmitUserMessage,
)
from protocol.events import ErrorOccurred, SessionList, WarningOccurred
from runtime.commands.register import handles
from runtime.store import SessionState
from runtime.store.sqlite import list_sessions, load as load_snapshot


@handles(StartSession)
def start_session(session, command: StartSession) -> None:
    incoming = Path(command.workspace).expanduser().resolve()
    if incoming != session._workspace:
        session._emit(
            ErrorOccurred(
                message=(
                    f"workspace mismatch: got {incoming}, "
                    f"expected {session._workspace}"
                )
            )
        )
        return

    session._persist()
    if command.session_id:
        loaded = load_snapshot(session._db_path, command.session_id)
        if loaded is None:
            session._emit(
                ErrorOccurred(message=f"unknown session: {command.session_id}")
            )
            return
        session._state = SessionState.from_snapshot(loaded)
    else:
        session._state = SessionState(session_id=uuid4().hex)
        session._persist()
    session._bind_loop()
    session._emit_snapshot()
    if session.language.warning:
        session._emit(WarningOccurred(message=session.language.warning))


@handles(ListSessions)
def list_stored_sessions(session, command: ListSessions) -> None:
    session._emit(SessionList(sessions=list_sessions(session._db_path)))


@handles(SubmitUserMessage)
async def submit_user_message(session, command: SubmitUserMessage) -> None:
    if not session._require_session():
        return
    if session._loop is None:
        session._emit(ErrorOccurred(message="set OPENROUTER_API_KEY"))
        return
    session._add_message(role="user", text=command.text)
    try:
        reply = await session._loop.run(command.text)
    except Exception as exc:
        session._emit(ErrorOccurred(message=f"llm error: {exc}"))
        return
    session._add_message(role="assistant", text=reply)
    session._persist()


@handles(RequestSnapshot)
def request_snapshot(session, command: RequestSnapshot) -> None:
    if not session._require_session():
        return
    session._emit_snapshot()


@handles(Shutdown)
def shutdown(session, command: Shutdown) -> None:
    session.shutdown()
