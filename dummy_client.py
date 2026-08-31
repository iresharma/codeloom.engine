from __future__ import annotations

import argparse
import asyncio
import json
import sys
from contextlib import suppress
from dataclasses import fields
from pathlib import Path

from protocol.codec import STREAM_LIMIT, decode_event, encode
from protocol.commands import (
    AbortAgent,
    AnswerPrompt,
    COMMANDS,
    StartSession,
    SubmitUserMessage,
    UndoLastEdit,
)
from protocol.events import (
    AgentStateChanged,
    ChatHistoryAdded,
    ChatHistoryComplete,
    ChatMessageAdded,
    ChatMessageDelta,
    ChatMessageStarted,
    CommandOutputChunk,
    ContextCompacted,
    ErrorOccurred,
    FileClosed,
    FileContent,
    FileEdited,
    FileTreeUpdated,
    GitStateUpdated,
    SessionEnded,
    SessionList,
    SnapshotReady,
    StatsUpdated,
    ToolCallFinished,
    ToolCallStarted,
    UserPromptRequested,
    WarningOccurred,
)
from protocol.snapshot import FileTreeNode, GitState

CLIENT_EXIT = object()
_COMMANDS_BY_NAME = {name.lower(): cls for name, cls in COMMANDS.items()}
_LAST_PROMPT_ID = ""
_STREAM_ID = ""


def _help_text() -> str:
    lines = [
        "start [id]          StartSession (workspace filled by this client)",
        "undo                UndoLastEdit (last agent write batch)",
        "abort               AbortAgent (cancel the in-flight turn)",
        "answer <text>       AnswerPrompt (or just type the answer while a prompt is up)",
        "exit                disconnect this client (server stays up)",
        "help                this text",
    ]
    for name, cls in COMMANDS.items():
        if name in (
            "StartSession",
            "SubmitUserMessage",
            "UndoLastEdit",
            "AbortAgent",
            "AnswerPrompt",
        ):
            continue
        names = [item.name for item in fields(cls)]
        if names:
            args = " ".join(f"<{item}>" for item in names)
            lines.append(f"{name} {args}")
        else:
            lines.append(name)
    lines.append("<text>              SubmitUserMessage (agent may call write tools)")
    lines.append("")
    lines.append("Write events: FileEdited prints the applied diff; tool chat lines")
    lines.append("are a 400-char preview. Open a file first to also see FileContent")
    lines.append("refresh after each edit. undo restores the last journal batch.")
    return "\n".join(lines) + "\n"


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
    text = line.removeprefix("/")
    parts = text.split(maxsplit=1)
    name = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""
    return name, rest


def command_from_line(line: str, workspace: Path):
    name, rest = _split(line)
    if name == "help":
        print(_help_text(), end="", flush=True)
        return None
    if name in ("exit", "quit"):
        return CLIENT_EXIT
    if name in ("start", "startsession"):
        return StartSession(
            workspace=str(workspace),
            session_id=rest or None,
        )
    if name in ("undo", "undolastedit"):
        return UndoLastEdit()
    if name in ("abort", "abortagent"):
        return AbortAgent()
    if name in ("answer", "answerprompt"):
        if not _LAST_PROMPT_ID:
            print("no prompt is outstanding", flush=True)
            return None
        if not rest:
            print("usage: answer <text>  (or type the answer at the answer> prompt)", flush=True)
            return None
        return _take_answer(rest)
    cls = _COMMANDS_BY_NAME.get(name)
    if cls is StartSession:
        return StartSession(
            workspace=str(workspace),
            session_id=rest or None,
        )
    if cls is SubmitUserMessage:
        if not rest:
            print("usage: SubmitUserMessage <text>", flush=True)
            return None
        return SubmitUserMessage(text=rest)
    if cls is not None:
        names = [item.name for item in fields(cls)]
        if not names:
            return cls()
        if len(names) == 1:
            if not rest:
                print(f"usage: {cls.__name__} <{names[0]}>", flush=True)
                return None
            return cls(**{names[0]: rest})
        print(f"usage: {cls.__name__}", flush=True)
        return None
    if line.startswith("/"):
        print(f"unknown command: {line.split()[0]}  (try help)", flush=True)
        return None
    if _LAST_PROMPT_ID:
        return _take_answer(line)
    return SubmitUserMessage(text=line)


