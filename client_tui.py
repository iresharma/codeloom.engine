from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from pathlib import Path

from rich.markup import escape
from rich.rule import Rule as RichRule
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Input, RichLog, Rule, Static

import dummy_client
from protocol.codec import STREAM_LIMIT, decode_event, encode
from protocol.commands import RequestOrchContext, RequestSnapshot, StartSession
from protocol.events import (
    AgentStateChanged,
    AgentsUpdated,
    OrchContext,
    ChatHistoryAdded,
    ChatMessageAdded,
    ChatMessageDelta,
    ChatMessageStarted,
    CommandOutputChunk,
    SnapshotReady,
    StatsUpdated,
    ToolCallFinished,
    ToolCallStarted,
    UserPromptRequested,
)


class EventReceived(Message):
    def __init__(self, event) -> None:
        super().__init__()
        self.event = event


class StreamNotice(Message):
    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class ChatLine(Static):
    def __init__(self, role: str, text: str, message_id: str = "", history: bool = False) -> None:
        self.role = role
        self.message_id = message_id
        self._body = text
        classes = "chat-history" if history else f"chat-{_role_class(role)}"
        super().__init__(self._markup(), classes=classes, markup=True)

    def append(self, chunk: str) -> None:
        self._body += chunk
        self.update(self._markup())

    def set_body(self, text: str) -> None:
        self._body = text
        self.update(self._markup())

    def _markup(self) -> str:
        label = escape(self.role)
        body = escape(self._body) if self._body else "[dim]…[/dim]"
        return f"[b]{label}[/b]\n{body}"


class PromptLine(Static):
    def __init__(self, event: UserPromptRequested) -> None:
        extra = f"\nchoices: {', '.join(event.choices)}" if event.choices else ""
        default = f"\ndefault: {event.default}" if event.default else ""
        text = (
            f"[b]prompt · {escape(event.kind)}[/b]\n"
            f"{escape(event.question)}{escape(extra)}{escape(default)}\n"
            "[dim]type the answer, or: answer <text>[/dim]"
        )
        super().__init__(text, classes="chat-prompt", markup=True)


class ChatPanel(VerticalScroll):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._lines: dict[str, ChatLine] = {}

    def add_history(self, event: ChatHistoryAdded) -> None:
        label = f"{event.role}  history {event.index + 1}/{event.total}"
        line = ChatLine(label, event.text, message_id=event.id, history=True)
        self._mount_line(line)

    def start_message(self, message_id: str, role: str) -> ChatLine:
        existing = self._lines.get(message_id)
        if existing is not None:
            return existing
        line = ChatLine(role, "", message_id=message_id)
        self._lines[message_id] = line
        return line

    def append_delta(self, message_id: str, text: str) -> None:
        if not text:
            return
        line = self._lines.get(message_id)
        if line is None:
            line = self.start_message(message_id, "assistant")
        self._ensure_visible(line)
        line.append(text)
        self.scroll_end(animate=False)

    def add_message(self, event: ChatMessageAdded) -> None:
        if not (event.text or "").strip() and event.role == "assistant":
            return
        existing = self._lines.get(event.id)
        if existing is not None:
            if event.text and event.text != existing._body:
                existing.set_body(event.text)
            self._ensure_visible(existing)
            self.scroll_end(animate=False)
            return
        line = ChatLine(event.role, event.text, message_id=event.id)
        self._lines[event.id] = line
        self._mount_line(line)

    def _ensure_visible(self, line: ChatLine) -> None:
        if line.parent is None:
            self._mount_line(line)

    def add_prompt(self, event: UserPromptRequested) -> None:
        self._mount_line(PromptLine(event))

    def _mount_line(self, widget: Static) -> None:
        if self.children:
            self.mount(Rule(classes="item-sep"))
        self.mount(widget)
        self.scroll_end(animate=False)


