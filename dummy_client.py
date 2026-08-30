from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from protocol.codec import ProtocolError, decode_event, encode
from protocol.commands import (
    ListSessions,
    RequestSnapshot,
    Shutdown,
    StartSession,
    SubmitUserMessage,
)

HELP = """\
/start              start a new session
/start <id>         resume a stored session
/sessions           list stored sessions
/snapshot           request the current snapshot
/shutdown           persist and close the current session (server stays up)
/help               this text
<text>              send as a user message
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Engine REPL client")
    parser.add_argument(
        "workspace",
        nargs="?",
        default=".",
        help="project root (default: current directory)",
    )
    return parser.parse_args()


def command_from_line(line: str, workspace: Path):
    if line == "/start":
        return StartSession(workspace=str(workspace))
    if line.startswith("/start "):
        session_id = line.split(maxsplit=1)[1].strip()
        if not session_id:
            print("usage: /start [session_id]", flush=True)
            return None
        return StartSession(workspace=str(workspace), session_id=session_id)
    if line == "/sessions":
        return ListSessions()
    if line == "/snapshot":
        return RequestSnapshot()
    if line == "/shutdown":
        return Shutdown()
    if line.startswith("/"):
        print(f"unknown command: {line.split()[0]}  (try /help)", flush=True)
        return None
    return SubmitUserMessage(text=line)


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
        print(f"\n{json.dumps(event.to_json(), indent=2)}", flush=True)


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
            print(flush=True)
            break
        if done.is_set():
            break
        text = line.strip()
        if not text:
            continue
        if text == "/help":
            print(HELP, end="", flush=True)
            continue
        command = command_from_line(text, workspace)
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
