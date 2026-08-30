from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from protocol.codec import ProtocolError, decode_event, encode
from protocol.commands import (
    CloseFile,
    ListSessions,
    OpenFile,
    RequestSnapshot,
    Shutdown,
    StartSession,
    SubmitUserMessage,
)
from protocol.events import FileContent, SessionList, SnapshotReady
from protocol.snapshot import FileTreeNode

HELP = """\
start [id]          start a new session, or resume id
sessions            list stored sessions
open <path>         open a file (path is stored as UI state)
close <path>        close a file
snapshot            request the current snapshot (tree rebuilt from disk)
shutdown            persist and close the current session (server stays up)
exit                disconnect this client (server stays up)
help                this text
<text>              send as a user message
"""

CLIENT_EXIT = object()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Engine REPL client")
    parser.add_argument(
        "workspace",
        nargs="?",
        default=".",
        help="project root (default: current directory)",
    )
    return parser.parse_args()


def _split(line: str) -> tuple[str, str]:
    text = line[1:] if line.startswith("/") else line
    parts = text.split(maxsplit=1)
    name = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""
    if name == "startsession":
        name = "start"
    return name, rest


def command_from_line(line: str, workspace: Path):
    name, rest = _split(line)
    if name == "help":
        print(HELP, end="", flush=True)
        return None
    if name == "start":
        if not rest:
            return StartSession(workspace=str(workspace))
        return StartSession(workspace=str(workspace), session_id=rest)
    if name == "sessions":
        return ListSessions()
    if name == "snapshot":
        return RequestSnapshot()
    if name == "open":
        if not rest:
            print("usage: open <path>", flush=True)
            return None
        return OpenFile(path=rest)
    if name == "close":
        if not rest:
            print("usage: close <path>", flush=True)
            return None
        return CloseFile(path=rest)
    if name == "shutdown":
        return Shutdown()
    if name in ("exit", "quit"):
        return CLIENT_EXIT
    if line.startswith("/"):
        print(f"unknown command: {line.split()[0]}  (try help)", flush=True)
        return None
    return SubmitUserMessage(text=line)


def _tree_size(nodes: list[FileTreeNode]) -> int:
    total = 0
    for node in nodes:
        total += 1
        if node.children:
            total += _tree_size(node.children)
    return total


def format_event(event) -> str:
    if isinstance(event, SnapshotReady):
        snap = event.snapshot
        lines = [
            f"session {snap.session_id}",
            f"open_files: {snap.open_files or []}",
            f"file_tree: {_tree_size(snap.file_tree)} entries (rebuilt, not stored)",
            "messages:",
        ]
        if not snap.messages:
            lines.append("  (none)")
        else:
            for message in snap.messages:
                lines.append(f"  {message.role}: {message.text}")
        return "\n".join(lines)
    if isinstance(event, SessionList):
        if not event.sessions:
            return "sessions: (none)"
        lines = ["sessions:"]
        for item in event.sessions:
            lines.append(
                f"  {item.id}  messages={item.message_count}  "
                f"open={item.open_files or []}"
            )
        return "\n".join(lines)
    if isinstance(event, FileContent):
        nlines = event.content.count("\n") + (0 if event.content.endswith("\n") else 1)
        return f"opened {event.path} ({nlines} lines)"
    return json.dumps(event.to_json(), indent=2)


async def print_events(reader: asyncio.StreamReader, done: asyncio.Event) -> None:
    while True:
        line = await reader.readline()
        if not line:
            print("\ndisconnected from server; press enter to exit", flush=True)
            done.set()
            break
        try:
            event = decode_event(line)
        except ProtocolError as exc:
            print(f"\nbad event: {exc}", flush=True)
            continue
        print(f"\n{format_event(event)}", flush=True)


async def repl(
    writer: asyncio.StreamWriter,
    workspace: Path,
    done: asyncio.Event,
) -> None:
    loop = asyncio.get_running_loop()
    while not done.is_set():
        try:
            line = await loop.run_in_executor(None, lambda: input("engine> "))
        except EOFError:
            print("bye", flush=True)
            done.set()
            break
        if done.is_set():
            break
        text = line.strip()
        if not text:
            continue
        command = command_from_line(text, workspace)
        if command is CLIENT_EXIT:
            print("bye", flush=True)
            done.set()
            break
        if command is None:
            continue
        writer.write(encode(command))
        await writer.drain()


async def main() -> None:
    args = parse_args()
    workspace = Path(args.workspace).expanduser().resolve()
    socket_path = workspace / ".engine" / "engine.sock"
    if not socket_path.exists():
        print(f"no server socket at {socket_path}", file=sys.stderr)
        print("start the engine first: python app.py", file=sys.stderr)
        sys.exit(1)

    try:
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
    except (ConnectionRefusedError, FileNotFoundError) as exc:
        print(f"could not connect to {socket_path}: {exc}", file=sys.stderr)
        sys.exit(1)

    done = asyncio.Event()
    events_task = asyncio.create_task(print_events(reader, done))
    repl_task = asyncio.create_task(repl(writer, workspace, done))
    try:
        await asyncio.wait(
            {events_task, repl_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        done.set()
        for task in (events_task, repl_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(events_task, repl_task, return_exceptions=True)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