class ProtocolLog(RichLog):
    def __init__(self, **kwargs) -> None:
        super().__init__(highlight=False, markup=True, max_lines=2000, **kwargs)
        self._has_entry = False

    def log_outbound(self, command) -> None:
        self._separate()
        self.write(f"[bold #d4b44a]→[/] {escape(dummy_client.format_command(command))}")

    def log_inbound(self, text: str) -> None:
        if not text:
            return
        self._separate()
        self.write(escape(text))

    def log_note(self, text: str) -> None:
        if not text:
            return
        self._separate()
        self.write(f"[dim]{escape(text)}[/dim]")

    def _separate(self) -> None:
        if self._has_entry:
            self.write(RichRule(style="#5a4e28"))
        self._has_entry = True


class ToolCard(Static):
    def __init__(
        self,
        call_id: str,
        name: str,
        arguments_json: str,
        agent_id: str = "",
    ) -> None:
        self.call_id = call_id
        self.tool_name = name
        self.arguments_json = arguments_json
        self.agent_id = agent_id
        self.chunks: list[str] = []
        self.ok: bool | None = None
        self.duration_ms: int | None = None
        self.preview = ""
        super().__init__(self._markup(), classes="tool-card tool-running", markup=True)

    def add_chunk(self, stream: str, text: str) -> None:
        cleaned = text.rstrip("\n")
        if cleaned:
            self.chunks.append(f"[{stream}] {cleaned}")
            if len(self.chunks) > 80:
                self.chunks = self.chunks[-80:]
        self.update(self._markup())

    def finish(self, ok: bool, duration_ms: int, preview: str) -> None:
        self.ok = ok
        self.duration_ms = duration_ms
        self.preview = preview
        self.remove_class("tool-running")
        self.add_class("tool-ok" if ok else "tool-error")
        self.update(self._markup())

    def _markup(self) -> str:
        if self.ok is None:
            status = "[#d4b44a]running[/]"
        elif self.ok:
            status = f"[#7dcea0]ok[/]  {self.duration_ms}ms"
        else:
            status = f"[#e07a7a]error[/]  {self.duration_ms}ms"
        lines = [f"[b]{escape(self.tool_name)}[/b]  {status}"]
        if self.agent_id:
            lines[0] += f"  [dim]{escape(self.agent_id[:8])}[/dim]"
        args = _pretty_args(self.arguments_json)
        if args:
            lines.append(escape(args))
        for chunk in self.chunks:
            lines.append(f"[dim]{escape(chunk)}[/dim]")
        if self.preview:
            lines.append(escape(self.preview))
        return "\n".join(lines)


class ToolsPanel(VerticalScroll):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._cards: dict[str, ToolCard] = {}
        self._last_shell: ToolCard | None = None

    def start_call(self, event: ToolCallStarted) -> None:
        card = ToolCard(
            event.call_id, event.name, event.arguments_json, event.agent_id
        )
        if event.call_id:
            self._cards[event.call_id] = card
        if event.name == "run_command":
            self._last_shell = card
        self._mount_card(card)

    def add_output(self, event: CommandOutputChunk) -> None:
        card = None
        if event.call_id:
            card = self._cards.get(event.call_id)
        if card is None:
            card = self._last_shell
        if card is None:
            return
        card.add_chunk(event.stream, event.text)
        self.scroll_end(animate=False)

    def finish_call(self, event: ToolCallFinished) -> None:
        card = self._cards.get(event.call_id)
        if card is None:
            card = ToolCard(
                event.call_id, event.name, "{}", event.agent_id
            )
            if event.call_id:
                self._cards[event.call_id] = card
            self._mount_card(card)
        card.finish(event.ok, event.duration_ms, event.preview)
        self.scroll_end(animate=False)

    def _mount_card(self, card: ToolCard) -> None:
        if self.query(ToolCard):
            self.mount(Rule(classes="item-sep"))
        self.mount(card)
        self.scroll_end(animate=False)


