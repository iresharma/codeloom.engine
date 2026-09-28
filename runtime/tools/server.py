from __future__ import annotations

import asyncio
import os
import signal
import socket
import time
from contextlib import suppress
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from runtime.tools.fs import WorkspacePathError, resolve_in_workspace
from runtime.tools.shell import (
    _auto_allowed,
    _hard_deny,
    _kill_group,
    file_limit_blocks,
)

READY_TIMEOUT = 30
LOG_LINES = 200
RING_CHARS = 80_000


@dataclass
class ManagedServer:
    proc: asyncio.subprocess.Process
    port: int
    workspace: Path
    command: str
    pgid: int = 0
    logs: deque[str] = field(default_factory=lambda: deque(maxlen=LOG_LINES))
    drains: list[asyncio.Task] = field(default_factory=list)
    on_proc: Callable[[object, bool], None] | None = None


_lock = asyncio.Lock()
_servers: dict[str, ManagedServer] = {}


def _key(workspace: Path) -> str:
    return str(workspace.resolve())


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def _join_logs(managed: ManagedServer) -> str:
    text = "".join(managed.logs)
    if len(text) <= RING_CHARS:
        return text
    return text[-RING_CHARS:]


async def start_server(
    workspace: Path,
    command: str,
    port: int = 0,
    cwd: str = "",
    *,
    approve: Callable[[str, str], Awaitable[bool]] | None = None,
    approval: str = "auto",
    file_limit_mb: int = 2048,
    on_proc: Callable[[object, bool], None] | None = None,
    on_output: Callable[[str, str], None] | None = None,
) -> str:
    if not command or not command.strip():
        raise ValueError("command is required")
    stripped = command.strip()
    _hard_deny(stripped)
    if not _auto_allowed(stripped, approval):
        if approval == "never":
            pass
        elif approve is None:
            raise RuntimeError("command not approved by the user")
        else:
            allowed = await approve(
                f"Allow this command?\n{stripped}",
                "confirm",
            )
            if str(allowed).strip().lower() not in {"yes", "y", "true", "allow"}:
                raise RuntimeError("command not approved by the user")

    workspace = workspace.resolve()
    workdir = workspace
    if cwd:
        workdir = resolve_in_workspace(workspace, cwd)
        if not workdir.is_dir():
            raise WorkspacePathError(f"cwd is not a directory: {cwd}")

    chosen = int(port) if port else free_port()
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("OPENROUTER_") and not key.startswith("TYPESAFE_")
    }
    env["PYTHONUNBUFFERED"] = "1"
    env["TERM"] = "dumb"
    env["PORT"] = str(chosen)
    env["HOST"] = "127.0.0.1"
    blocks = file_limit_blocks(file_limit_mb)
    script = f"ulimit -f {blocks}; {stripped}"

    key = _key(workspace)
    async with _lock:
        if key in _servers:
            await _stop_locked(key)
        proc = await asyncio.create_subprocess_shell(
            script,
            cwd=str(workdir),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        if on_proc is not None:
            on_proc(proc, True)
        managed = ManagedServer(
            proc=proc,
            port=chosen,
            workspace=workspace,
            command=stripped,
            pgid=os.getpgid(proc.pid) if proc.pid else 0,
            on_proc=on_proc,
        )
        managed.drains = [
            asyncio.create_task(_drain(proc.stdout, "stdout", managed, on_output)),
            asyncio.create_task(_drain(proc.stderr, "stderr", managed, on_output)),
        ]
        _servers[key] = managed

    ready = await _wait_ready(managed, READY_TIMEOUT)
    async with _lock:
        current = _servers.get(key)
        if current is not managed:
            return "error: server was replaced before it became ready"
        if ready:
            return (
                f"listening http://127.0.0.1:{chosen}\n"
                f"command: {stripped}\n"
                "HOST=127.0.0.1 and PORT are set. If the framework ignores PORT, "
                "pass this port in the command next time."
            )
        logs = _join_logs(managed).strip() or "(no output)"
        died = managed.proc.returncode is not None
        await _stop_locked(key)
        if died:
            return (
                f"error: server exited before listening on {chosen} "
                f"(exit {managed.proc.returncode})\n--- logs ---\n{logs}"
            )
        return (
            f"error: timed out after {READY_TIMEOUT}s waiting for 127.0.0.1:{chosen}\n"
            f"--- logs ---\n{logs}"
        )


async def stop_server(workspace: Path) -> str:
    async with _lock:
        key = _key(workspace)
        if key not in _servers:
            return "no server running"
        await _stop_locked(key)
        return "stopped"


async def server_logs(workspace: Path) -> str:
    async with _lock:
        managed = _servers.get(_key(workspace))
        if managed is None:
            return "no server running"
        alive = managed.proc.returncode is None
        header = (
            f"http://127.0.0.1:{managed.port} running={alive} "
            f"command={managed.command!r}"
        )
        body = _join_logs(managed).strip() or "(no output yet)"
        return f"{header}\n--- logs ---\n{body}"


async def stop_all() -> None:
    async with _lock:
        for key in list(_servers):
            await _stop_locked(key)


async def _stop_locked(key: str) -> None:
    managed = _servers.pop(key, None)
    if managed is None:
        return
    for task in managed.drains:
        task.cancel()
    if managed.drains:
        await asyncio.gather(*managed.drains, return_exceptions=True)
    await _kill_group(managed.proc)
    if managed.pgid:
        with suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(managed.pgid, signal.SIGKILL)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and _port_open(managed.port):
        await asyncio.sleep(0.05)
    if managed.on_proc is not None:
        managed.on_proc(managed.proc, False)


async def _wait_ready(managed: ManagedServer, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if managed.proc.returncode is not None:
            return False
        if _port_open(managed.port):
            return True
        await asyncio.sleep(0.1)
    return False


async def _drain(
    stream,
    name: str,
    managed: ManagedServer,
    on_output: Callable[[str, str], None] | None,
) -> None:
    if stream is None:
        return
    try:
        while True:
            line = await stream.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace")
            managed.logs.append(text)
            if on_output is not None:
                on_output(name, text)
    except asyncio.CancelledError:
        return