def _take_answer(text: str) -> AnswerPrompt:
    global _LAST_PROMPT_ID
    prompt_id = _LAST_PROMPT_ID
    _LAST_PROMPT_ID = ""
    return AnswerPrompt(prompt_id=prompt_id, text=text)


def _tree_size(nodes: list[FileTreeNode]) -> int:
    total = 0
    for node in nodes:
        total += 1
        if node.children:
            total += _tree_size(node.children)
    return total


def _format_git(git: GitState) -> list[str]:
    if git.branch is None:
        return ["git: (not a repository)"]
    lines = [
        f"git: {git.branch}  dirty={git.dirty}",
        f"  staged: {git.staged or []}",
        f"  unstaged: {git.unstaged or []}",
        f"  untracked: {git.untracked or []}",
    ]
    if git.staged_diff:
        lines.append("  staged_diff: (present)")
    if git.unstaged_diff:
        lines.append("  unstaged_diff: (present)")
    return lines


def format_event(event) -> str:
    if isinstance(event, SnapshotReady):
        snap = event.snapshot
        lines = [
            f"session {snap.session_id}",
            (
                f"language: {snap.language or 'unknown'}  "
                f"tree-sitter/LSP: {'yes' if snap.language_supported else 'no'}"
            ),
            f"open_files: {snap.open_files or []}",
            f"file_tree: {_tree_size(snap.file_tree)} entries (rebuilt, not stored)",
        ]
        lines.extend(_format_git(snap.git))
        lines.append(
            f"chat history: {snap.message_count} messages (streamed next, not packed here)"
        )
        lines.append(
            f"file_tree_count: {snap.file_tree_count} (tree streamed as FileTreeUpdated)"
        )
        if snap.stats is not None:
            lines.append(
                f"stats: {snap.stats.total_tokens} tokens · ${snap.stats.cost:.3f}"
            )
        return "\n".join(lines)
    if isinstance(event, GitStateUpdated):
        return "\n".join(_format_git(event.git))
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
        return _format_file_content(event)
    if isinstance(event, FileEdited):
        return _format_file_edited(event)
    if isinstance(event, FileClosed):
        return f"closed {event.path}"
    if isinstance(event, ChatHistoryAdded):
        return f"[history {event.index + 1}/{event.total}] {event.role}: {event.text}"
    if isinstance(event, ChatHistoryComplete):
        return f"chat history complete ({event.count})"
    if isinstance(event, ChatMessageStarted):
        return ""
    if isinstance(event, ChatMessageDelta):
        global _STREAM_ID
        prefix = "" if _STREAM_ID == event.id else "\n"
        _STREAM_ID = event.id
        print(f"{prefix}{event.text}", end="", flush=True)
        return ""
    if isinstance(event, ChatMessageAdded):
        if event.id == _STREAM_ID:
            return ""
        return f"{event.role}: {event.text}"
    if isinstance(event, ToolCallStarted):
        return f"tool {event.name} started"
    if isinstance(event, ToolCallFinished):
        flag = "ok" if event.ok else "error"
        return f"tool {event.name} {flag} ({event.duration_ms}ms)\n  {event.preview}"
    if isinstance(event, CommandOutputChunk):
        return f"  [{event.stream}] {event.text.rstrip()}"
    if isinstance(event, AgentStateChanged):
        return f"agent {event.state}  turn={event.turn}/{event.max_turns}"
    if isinstance(event, StatsUpdated):
        s = event.stats
        return (
            f"tokens {s.prompt_tokens / 1000:.1f}k in / {s.completion_tokens / 1000:.1f}k out "
            f"· ${s.cost:.3f} · {s.elapsed_s:.1f}s · turn {s.turns}"
        )
    if isinstance(event, UserPromptRequested):
        global _LAST_PROMPT_ID
        _LAST_PROMPT_ID = event.prompt_id
        extra = f"  choices={event.choices}" if event.choices else ""
        return (
            f"PROMPT {event.kind}: {event.question}{extra}\n"
            f"  type the answer (yes/no) or: answer <text>"
        )
    if isinstance(event, ContextCompacted):
        return (
            f"compacted {event.strategy}: {event.messages_before}->{event.messages_after} "
            f"saved {event.chars_saved} chars"
        )
    if isinstance(event, FileTreeUpdated):
        return f"file_tree updated ({_tree_size(event.file_tree)} entries)"
    if isinstance(event, ErrorOccurred):
        return f"error: {event.message}"
    if isinstance(event, WarningOccurred):
        return f"warning: {event.message}"
    if isinstance(event, SessionEnded):
        return f"session ended ({event.reason})"
    return json.dumps(event.to_json(), indent=2)


