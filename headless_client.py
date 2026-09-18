from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from protocol.codec import STREAM_LIMIT, decode_event, encode
from protocol.commands import AnswerPrompt, StartSession, SubmitUserMessage
from protocol.events import (
    AgentStateChanged,
    AgentsUpdated,
    ErrorOccurred,
    SessionEnded,
    SnapshotReady,
    UserPromptRequested,
)
from runtime.tools.git import is_settle_prompt

TURN_CAP_CHOICES = frozenset({"continue", "handoff", "stop"})
QUIET_S = 1.0
Send = Callable[[object], Awaitable[None]]
GetEvent = Callable[[], Awaitable[object]]


class HeadlessError(RuntimeError):
    pass


def is_turn_cap_prompt(event: UserPromptRequested) -> bool:
    names = {str(choice).strip().lower() for choice in (event.choices or [])}
    return event.kind == "choice" and TURN_CAP_CHOICES <= names


def auto_answer(event: UserPromptRequested) -> str:
    """Pick an unattended reply for a UserPromptRequested."""
    if is_settle_prompt(event.choices):
        return "keep"
    if event.kind == "mcp_auth":
        return "no"
    if is_turn_cap_prompt(event):
        return "continue"
    return "yes"


def _live_agents(event: AgentsUpdated) -> int:
    return len(event.agents or [])


async def wait_until_idle(
    get_event: GetEvent,
    send: Send,
    *,
    timeout: float,
    quiet_s: float = QUIET_S,
) -> None:
    """Auto-answer prompts and return after orch idle + no children + quiet_s."""
    deadline = time.monotonic() + max(0.1, float(timeout))
    orch_state = "idle"
    live = 0
    pending = False
    saw_busy = False
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

    while True:
        now = time.monotonic()
        if now >= deadline:
            raise HeadlessError("timed out waiting for the session to go idle")
        wait = deadline - now
        if idle_since is not None:
            remaining_quiet = quiet_s - (now - idle_since)
            if remaining_quiet <= 0:
                return
            wait = min(wait, remaining_quiet)
        try:
            event = await asyncio.wait_for(get_event(), timeout=max(0.01, wait))
        except asyncio.TimeoutError:
            continue
        if isinstance(event, UserPromptRequested):
            pending = True
            refresh()
            await send(
                AnswerPrompt(prompt_id=event.prompt_id, text=auto_answer(event))
            )
            pending = False
            refresh()
            continue
        if isinstance(event, AgentStateChanged) and not event.agent_id:
            orch_state = event.state or "idle"
            refresh()
        elif isinstance(event, AgentsUpdated):
            live = _live_agents(event)
            refresh()
        elif isinstance(event, ErrorOccurred) and not saw_busy:
            raise HeadlessError(event.message)
        elif isinstance(event, SessionEnded):
            raise HeadlessError(f"session ended ({event.reason})")


async def drive_session(
    session,
    message: str,
    *,
    timeout: float = 1800.0,
    quiet_s: float = QUIET_S,
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
            get_event, send, timeout=timeout, quiet_s=quiet_s
        )
    finally:
        session.unsubscribe(queue)


async def run_once(
    workspace: Path,
    message: str,
    *,
    timeout: float = 1800.0,
    quiet_s: float = QUIET_S,
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
        deadline = time.monotonic() + max(0.1, float(timeout))
        while not submitted:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HeadlessError("timed out waiting for SnapshotReady")
            event = await asyncio.wait_for(get_event(), timeout=remaining)
            if isinstance(event, ErrorOccurred):
                raise HeadlessError(event.message)
            if isinstance(event, SnapshotReady):
                await send(SubmitUserMessage(text=message))
                submitted = True
                break
        await wait_until_idle(
            get_event, send, timeout=max(0.1, deadline - time.monotonic()),
            quiet_s=quiet_s,
        )
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, ConnectionError):
            pass
