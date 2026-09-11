from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from protocol.commands import (
    ListSessions,
    RequestAgentTranscript,
    RequestContext,
    RequestMemory,
    RequestOrchContext,
    RequestSnapshot,
    Shutdown,
    StartSession,
    SubmitUserMessage,
)
from protocol.events import (
    ChatHistoryAdded,
    ChatHistoryComplete,
    ErrorOccurred,
    OrchContext,
    SessionList,
    WarningOccurred,
)
from runtime.commands.register import handles
from runtime.store import SessionState
from runtime.store.sqlite import list_sessions
from runtime.store.sqlite import load as load_snapshot
from runtime.tools.git import is_settle_prompt, parse_settle_intent


@handles(StartSession)
async def start_session(session, command: StartSession) -> None:
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
    await session._bind_loop()
    session._emit_snapshot()
    if session.language.warning:
        session._emit(WarningOccurred(message=session.language.warning))


@handles(ListSessions)
def list_stored_sessions(session, command: ListSessions) -> None:
    session._emit(SessionList(sessions=list_sessions(session._db_path)))


@handles(SubmitUserMessage)
def submit_user_message(session, command: SubmitUserMessage) -> None:
    if not session._require_session():
        return
    pending = session._prompts.pending()
    if pending is not None:
        if is_settle_prompt(pending.choices):
            intent = parse_settle_intent(command.text)
            if intent is None:
                if session._loop is None:
                    session._emit(ErrorOccurred(message="set OPENROUTER_API_KEY"))
                    return
                session.start_turn(command.text)
                return
            session._prompts.answer(pending.prompt_id, intent)
            return
        session._prompts.answer(pending.prompt_id, command.text)
        return
    if session._loop is None:
        session._emit(ErrorOccurred(message="set OPENROUTER_API_KEY"))
        return
    session.start_turn(command.text)


@handles(RequestSnapshot)
def request_snapshot(session, command: RequestSnapshot) -> None:
    if not session._require_session():
        return
    session._emit_snapshot(replay=command.replay)


@handles(RequestOrchContext)
def request_orch_context(session, command: RequestOrchContext) -> None:
    if not session._require_session():
        return
    if session._loop is None:
        session._emit(ErrorOccurred(message="set OPENROUTER_API_KEY"))
        return
    from runtime.subscriber import EVENT_SOFT_LIMIT, clip_text

    text, _ = clip_text(session._loop.context_dump(), EVENT_SOFT_LIMIT)
    session._emit(OrchContext(text=text))


@handles(RequestContext)
def request_context(session, command: RequestContext) -> None:
    if not session._require_session():
        return
    if session._loop is None:
        session._emit(ErrorOccurred(message="set OPENROUTER_API_KEY"))
        return
    agent_id = command.agent_id or ""
    breakdown = session._loop.breakdown_for(agent_id)
    if breakdown is None:
        session._emit(ErrorOccurred(message=f"unknown agent: {agent_id}"))
        return
    from runtime.subscriber import EVENT_SOFT_LIMIT, clip_text

    for section in breakdown.sections:
        clipped, _ = clip_text(section.text, EVENT_SOFT_LIMIT)
        section.text = clipped
        section.chars = len(clipped)
        section.tokens_est = max(0, len(clipped) // 4)
    session._emit(breakdown)


@handles(RequestMemory)
def request_memory(session, command: RequestMemory) -> None:
    if not session._require_session():
        return
    session._emit_memory()


@handles(RequestAgentTranscript)
def request_agent_transcript(session, command: RequestAgentTranscript) -> None:
    if not session._require_session():
        return
    if session._loop is None:
        session._emit(ErrorOccurred(message="set OPENROUTER_API_KEY"))
        return
    agent_id = command.agent_id or ""
    if not agent_id:
        session._emit(ErrorOccurred(message="agent_id is required"))
        return
    lines = session._loop.transcript_for(agent_id)
    if lines is None:
        session._emit(ErrorOccurred(message=f"unknown agent: {agent_id}"))
        return
    total = len(lines)
    if total == 0:
        session._emit(ChatHistoryComplete(count=0, agent_id=agent_id))
        return
    from datetime import datetime, timezone
    from uuid import uuid4

    ts = datetime.now(timezone.utc).isoformat()
    for index, line in enumerate(lines):
        session._emit(
            ChatHistoryAdded(
                id=uuid4().hex,
                role=str(line.get("role") or "assistant"),
                text=str(line.get("text") or ""),
                ts=ts,
                index=index,
                total=total,
                agent_id=agent_id,
            )
        )
    session._emit(ChatHistoryComplete(count=total, agent_id=agent_id))


@handles(Shutdown)
async def shutdown(session, command: Shutdown) -> None:
    await session.aclose()
