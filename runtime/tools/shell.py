from __future__ import annotations

import asyncio
import os
import signal
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from runtime.tools.fs import WorkspacePathError, resolve_in_workspace

DEFAULT_TIMEOUT = 120
HARD_MAX_TIMEOUT = 600
STREAM_CAP = 30_000
TOTAL_CAP = 60_000
COALESCE_MS = 100
COALESCE_BYTES = 4096
READ_ONLY_PREFIXES = (
    "git status",
    "git diff",
    "git log",
    "git show",
    "ls",
    "cat",
    "pwd",
    "which",
    "python -V",
    "python --version",
    "pytest",
    "npm test",
    "go test",
    "go build",
    "cargo test",
    "make test",
    "ruff",
    "mypy",
    "tsc --noEmit",
)
SHELL_METACHAR = set(";|&>`")
DENY_TOKENS = ("sudo", ".engine")


@dataclass
class CommandResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False


def file_limit_blocks(mb: int) -> int:
    """POSIX ulimit -f units: 512-byte blocks. 1 MiB = 2048 blocks."""
    return max(1, mb) * 2048


def format_command_result(result: CommandResult) -> str:
    lines = [
        f"$ {result.command}",
        f"exit code: {result.exit_code}  ({result.duration_s:.1f}s)",
    ]
    if result.timed_out:
        lines.append(f"(timed out after {result.duration_s:.0f}s; process group killed)")
    lines.append("--- stdout ---")
    lines.append(result.stdout or "(empty)")
    lines.append("--- stderr ---")
    lines.append(result.stderr or "(empty)")
    return "\n".join(lines)


async def run_command(
    workspace: Path,
    command: str,
    cwd: str = "",
    timeout: int = DEFAULT_TIMEOUT,
    *,
    on_output: Callable[[str, str], None] | None = None,
    approve: Callable[[str, str], Awaitable[bool]] | None = None,
    approval: str = "auto",
    file_limit_mb: int = 2048,
    on_proc: Callable[[object, bool], None] | None = None,
) -> CommandResult:
    if not command or not command.strip():
        raise ValueError("command is required")
    stripped = command.strip()
    _hard_deny(stripped)
    timeout = min(max(1, timeout), HARD_MAX_TIMEOUT)
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

    env = {key: value for key, value in os.environ.items() if not key.startswith("OPENROUTER_")}
    env["PYTHONUNBUFFERED"] = "1"
    env["CI"] = "1"
    env["TERM"] = "dumb"
    blocks = file_limit_blocks(file_limit_mb)
    script = f"ulimit -f {blocks}; {stripped}"
    started = time.monotonic()
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
    stdout_buf: list[str] = []
    stderr_buf: list[str] = []
    timed_out = False
    try:
        await asyncio.wait_for(
            asyncio.gather(
                _drain(proc.stdout, "stdout", stdout_buf, on_output),
                _drain(proc.stderr, "stderr", stderr_buf, on_output),
            ),
            timeout=timeout,
        )
        await proc.wait()
    except asyncio.TimeoutError:
        timed_out = True
        await _kill_group(proc)
    except asyncio.CancelledError:
        await _kill_group(proc)
        raise
    finally:
        if on_proc is not None:
            on_proc(proc, False)
    duration = time.monotonic() - started
    code = proc.returncode if proc.returncode is not None else -1
    return CommandResult(
        command=stripped,
        exit_code=code,
        stdout=_join_capped(stdout_buf, STREAM_CAP),
        stderr=_join_capped(stderr_buf, STREAM_CAP),
        duration_s=duration,
        timed_out=timed_out,
    )


def _hard_deny(command: str) -> None:
    lowered = command.lower()
    if "sudo" in lowered.split() or lowered.startswith("sudo"):
        raise RuntimeError("sudo is not allowed")
    if ".engine" in command:
        raise RuntimeError("commands may not touch .engine")


def _auto_allowed(command: str, approval: str) -> bool:
    if approval == "never":
        return True
    if approval == "always":
        return False
    if any(ch in command for ch in SHELL_METACHAR):
        return False
    return any(command == prefix or command.startswith(prefix + " ") for prefix in READ_ONLY_PREFIXES)


async def _drain(stream, name: str, sink: list[str], on_output) -> None:
    if stream is None:
        return
    last = 0.0
    pending = ""
    total = 0
    while True:
        line = await stream.readline()
        if not line:
            break
        text = line.decode("utf-8", errors="replace")
        total += len(text)
        if total <= STREAM_CAP * 4:
            sink.append(text)
        if on_output is None:
            continue
        pending += text
        now = time.monotonic()
        if len(pending) >= COALESCE_BYTES or (now - last) * 1000 >= COALESCE_MS:
            on_output(name, pending)
            pending = ""
            last = now
    if on_output is not None and pending:
        on_output(name, pending)


def _join_capped(parts: list[str], cap: int) -> str:
    text = "".join(parts)
    if len(text) <= cap:
        return text
    omitted = len(text) - cap
    head = text[: cap // 2]
    tail = text[-(cap // 2) :]
    return f"{head}\n... ({omitted} chars elided) ...\n{tail}"


async def _kill_group(proc) -> None:
    pid = getattr(proc, "pid", None)
    if pid is None:
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            return
    try:
        await asyncio.wait_for(proc.wait(), timeout=3)
        return
    except (asyncio.TimeoutError, ProcessLookupError):
        pass
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            return
    with_suppress = True
    if with_suppress:
        try:
            await proc.wait()
        except ProcessLookupError:
            return