_FILE_PREVIEW = 24
_DIFF_PREVIEW = 80


def _line_count(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def _preview_block(text: str, limit: int) -> str:
    lines = text.splitlines()
    if len(lines) <= limit:
        return text if text.endswith("\n") or not text else text + "\n"
    head = "\n".join(lines[:limit])
    extra = len(lines) - limit
    return f"{head}\n... ({extra} more lines)\n"


def _format_file_content(event: FileContent) -> str:
    nlines = _line_count(event.content)
    header = f"file {event.path} ({nlines} lines)"
    if not event.content:
        return f"{header}\n  (empty)"
    body = _preview_block(event.content, _FILE_PREVIEW)
    return header + "\n" + "".join(f"  {line}" for line in body.splitlines(keepends=True))


def _format_file_edited(event: FileEdited) -> str:
    ident = event.edit_id or "-"
    header = f"edited {event.path}  tool={event.tool}  id={ident}"
    if not event.diff.strip():
        return f"{header}\n  (no textual diff)"
    body = _preview_block(event.diff, _DIFF_PREVIEW)
    return header + "\n" + body


async def print_events(reader: asyncio.StreamReader, done: asyncio.Event) -> None:
    while True:
        try:
            line = await reader.readline()
        except (asyncio.LimitOverrunError, ValueError) as exc:
            print(f"\nbad event stream: {exc}", flush=True)
            done.set()
            break
        if not line:
            print("\ndisconnected from server; press enter to exit", flush=True)
            done.set()
            break
        try:
            event = decode_event(line)
            text = format_event(event)
        except Exception as exc:  # noqa: BLE001
            print(f"\nbad event: {exc}", flush=True)
            continue
        if text == "":
            continue
        print(f"\n{text}", flush=True)


async def repl(
    writer: asyncio.StreamWriter,
    workspace: Path,
    done: asyncio.Event,
) -> None:
    loop = asyncio.get_running_loop()
    while not done.is_set():
        try:
            hint = "answer> " if _LAST_PROMPT_ID else "engine> "
            line = await loop.run_in_executor(None, lambda h=hint: input(h))
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
        reader, writer = await asyncio.open_unix_connection(
            str(socket_path), limit=STREAM_LIMIT
        )
    except (ConnectionRefusedError, FileNotFoundError) as exc:
        print(f"could not connect to {socket_path}: {exc}", file=sys.stderr)
        sys.exit(1)

    done = asyncio.Event()
    events_task = asyncio.create_task(print_events(reader, done))
    repl_task = asyncio.create_task(repl(writer, workspace, done))
    try:
        done_tasks, pending = await asyncio.wait(
            {events_task, repl_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done_tasks:
            exc = task.exception() if not task.cancelled() else None
            if exc is not None:
                print(f"client error: {exc}", file=sys.stderr, flush=True)
        done.set()
        for task in pending:
            task.cancel()
        await asyncio.gather(events_task, repl_task, return_exceptions=True)
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
