from __future__ import annotations

from dataclasses import dataclass

from protocol.message import ProtocolMessage
from protocol.snapshot import (
    AgentRow,
    EngineSnapshot,
    FileTreeNode,
    GitState,
    PendingPrompt,
    SessionSummary,
    Stats,
)

EVENTS: dict[str, type[ProtocolMessage]] = {}


def event(cls: type[ProtocolMessage]) -> type[ProtocolMessage]:
    EVENTS[cls.__name__] = cls
    return cls


Event = ProtocolMessage


@event
@dataclass
class ChatMessageAdded(ProtocolMessage):
    id: str
    role: str
    text: str
    ts: str


@event
@dataclass
class ChatHistoryAdded(ProtocolMessage):
    id: str
    role: str
    text: str
    ts: str
    index: int
    total: int


@event
@dataclass
class ChatHistoryComplete(ProtocolMessage):
    count: int


@event
@dataclass
class SnapshotReady(ProtocolMessage):
    snapshot: EngineSnapshot


@event
@dataclass
class SessionList(ProtocolMessage):
    sessions: list[SessionSummary]


@event
@dataclass
class FileContent(ProtocolMessage):
    path: str
    content: str


@event
@dataclass
class FileEdited(ProtocolMessage):
    path: str
    diff: str
    tool: str
    edit_id: str


@event
@dataclass
class FileClosed(ProtocolMessage):
    path: str


@event
@dataclass
class FileTreeUpdated(ProtocolMessage):
    file_tree: list[FileTreeNode]


@event
@dataclass
class GitStateUpdated(ProtocolMessage):
    git: GitState


@event
@dataclass
class ErrorOccurred(ProtocolMessage):
    message: str


@event
@dataclass
class WarningOccurred(ProtocolMessage):
    message: str


@event
@dataclass
class SessionEnded(ProtocolMessage):
    reason: str


@event
@dataclass
class ChatMessageStarted(ProtocolMessage):
    id: str
    role: str
    ts: str


@event
@dataclass
class ChatMessageDelta(ProtocolMessage):
    id: str
    channel: str
    text: str


@event
@dataclass
class ToolCallStarted(ProtocolMessage):
    call_id: str
    name: str
    arguments_json: str
    agent_id: str = ""


@event
@dataclass
class ToolCallFinished(ProtocolMessage):
    call_id: str
    name: str
    preview: str
    ok: bool
    duration_ms: int
    agent_id: str = ""


@event
@dataclass
class CommandOutputChunk(ProtocolMessage):
    call_id: str
    stream: str
    text: str
    agent_id: str = ""


@event
@dataclass
class AgentStateChanged(ProtocolMessage):
    state: str
    turn: int
    max_turns: int
    agent_id: str = ""


@event
@dataclass
class StatsUpdated(ProtocolMessage):
    stats: Stats


@event
@dataclass
class UserPromptRequested(ProtocolMessage):
    prompt_id: str
    question: str
    kind: str
    choices: list[str]
    default: str | None = None
    agent_id: str = ""


@event
@dataclass
class ContextCompacted(ProtocolMessage):
    strategy: str
    messages_before: int
    messages_after: int
    chars_saved: int
    summary: str
    agent_id: str = ""


@event
@dataclass
class AgentStarted(ProtocolMessage):
    agent_id: str
    profile: str
    parent_id: str
    task: str
    worktree: str = ""
    branch: str = ""
    batch_id: str = ""
    batch_name: str = ""


@event
@dataclass
class AgentFinished(ProtocolMessage):
    agent_id: str
    profile: str
    status: str
    summary: str


@event
@dataclass
class AgentsUpdated(ProtocolMessage):
    agents: list[AgentRow]


@event
@dataclass
class OrchContext(ProtocolMessage):
    text: str


@event
@dataclass
class WorktreeSettled(ProtocolMessage):
    agent_id: str
    profile: str
    action: str
    detail: str
    branch: str
    pr_url: str = ""
    ok: bool = True
