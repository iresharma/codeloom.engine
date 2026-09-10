from __future__ import annotations

import argparse
import json
import sys
from dataclasses import fields
from pathlib import Path

from protocol.commands import (
    AbortAgent,
    AnswerPrompt,
    COMMANDS,
    RequestOrchContext,
    RequestSnapshot,
    StartSession,
    SubmitUserMessage,
    UndoLastEdit,
)
from protocol.events import (
    AgentFinished,
    AgentStarted,
    AgentStateChanged,
    AgentsUpdated,
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
    OrchContext,
    SessionEnded,
    SessionList,
    SnapshotReady,
    StatsUpdated,
    ToolCallFinished,
    ToolCallStarted,
    UserPromptRequested,
    WarningOccurred,
    WorktreeSettled,
)
from protocol.snapshot import FileTreeNode, GitState
from runtime.tools.git import is_settle_prompt, parse_settle_intent

CLIENT_EXIT = object()
_COMMANDS_BY_NAME = {name.lower(): cls for name, cls in COMMANDS.items()}
_LAST_PROMPT_ID = ""
_LAST_PROMPT_CHOICES: list[str] = []
_STREAM_ID = ""
_NOTES: list[str] = []

_CHAT_EVENTS = (
    ChatHistoryAdded,
    ChatMessageStarted,
    ChatMessageDelta,
    ChatMessageAdded,
    UserPromptRequested,
)
_TOOL_EVENTS = (ToolCallStarted, ToolCallFinished, CommandOutputChunk)
_AGENT_EVENTS = (AgentsUpdated,)


def _note(text: str) -> None:
    _NOTES.append(text)


def drain_notes() -> list[str]:
    notes = _NOTES[:]
    _NOTES.clear()
    return notes


def route_event(event) -> str:
    if isinstance(event, _TOOL_EVENTS):
        return "tools"
    if isinstance(
        event, (ChatMessageStarted, ChatMessageDelta, ChatMessageAdded)
    ) and getattr(event, "agent_id", ""):
        return "agents"
    if isinstance(event, _AGENT_EVENTS):
        return "agents"
    if isinstance(event, OrchContext):
        return "context"
    if isinstance(event, _CHAT_EVENTS):
        return "chat"
    return "protocol"


def format_command(command) -> str:
    data = command.to_json()
    name = data.pop("type", type(command).__name__)
    if not data:
        return name
    parts = []
    for key, value in data.items():
        rendered = repr(value)
        if len(rendered) > 120:
            rendered = rendered[:117] + "..."
        parts.append(f"{key}={rendered}")
    return f"{name} {', '.join(parts)}"


