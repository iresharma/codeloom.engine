from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from protocol.codec import STREAM_LIMIT, decode_event, encode
from protocol.commands import AnswerPrompt, StartSession, SubmitUserMessage
from protocol.events import (
    AgentFinished,
    AgentStarted,
    AgentStateChanged,
    AgentsUpdated,
    ErrorOccurred,
    SessionEnded,
    SnapshotReady,
    UserPromptRequested,
)
from runtime.tools.git import is_settle_prompt

TURN_CAP_CHOICES = frozenset({"continue", "handoff", "stop"})
SETTLE_ACTIONS = frozenset({"keep", "pr", "merge", "discard"})
QUIET_S = 1.0
# After a child finishes the orch still has to merge-gate + pump a follow-up
# turn. 1s of idle is shorter than a timed-out TypeSafe call (~800ms) plus
# intent classify, so the headless client would leave and the bench would
# SIGTERM the engine mid-turn ("aborted by the user").
POST_CHILD_QUIET_S = 8.0
Send = Callable[[object], Awaitable[None]]
GetEvent = Callable[[], Awaitable[object]]
OnEvent = Callable[[object], None]


class HeadlessError(RuntimeError):
    pass


def is_turn_cap_prompt(event: UserPromptRequested) -> bool:
    names = {str(choice).strip().lower() for choice in (event.choices or [])}
    return event.kind == "choice" and TURN_CAP_CHOICES <= names


def auto_answer(event: UserPromptRequested, *, settle: str = "keep") -> str:
    """Pick an unattended reply for a UserPromptRequested."""
    if is_settle_prompt(event.choices):
        action = (settle or "keep").strip().lower()
        return action if action in SETTLE_ACTIONS else "keep"
    if event.kind == "mcp_auth":
        return "no"
    if is_turn_cap_prompt(event):
        return "continue"
    return "yes"


def _live_agents(event: AgentsUpdated) -> int:
    return len(event.agents or [])


def _deadline_at(timeout: float | None) -> float | None:
    if timeout is None or timeout <= 0:
        return None
    return time.monotonic() + float(timeout)


async def wait_until_idle(
    get_event: GetEvent,
    send: Send,
    *,
    timeout: float | None = None,
    quiet_s: float = QUIET_S,
    post_child_quiet_s: float = POST_CHILD_QUIET_S,
    settle: str = "keep",
    on_event: OnEvent | None = None,
) -> None:
    """Auto-answer prompts and return after orch idle + no children + quiet_s.

    `timeout` <= 0 or None waits until idle with no wall-clock cap.
    After a child finishes, wait `post_child_quiet_s` for the orch to pump
    the report before treating the session as done.
    """
    deadline = _deadline_at(timeout)
    orch_state = "idle"
    live = 0
    pending = False
    saw_busy = False
    expect_pump = False
    idle_since: float | None = None

    def mark(busy: bool) -> None:
        nonlocal saw_busy, idle_since
        if busy:
            saw_busy = True
            idle_since = None
            return
        if saw_busy and idle_since is None:
            idle_since = time.monotonic()

    def refresh() -> None:
        mark(orch_state != "idle" or live > 0 or pending)

    def needed_quiet() -> float:
        return post_child_quiet_s if expect_pump else quiet_s

    while True:
        now = time.monotonic()
        if deadline is not None and now >= deadline:
            raise HeadlessError("timed out waiting for the session to go idle")
        event_timeout: float | None = None
        if idle_since is not None:
            remaining_quiet = needed_quiet() - (now - idle_since)
            if remaining_quiet <= 0:
                return
            event_timeout = remaining_quiet
        if deadline is not None:
            remaining_dead = deadline - now
            event_timeout = (
                remaining_dead
                if event_timeout is None
                else min(event_timeout, remaining_dead)
            )
        try:
            if event_timeout is None:
                event = await get_event()
            else:
                event = await asyncio.wait_for(
                    get_event(), timeout=max(0.01, event_timeout)
                )
        except asyncio.TimeoutError:
            continue
        if on_event is not None:
            on_event(event)
        if isinstance(event, UserPromptRequested):
            pending = True
            refresh()
            await send(
                AnswerPrompt(
                    prompt_id=event.prompt_id,
                    text=auto_answer(event, settle=settle),
                )
            )
            pending = False
            refresh()
            continue
        if isinstance(event, AgentStateChanged) and not event.agent_id:
            orch_state = event.state or "idle"
            if orch_state != "idle":
                expect_pump = False
            refresh()
        elif isinstance(event, AgentsUpdated):
            live = _live_agents(event)
            refresh()
        elif isinstance(event, AgentFinished):
            expect_pump = True
            mark(True)
            refresh()
        elif isinstance(event, AgentStarted):
            mark(True)
            refresh()
        elif isinstance(event, ErrorOccurred) and not saw_busy:
            raise HeadlessError(event.message)
        elif isinstance(event, SessionEnded):
            raise HeadlessError(f"session ended ({event.reason})")


async def drive_session(
    session,
    message: str,
    *,
    timeout: float | None = None,
    quiet_s: float = QUIET_S,
    settle: str = "keep",
    on_event: OnEvent | None = None,
) -> None:
    """In-process one-shot: submit `message` and wait until idle."""
    queue = session.subscribe()
    while not queue.empty():
        queue.get_nowait()

    async def send(command) -> None:
        await session.handle(command)

    async def get_event():
        return await queue.get()

    try:
        await send(SubmitUserMessage(text=message))
        await wait_until_idle(
            get_event,
            send,
            timeout=timeout,
            quiet_s=quiet_s,
            settle=settle,
            on_event=on_event,
        )
    finally:
        session.unsubscribe(queue)


async def run_once(
    workspace: Path,
    message: str,
    *,
    timeout: float | None = None,
    quiet_s: float = QUIET_S,
    settle: str = "keep",
    on_event: OnEvent | None = None,
) -> None:
    """Socket one-shot against `{workspace}/.engine/engine.sock`."""
    socket_path = workspace / ".engine" / "engine.sock"
    reader, writer = await asyncio.open_unix_connection(
        str(socket_path), limit=STREAM_LIMIT
    )

    async def send(command) -> None:
        writer.write(encode(command))
        await writer.drain()

    async def get_event():
        line = await reader.readline()
        if not line:
            raise HeadlessError("disconnected")
        return decode_event(line)

    submitted = False
    try:
        await send(StartSession(workspace=str(workspace)))
        deadline = _deadline_at(timeout)
        while not submitted:
            if deadline is None:
                event = await get_event()
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise HeadlessError("timed out waiting for SnapshotReady")
                event = await asyncio.wait_for(get_event(), timeout=remaining)
            if on_event is not None:
                on_event(event)
            if isinstance(event, ErrorOccurred):
                raise HeadlessError(event.message)
            if isinstance(event, SnapshotReady):
                await send(SubmitUserMessage(text=message))
                submitted = True
                break
        idle_timeout = None if deadline is None else deadline - time.monotonic()
        if idle_timeout is not None and idle_timeout <= 0:
            raise HeadlessError("timed out waiting for the session to go idle")
        await wait_until_idle(
            get_event,
            send,
            timeout=idle_timeout,
            quiet_s=quiet_s,
            settle=settle,
            on_event=on_event,
        )
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, ConnectionError):
            pass
