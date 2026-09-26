from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from runtime.tools.fs import WorkspacePathError, resolve_in_workspace
from runtime.tools.runners import RunnerResult, parse_runner_result
from tools.base import ELIDED_MARKER, elide_middle

DEFAULT_TIMEOUT = 120
HARD_MAX_TIMEOUT = 600
STREAM_CAP = 30_000
TOTAL_CAP = 60_000
COALESCE_MS = 100
COALESCE_BYTES = 4096
# What the *model* sees per stream. A test runner prints its verdict last
# ("92 passed, 2 failed in 4.2s"), so a head-only cut removes the one line
# the agent actually needs and invites it to invent the counts. Keep both
# ends: enough head to see which target ran, a bigger tail to hold the
# summary and the last failure.
MODEL_HEAD_CHARS = 1500
MODEL_TAIL_CHARS = 2500
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
    # Structured runner verdict (runtime/tools/runners.py), or None when
    # nothing recognized the command. Callers that need counts read this
    # instead of parsing prose.
    parsed: RunnerResult | None = None


def file_limit_blocks(mb: int) -> int:
    """POSIX ulimit -f units: 512-byte blocks. 1 MiB = 2048 blocks."""
    return max(1, mb) * 2048


def _for_model(text: str) -> str:
    return elide_middle(text, MODEL_HEAD_CHARS, MODEL_TAIL_CHARS)


def format_command_result(result: CommandResult) -> str:
    lines = [
        f"$ {result.command}",
        f"exit code: {result.exit_code}  ({result.duration_s:.1f}s)",
    ]
    if result.timed_out:
        lines.append(f"(timed out after {result.duration_s:.0f}s; process group killed)")
    parsed = result.parsed
    if parsed is not None and parsed.runner is not None:
        lines.append(f"--- result ---\n{json.dumps(parsed.as_dict(), sort_keys=True)}")
    lines.append("--- stdout ---")
    lines.append(_for_model(result.stdout) or "(empty)")
    lines.append("--- stderr ---")
    lines.append(_for_model(result.stderr) or "(empty)")
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
    drop_env_prefixes: tuple[str, ...] = (),
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

    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("OPENROUTER_", "TYPESAFE_", *drop_env_prefixes))
    }
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
    head_budget = STREAM_CAP // 2
    tail_budget = STREAM_CAP - head_budget
    stdout_buf = _StreamBuffer(head_budget, tail_budget)
    stderr_buf = _StreamBuffer(head_budget, tail_budget)
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
    out = stdout_buf.text()
    err = stderr_buf.text()
    return CommandResult(
        command=stripped,
        exit_code=code,
        stdout=out,
        stderr=err,
        duration_s=duration,
        timed_out=timed_out,
        parsed=parse_runner_result(stripped, code, out, err),
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


class _StreamBuffer:
    """Bounded capture that keeps both ends of a stream.

    The previous sink appended only while the running total stayed under
    `STREAM_CAP * 4` and then stopped, so on a long run the recorded
    "tail" was really the tail of the first 120k characters -- the runner's
    summary line, printed last, had already been dropped before any
    formatter could choose to keep it. This keeps a head window and a
    rolling tail window instead, so the end of the stream always survives.
    """

    def __init__(self, head_budget: int, tail_budget: int):
        self._head_budget = max(0, head_budget)
        self._tail_budget = max(0, tail_budget)
        self._head: list[str] = []
        self._head_len = 0
        self._tail: deque[str] = deque()
        self._tail_len = 0
        self._elided = 0

    def append(self, text: str) -> None:
        if not text:
            return
        if self._head_len < self._head_budget:
            room = self._head_budget - self._head_len
            if len(text) <= room:
                self._head.append(text)
                self._head_len += len(text)
                return
            self._head.append(text[:room])
            self._head_len += room
            text = text[room:]
        self._tail.append(text)
        self._tail_len += len(text)
        while self._tail_len - len(self._tail[0]) >= self._tail_budget:
            dropped = self._tail.popleft()
            self._tail_len -= len(dropped)
            self._elided += len(dropped)

    def text(self) -> str:
        head = "".join(self._head)
        tail = "".join(self._tail)
        if self._tail_len > self._tail_budget:
            overflow = self._tail_len - self._tail_budget
            tail = tail[overflow:]
            self._elided += overflow
            self._tail.clear()
            self._tail.append(tail)
            self._tail_len = len(tail)
        if not self._elided:
            return head + tail
        return head + ELIDED_MARKER.format(n=self._elided) + tail


async def _drain(stream, name: str, sink: _StreamBuffer, on_output) -> None:
    if stream is None:
        return
    last = 0.0
    pending = ""
    while True:
        line = await stream.readline()
        if not line:
            break
        text = line.decode("utf-8", errors="replace")
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
