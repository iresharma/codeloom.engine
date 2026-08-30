from __future__ import annotations

from dataclasses import dataclass

from protocol.message import ProtocolMessage
from protocol.snapshot import EngineSnapshot, FileTreeNode, GitState, SessionSummary

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