class AgentsPanel(Static):
    def __init__(self, **kwargs) -> None:
        super().__init__(self._markup([]), markup=True, **kwargs)
        self._rows = []
        self._streams: dict[str, dict[str, str]] = {}

    def set_agents(self, rows) -> None:
        self._rows = list(rows or [])
        live = {row.id for row in self._rows}
        self._streams = {
            agent_id: buf
            for agent_id, buf in self._streams.items()
            if agent_id in live
        }
        self._refresh()

    def start_stream(self, event: ChatMessageStarted) -> None:
        if not event.agent_id:
            return
        self._streams[event.agent_id] = {"id": event.id, "text": "", "reasoning": ""}
        self._refresh()

    def append_delta(self, event: ChatMessageDelta) -> None:
        if not event.agent_id or not event.text:
            return
        buf = self._streams.setdefault(
            event.agent_id, {"id": event.id, "text": "", "reasoning": ""}
        )
        if buf["id"] != event.id:
            buf["id"] = event.id
            buf["text"] = ""
            buf["reasoning"] = ""
        key = "reasoning" if event.channel == "reasoning" else "text"
        buf[key] += event.text
        self._refresh()

    def finish_stream(self, event: ChatMessageAdded) -> None:
        if not event.agent_id:
            return
        buf = self._streams.setdefault(
            event.agent_id, {"id": event.id, "text": "", "reasoning": ""}
        )
        buf["id"] = event.id
        if event.text:
            buf["text"] = event.text
        self._refresh()

    def _refresh(self) -> None:
        count = len(self._rows)
        self.border_title = f"agents · {count}"
        self.update(self._markup(self._rows))

    def _markup(self, rows) -> str:
        if not rows:
            return "[dim]none running[/dim]"
        groups: dict[str, list] = {}
        for row in rows:
            groups.setdefault(row.batch_id or "ungrouped", []).append(row)
        lines: list[str] = []
        for index, (batch_id, members) in enumerate(groups.items()):
            if index:
                lines.append("")
            name = next((row.batch_name for row in members if row.batch_name), "")
            short = batch_id[:8] if batch_id and batch_id != "ungrouped" else "—"
            title = name or short
            extra = f"  [dim]{escape(short)}[/dim]" if name and short != "—" else ""
            lines.append(f"[b]{escape(title)}[/b]{extra}  {len(members)}")
            for row in members:
                tool = f" · {row.current_tool}" if row.current_tool else ""
                task = (row.task or "").replace("\n", " ")
                if len(task) > 72:
                    task = task[:69] + "..."
                lines.append(
                    f"[#d4b44a]{escape(row.profile)}[/]  "
                    f"{escape(row.status)}{escape(tool)}"
                )
                if task:
                    lines.append(f"[dim]{escape(task)}[/dim]")
                stream = self._clip_stream(self._streams.get(row.id))
                if stream:
                    lines.append(f"[italic]{escape(stream)}[/italic]")
        return "\n".join(lines)

    def _clip_stream(self, buf) -> str:
        if not buf:
            return ""
        text = (buf.get("text") or "").replace("\n", " ").strip()
        if not text:
            text = (buf.get("reasoning") or "").replace("\n", " ").strip()
        if len(text) > 160:
            text = "…" + text[-157:]
        return text