def _help_text() -> str:
    lines = [
        "start [id]          StartSession (workspace filled by this client)",
        "undo                UndoLastEdit (last agent write batch)",
        "abort [id]          AbortAgent (orch reply only; pass an id to cancel one child)",
        "snapshot            RequestSnapshot base state (same as the snapshot button / F5)",
        "context             RequestOrchContext (same as the context button / F6)",
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
            "RequestSnapshot",
            "RequestOrchContext",
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
    lines.append("Live agents: AgentsUpdated / SnapshotReady.agents show count,")
    lines.append("batch, profile, task, status, and current_tool. Child tokens")
    lines.append("stream as ChatMessageDelta with agent_id. abort <id> kills one child.")
    lines.append("When a writer finishes, answer merge / pr / keep / discard")
    lines.append("(or 'please merge it' / 'open a PR'). After keep, tell the orch")
    lines.append("to merge or open a PR — do not spawn another coder.")
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Engine TUI client")
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
        _note(_help_text().rstrip("\n"))
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
    if name in ("snapshot", "snap", "requestsnapshot"):
        return RequestSnapshot(replay=False)
    if name in ("context", "orchcontext", "requestorchcontext"):
        return RequestOrchContext()
    if name in ("abort", "abortagent"):
        return AbortAgent(agent_id=rest or None)
    if name in ("answer", "answerprompt"):
        if not _LAST_PROMPT_ID:
            _note("no prompt is outstanding")
            return None
        if not rest:
            _note("usage: answer <text>  (or type the answer at the answer> prompt)")
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
            _note("usage: SubmitUserMessage <text>")
            return None
        return SubmitUserMessage(text=rest)
    if cls is not None:
        names = [item.name for item in fields(cls)]
        if not names:
            return cls()
        if len(names) == 1:
            if not rest:
                _note(f"usage: {cls.__name__} <{names[0]}>")
                return None
            return cls(**{names[0]: rest})
        _note(f"usage: {cls.__name__}")
        return None
    if line.startswith("/"):
        _note(f"unknown command: {line.split()[0]}  (try help)")
        return None
    if _LAST_PROMPT_ID:
        if is_settle_prompt(_LAST_PROMPT_CHOICES):
            intent = parse_settle_intent(line)
            if intent is None:
                return SubmitUserMessage(text=line)
            return _take_answer(intent)
        return _take_answer(line)
    return SubmitUserMessage(text=line)


def _take_answer(text: str) -> AnswerPrompt:
    global _LAST_PROMPT_ID, _LAST_PROMPT_CHOICES
    prompt_id = _LAST_PROMPT_ID
    _LAST_PROMPT_ID = ""
    _LAST_PROMPT_CHOICES = []
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


def _format_agents(rows) -> str:
    rows = list(rows or [])
    if not rows:
        return "agents: 0 running"
    groups: dict[str, list] = {}
    for row in rows:
        groups.setdefault(row.batch_id or "", []).append(row)
    lines = [f"agents: {len(rows)} running"]
    for batch_id, members in groups.items():
        name = next((row.batch_name for row in members if row.batch_name), "")
        short = batch_id[:8] if batch_id else "—"
        label = f"{name} ({short})" if name else short
        lines.append(f"  batch {label}  ({len(members)})")
        for row in members:
            tool = f" {row.current_tool}" if row.current_tool else ""
            lines.append(
                f"    {row.profile} {row.id[:8]}  {row.status}{tool}"
            )
            task = (row.task or "").replace("\n", " ").strip()
            if task:
                if len(task) > 80:
                    task = task[:77] + "..."
                lines.append(f"      {task}")
            if row.worktree:
                extra = f" branch={row.branch}" if row.branch else ""
                lines.append(f"      worktree={row.worktree}{extra}")
    return "\n".join(lines)


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
        if snap.pending_prompt is not None:
            prompt = snap.pending_prompt
            who = f" agent={prompt.agent_id}" if prompt.agent_id else ""
            lines.append(f"pending_prompt{who}: {prompt.kind} {prompt.question}")
        if snap.ended:
            lines.append("ended: true")
        lines.append(_format_agents(snap.agents))
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
        if event.agent_id:
            return f"agent {event.agent_id[:8]} streaming"
        return ""
    if isinstance(event, ChatMessageDelta):
        global _STREAM_ID
        prefix = "" if not _STREAM_ID or _STREAM_ID == event.id else "\n"
        _STREAM_ID = event.id
        who = f"[{event.agent_id[:8]}] " if event.agent_id and prefix else ""
        return f"{prefix}{who}{event.text}"
    if isinstance(event, ChatMessageAdded):
        if event.id == _STREAM_ID:
            return ""
        who = f" [{event.agent_id[:8]}]" if event.agent_id else ""
        return f"{event.role}{who}: {event.text}"
    if isinstance(event, ToolCallStarted):
        who = f" [{event.agent_id}]" if event.agent_id else ""
        return f"tool {event.name} started{who}"
    if isinstance(event, ToolCallFinished):
        flag = "ok" if event.ok else "error"
        who = f" [{event.agent_id}]" if event.agent_id else ""
        return f"tool {event.name} {flag} ({event.duration_ms}ms){who}\n  {event.preview}"
    if isinstance(event, CommandOutputChunk):
        return f"  [{event.stream}] {event.text.rstrip()}"
    if isinstance(event, AgentStateChanged):
        who = event.agent_id or "orch"
        return f"agent {who} {event.state}  turn={event.turn}/{event.max_turns}"
    if isinstance(event, AgentStarted):
        extra = ""
        if event.worktree:
            extra = f" worktree={event.worktree} branch={event.branch}"
        return (
            f"agent started {event.profile} {event.agent_id} "
            f"batch={event.batch_name or event.batch_id[:8] or '-'} "
            f"task={event.task[:80]}{extra}"
        )
    if isinstance(event, AgentFinished):
        extra = ""
        if event.cost or event.total_tokens:
            extra = (
                f" ${event.cost:.3f} {event.total_tokens} tok "
                f"{event.cached_tokens} cached"
            )
        return (
            f"agent finished {event.profile} {event.agent_id} "
            f"{event.status}:{extra} {event.summary[:120]}"
        )
    if isinstance(event, AgentsUpdated):
        return _format_agents(event.agents)
    if isinstance(event, WorktreeSettled):
        flag = "ok" if event.ok else "error"
        extra = f" {event.pr_url}" if event.pr_url else ""
        return (
            f"worktree {event.action} {flag} {event.profile} {event.agent_id[:8]} "
            f"{event.branch}{extra}\n  {event.detail}"
        )
    if isinstance(event, OrchContext):
        preview = event.text if len(event.text) <= 400 else event.text[:400] + "…"
        return f"orch context ({len(event.text)} chars)\n{preview}"
    if isinstance(event, StatsUpdated):
        s = event.stats
        return (
            f"tokens {s.prompt_tokens / 1000:.1f}k in / {s.completion_tokens / 1000:.1f}k out "
            f"· {s.cached_tokens / 1000:.1f}k cached · ${s.cost:.3f} · {s.elapsed_s:.1f}s · turn {s.turns}"
        )
    if isinstance(event, UserPromptRequested):
        global _LAST_PROMPT_ID, _LAST_PROMPT_CHOICES
        _LAST_PROMPT_ID = event.prompt_id
        _LAST_PROMPT_CHOICES = list(event.choices or [])
        extra = f"  choices={event.choices}" if event.choices else ""
        who = f" agent={event.agent_id}" if event.agent_id else ""
        return (
            f"PROMPT {event.kind}{who}: {event.question}{extra}\n"
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


def main() -> None:
    args = parse_args()
    workspace = Path(args.workspace).expanduser().resolve()
    socket_path = workspace / ".engine" / "engine.sock"
    if not socket_path.exists():
        print(f"no server socket at {socket_path}", file=sys.stderr)
        print("start the engine first: python app.py", file=sys.stderr)
        sys.exit(1)

    from client_tui import DummyClientApp

    DummyClientApp(workspace=workspace, socket_path=socket_path).run()


if __name__ == "__main__":
    main()
