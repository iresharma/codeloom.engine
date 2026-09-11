from __future__ import annotations

from dataclasses import dataclass, field

from protocol.message import ProtocolMessage
from protocol.snapshot import (
    AgentRow,
    ContextSection,
    EngineSnapshot,
    FileTreeNode,
    GitState,
    McpServerRow,
    MemoryDecision,
    MemoryFileNote,
    PendingPrompt,
    SessionSummary,
    SkillRow,
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
    agent_id: str = ""


@event
@dataclass
class ChatHistoryAdded(ProtocolMessage):
    id: str
    role: str
    text: str
    ts: str
    index: int
    total: int
    agent_id: str = ""


@event
@dataclass
class ChatHistoryComplete(ProtocolMessage):
    count: int
    agent_id: str = ""


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
    agent_id: str = ""


@event
@dataclass
class ChatMessageDelta(ProtocolMessage):
    id: str
    channel: str
    text: str
    agent_id: str = ""


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
    cost: float = 0.0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0
    requests: int = 0


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


@event
@dataclass
class McpServersUpdated(ProtocolMessage):
    servers: list[McpServerRow]


@event
@dataclass
class SkillCatalogUpdated(ProtocolMessage):
    skills: list[SkillRow]


@event
@dataclass
class SkillActivated(ProtocolMessage):
    name: str
    agent_id: str = ""


@event
@dataclass
class McpAuthRequired(ProtocolMessage):
    server: str
    url: str = ""
    prompt_id: str = ""


@event
@dataclass
class PathChanged(ProtocolMessage):
    path: str
    action: str
    dest: str = ""


@event
@dataclass
class ContextBreakdown(ProtocolMessage):
    agent_id: str = ""
    budget: int = 0
    prompt_tokens: int = 0
    compacted: bool = False
    sections: list[ContextSection] = field(default_factory=list)


@event
@dataclass
class MemoryUpdated(ProtocolMessage):
    files: list[MemoryFileNote] = field(default_factory=list)
    engineering: list[MemoryDecision] = field(default_factory=list)
    product: list[MemoryDecision] = field(default_factory=list)
    cicd: list[MemoryDecision] = field(default_factory=list)
    other: list[MemoryDecision] = field(default_factory=list)