class InspectModal(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss", "close", show=True)]
    CSS = """
    InspectModal {
        align: center middle;
    }
    #inspect-box {
        width: 92%;
        height: 84%;
        background: #101410;
        border: tall #4a4320;
        padding: 1;
    }
    #inspect-title {
        height: 1;
        color: #d4b44a;
        text-style: bold;
    }
    #inspect-body {
        height: 1fr;
        background: #16140e;
        color: #dce6df;
        scrollbar-color: #4a4320;
    }
    #close-inspect {
        dock: bottom;
        width: auto;
        min-width: 12;
        background: #2f4a3a;
        color: #c5d4c8;
        border: tall #3f5a4a;
    }
    """

    def __init__(self, title: str, text: str) -> None:
        super().__init__()
        self._title = title
        self._text = text

    def compose(self) -> ComposeResult:
        with Vertical(id="inspect-box"):
            yield Static(f"{self._title}  ·  esc to close", id="inspect-title")
            yield RichLog(id="inspect-body", highlight=False, markup=False, wrap=True)
            yield Button("close", id="close-inspect")

    def on_mount(self) -> None:
        log = self.query_one("#inspect-body", RichLog)
        log.write(self._text or "(empty)")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close-inspect":
            self.dismiss()


def _role_class(role: str) -> str:
    if role.startswith("user"):
        return "user"
    if role.startswith("assistant"):
        return "assistant"
    return "other"


def _pretty_args(arguments_json: str) -> str:
    if not arguments_json:
        return ""
    try:
        data = json.loads(arguments_json)
    except json.JSONDecodeError:
        return arguments_json
    if isinstance(data, dict):
        if not data:
            return ""
        return "\n".join(f"{key}: {value}" for key, value in data.items())
    return json.dumps(data, indent=2)


class DummyClientApp(App):
    """Reference engine client: chat, protocol log, and live tool calls."""

    TITLE = "engine"
    CSS = """
    Screen {
        background: #101410;
        color: #dce6df;
    }

    Header {
        background: #1a221c;
        color: #c5d4c8;
    }

    #body {
        height: 1fr;
    }

    #chat {
        width: 2fr;
        height: 1fr;
        border: tall #2f4a3a;
        border-title-color: #8fbf9f;
        border-title-style: bold;
        background: #121814;
        scrollbar-color: #2f4a3a;
    }

    #right {
        width: 1fr;
        height: 1fr;
    }

    #agents {
        height: auto;
        max-height: 24;
        border: tall #3a4a2f;
        border-title-color: #a8c47a;
        border-title-style: bold;
        background: #12160e;
        padding: 0 1 1 1;
        color: #dce6df;
    }

    #protocol {
        height: 1fr;
        border: tall #4a4320;
        border-title-color: #d4b44a;
        border-title-style: bold;
        background: #16140e;
        scrollbar-color: #4a4320;
    }

    #tools {
        height: 1fr;
        border: tall #4a3220;
        border-title-color: #d4884a;
        border-title-style: bold;
        background: #16110e;
        scrollbar-color: #4a3220;
    }

    #prompt-row {
        dock: bottom;
        height: 3;
        background: #121814;
    }

    #prompt {
        width: 1fr;
        background: #121814;
        border: tall #2f4a3a;
        padding: 0 1;
    }

    #snapshot-btn {
        min-width: 16;
        height: 3;
        background: #2f4a3a;
        color: #c5d4c8;
        border: tall #3f5a4a;
    }

    #snapshot-btn:hover,
    #context-btn:hover {
        background: #3a5c48;
    }

    #context-btn {
        min-width: 14;
        height: 3;
        background: #2f4a3a;
        color: #c5d4c8;
        border: tall #3f5a4a;
    }

    .item-sep {
        height: 1;
        margin: 0 1;
    }

    #chat .item-sep {
        color: #2f4a3a;
    }

    #tools .item-sep {
        color: #5a3f28;
    }

    .chat-user {
        color: #8ec8d8;
        padding: 0 1 1 1;
    }

    .chat-assistant {
        color: #dce6df;
        padding: 0 1 1 1;
    }

    .chat-other {
        color: #b8c4bc;
        padding: 0 1 1 1;
    }

    .chat-history {
        color: #7a8a80;
        padding: 0 1 1 1;
    }

    .chat-prompt {
        color: #e6c36a;
        background: #2a2412;
        padding: 1 1;
        margin-bottom: 1;
    }

    .tool-card {
        padding: 0 1 1 1;
        margin-bottom: 1;
    }

    .tool-running {
        color: #e6d7a3;
    }

    .tool-ok {
        color: #c5d4c8;
    }

    .tool-error {
        color: #f0c4c4;
    }
    """
    BINDINGS = [
        Binding("f5", "snapshot", "snapshot", show=True, priority=True),
        Binding("f6", "context", "context", show=True, priority=True),
        Binding("ctrl+c", "quit", "quit", show=False, priority=True),
        Binding("ctrl+q", "quit", "quit", show=False),
    ]

    def __init__(self, workspace: Path, socket_path: Path) -> None:
        super().__init__()
        self.workspace = workspace
        self.socket_path = socket_path
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._agent_state = "connecting"
        self._turn = 0
        self._max_turns = 0
        self._stats_line = ""
        self._open_snapshot_modal = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="body"):
            chat = ChatPanel(id="chat")
            chat.border_title = "chat"
            yield chat
            with Vertical(id="right"):
                agents = AgentsPanel(id="agents")
                agents.border_title = "agents · 0"
                yield agents
                protocol = ProtocolLog(id="protocol")
                protocol.border_title = "protocol"
                yield protocol
                tools = ToolsPanel(id="tools")
                tools.border_title = "tools"
                yield tools
        with Horizontal(id="prompt-row"):
            yield Input(
                placeholder="message or /command  ·  help, abort, undo, snapshot, context, exit",
                id="prompt",
            )
            yield Button("snapshot", id="snapshot-btn")
            yield Button("context", id="context-btn")
        yield Footer()

    async def on_mount(self) -> None:
        self.title = f"engine · {self.workspace.name}"
        self._refresh_status()
        try:
            self._reader, self._writer = await asyncio.open_unix_connection(
                str(self.socket_path), limit=STREAM_LIMIT
            )
        except (ConnectionRefusedError, FileNotFoundError, OSError) as exc:
            self.query_one("#protocol", ProtocolLog).log_note(
                f"could not connect to {self.socket_path}: {exc}"
            )
            self._agent_state = "offline"
            self._refresh_status()
            return
        self._agent_state = "idle"
        self._refresh_status()
        self.run_worker(self._read_events, exclusive=True, name="events")
        await self.send_command(StartSession(workspace=str(self.workspace)))
        self.query_one("#prompt", Input).focus()

    async def on_unmount(self) -> None:
        await self._close_socket()

    async def action_quit(self) -> None:
        await self._close_socket()
        self.exit()

    async def action_snapshot(self) -> None:
        await self.send_command(RequestSnapshot(replay=False))

    async def action_context(self) -> None:
        await self.send_command(RequestOrchContext())

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "snapshot-btn":
            await self.action_snapshot()
        elif event.button.id == "context-btn":
            await self.action_context()

    async def _close_socket(self) -> None:
        writer = self._writer
        self._writer = None
        if writer is None:
            return
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()

    async def _read_events(self) -> None:
        reader = self._reader
        if reader is None:
            return
        while True:
            try:
                line = await reader.readline()
            except (asyncio.LimitOverrunError, ValueError) as exc:
                self.post_message(StreamNotice(f"bad event stream: {exc}"))
                break
            except asyncio.CancelledError:
                raise
            except OSError as exc:
                self.post_message(StreamNotice(f"disconnected: {exc}"))
                break
            if not line:
                self.post_message(StreamNotice("disconnected from server"))
                break
            try:
                event = decode_event(line)
            except Exception as exc:  # noqa: BLE001
                self.post_message(StreamNotice(f"bad event: {exc}"))
                continue
            self.post_message(EventReceived(event))

    def on_event_received(self, message: EventReceived) -> None:
        self._dispatch(message.event)

    def on_stream_notice(self, message: StreamNotice) -> None:
        self.query_one("#protocol", ProtocolLog).log_note(message.text)
        if message.text.startswith("disconnected") or message.text.startswith("bad event stream"):
            self._agent_state = "offline"
            self._refresh_status()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        command = dummy_client.command_from_line(text, self.workspace)
        protocol = self.query_one("#protocol", ProtocolLog)
        for note in dummy_client.drain_notes():
            protocol.log_note(note)
        if command is dummy_client.CLIENT_EXIT:
            await self.action_quit()
            return
        if command is None:
            return
        await self.send_command(command)
        self._refresh_prompt()

    async def send_command(self, command) -> None:
        if isinstance(command, RequestSnapshot):
            self._open_snapshot_modal = True
            if command.replay:
                command = RequestSnapshot(replay=False)
        protocol = self.query_one("#protocol", ProtocolLog)
        protocol.log_outbound(command)
        if self._writer is None:
            protocol.log_note("not connected")
            return
        try:
            self._writer.write(encode(command))
            await self._writer.drain()
        except OSError as exc:
            protocol.log_note(f"send failed: {exc}")
            self._agent_state = "offline"
            self._refresh_status()

    def _dispatch(self, event) -> None:
        dest = dummy_client.route_event(event)
        if dest == "chat":
            self._dispatch_chat(event)
        elif dest == "tools":
            self._dispatch_tools(event)
        elif dest == "agents":
            self._dispatch_agents(event)
        elif dest == "context":
            self._dispatch_context(event)
        else:
            if isinstance(event, SnapshotReady) and self._open_snapshot_modal:
                self._open_snapshot_modal = False
                if event.snapshot.agents is not None:
                    self.query_one("#agents", AgentsPanel).set_agents(
                        event.snapshot.agents
                    )
                text = dummy_client.format_event(event)
                self.query_one("#protocol", ProtocolLog).log_note(
                    f"snapshot ({event.snapshot.message_count} messages)"
                )
                self.push_screen(InspectModal("snapshot", text))
                return
            text = dummy_client.format_event(event)
            self.query_one("#protocol", ProtocolLog).log_inbound(text)
        if isinstance(event, SnapshotReady) and event.snapshot.agents is not None:
            self.query_one("#agents", AgentsPanel).set_agents(event.snapshot.agents)
        if isinstance(event, AgentStateChanged):
            self._agent_state = event.state
            self._turn = event.turn
            self._max_turns = event.max_turns
            self._refresh_status()
        elif isinstance(event, StatsUpdated):
            stats = event.stats
            self._stats_line = (
                f"{stats.total_tokens} tok · ${stats.cost:.3f} · {stats.elapsed_s:.1f}s"
            )
            self._refresh_status()

    def _dispatch_chat(self, event) -> None:
        chat = self.query_one("#chat", ChatPanel)
        if isinstance(event, ChatHistoryAdded):
            chat.add_history(event)
            return
        if isinstance(event, ChatMessageStarted):
            chat.start_message(event.id, event.role)
            return
        if isinstance(event, ChatMessageDelta):
            if event.channel == "reasoning":
                return
            chat.append_delta(event.id, event.text)
            return
        if isinstance(event, ChatMessageAdded):
            chat.add_message(event)
            return
        if isinstance(event, UserPromptRequested):
            dummy_client._LAST_PROMPT_ID = event.prompt_id
            dummy_client._LAST_PROMPT_CHOICES = list(event.choices or [])
            chat.add_prompt(event)
            self._refresh_prompt()

    def _dispatch_tools(self, event) -> None:
        tools = self.query_one("#tools", ToolsPanel)
        if isinstance(event, ToolCallStarted):
            tools.start_call(event)
        elif isinstance(event, CommandOutputChunk):
            tools.add_output(event)
        elif isinstance(event, ToolCallFinished):
            tools.finish_call(event)

    def _dispatch_agents(self, event) -> None:
        panel = self.query_one("#agents", AgentsPanel)
        if isinstance(event, AgentsUpdated):
            panel.set_agents(event.agents)
        elif isinstance(event, ChatMessageStarted):
            panel.start_stream(event)
        elif isinstance(event, ChatMessageDelta):
            panel.append_delta(event)
        elif isinstance(event, ChatMessageAdded):
            panel.finish_stream(event)

    def _dispatch_context(self, event) -> None:
        if not isinstance(event, OrchContext):
            return
        self.query_one("#protocol", ProtocolLog).log_note(
            f"orch context ({len(event.text)} chars)"
        )
        self.push_screen(InspectModal("orch context", event.text))

    def _refresh_status(self) -> None:
        parts = [self._agent_state]
        if self._max_turns:
            parts.append(f"turn {self._turn}/{self._max_turns}")
        if self._stats_line:
            parts.append(self._stats_line)
        self.sub_title = "  ·  ".join(parts)
        self._refresh_prompt()

    def _refresh_prompt(self) -> None:
        try:
            box = self.query_one("#prompt", Input)
        except Exception:
            return
        if dummy_client._LAST_PROMPT_ID:
            box.placeholder = "answer>  type the answer, or: answer <text>"
        else:
            box.placeholder = "message or /command  ·  help, abort, undo, exit"
