from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Union

from protocol.snapshot import EngineSnapshot, FileTreeNode, SessionSummary


@dataclass
class ChatMessageAdded:
    id: str
    role: str
    text: str
    ts: str

    def to_json(self) -> dict[str, Any]:
        return {
            "type": "ChatMessageAdded",
            "id": self.id,
            "role": self.role,
            "text": self.text,
            "ts": self.ts,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ChatMessageAdded:
        return cls(
            id=data["id"],
            role=data["role"],
            text=data["text"],
            ts=data["ts"],
        )


@dataclass
class SnapshotReady:
    snapshot: EngineSnapshot

    def to_json(self) -> dict[str, Any]:
        return {"type": "SnapshotReady", "snapshot": self.snapshot.to_json()}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> SnapshotReady:
        snap = data["snapshot"]
        if isinstance(snap, EngineSnapshot):
            return cls(snapshot=snap)
        return cls(snapshot=EngineSnapshot.from_json(snap))


@dataclass
class SessionList:
    sessions: list[SessionSummary]

    def to_json(self) -> dict[str, Any]:
        return {
            "type": "SessionList",
            "sessions": [item.to_json() for item in self.sessions],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> SessionList:
        return cls(
            sessions=[
                item if isinstance(item, SessionSummary) else SessionSummary.from_json(item)
                for item in data.get("sessions", [])
            ]
        )


@dataclass
class FileContent:
    path: str
    content: str

    def to_json(self) -> dict[str, Any]:
        return {"type": "FileContent", "path": self.path, "content": self.content}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> FileContent:
        return cls(path=data["path"], content=data["content"])


@dataclass
class FileClosed:
    path: str

    def to_json(self) -> dict[str, Any]:
        return {"type": "FileClosed", "path": self.path}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> FileClosed:
        return cls(path=data["path"])


@dataclass
class FileTreeUpdated:
    file_tree: list[FileTreeNode]

    def to_json(self) -> dict[str, Any]:
        return {
            "type": "FileTreeUpdated",
            "file_tree": [node.to_json() for node in self.file_tree],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> FileTreeUpdated:
        return cls(
            file_tree=[
                item if isinstance(item, FileTreeNode) else FileTreeNode.from_json(item)
                for item in data.get("file_tree") or []
            ]
        )


@dataclass
class ErrorOccurred:
    message: str

    def to_json(self) -> dict[str, Any]:
        return {"type": "ErrorOccurred", "message": self.message}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ErrorOccurred:
        return cls(message=data["message"])


@dataclass
class SessionEnded:
    reason: str

    def to_json(self) -> dict[str, Any]:
        return {"type": "SessionEnded", "reason": self.reason}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> SessionEnded:
        return cls(reason=data["reason"])


Event = Union[
    ChatMessageAdded,
    SnapshotReady,
    SessionList,
    FileContent,
    FileClosed,
    FileTreeUpdated,
    ErrorOccurred,
    SessionEnded,
]

EVENTS: dict[str, type[Event]] = {
    "ChatMessageAdded": ChatMessageAdded,
    "SnapshotReady": SnapshotReady,
    "SessionList": SessionList,
    "FileContent": FileContent,
    "FileClosed": FileClosed,
    "FileTreeUpdated": FileTreeUpdated,
    "ErrorOccurred": ErrorOccurred,
    "SessionEnded": SessionEnded,
}
