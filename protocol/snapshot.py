from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ChatMessage:
    id: str
    role: str
    text: str
    ts: str

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role,
            "text": self.text,
            "ts": self.ts,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ChatMessage:
        return cls(
            id=data["id"],
            role=data["role"],
            text=data["text"],
            ts=data["ts"],
        )


@dataclass
class SessionSummary:
    id: str
    saved_at: str
    message_count: int
    open_files: list[str]

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "saved_at": self.saved_at,
            "message_count": self.message_count,
            "open_files": list(self.open_files),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> SessionSummary:
        return cls(
            id=data["id"],
            saved_at=data["saved_at"],
            message_count=int(data["message_count"]),
            open_files=list(data.get("open_files") or []),
        )


@dataclass
class FileTreeNode:
    name: str
    path: str
    is_dir: bool
    children: list[FileTreeNode] | None = None

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "path": self.path,
            "is_dir": self.is_dir,
        }
        if self.children is not None:
            payload["children"] = [child.to_json() for child in self.children]
        return payload

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> FileTreeNode:
        raw_children = data.get("children")
        children = None
        if raw_children is not None:
            children = [
                item if isinstance(item, FileTreeNode) else FileTreeNode.from_json(item)
                for item in raw_children
            ]
        return cls(
            name=data["name"],
            path=data["path"],
            is_dir=bool(data["is_dir"]),
            children=children,
        )


@dataclass
class GitState:
    branch: str | None
    dirty: bool
    staged: list[str]
    unstaged: list[str]
    untracked: list[str]
    staged_diff: str
    unstaged_diff: str

    def to_json(self) -> dict[str, Any]:
        return {
            "branch": self.branch,
            "dirty": self.dirty,
            "staged": list(self.staged),
            "unstaged": list(self.unstaged),
            "untracked": list(self.untracked),
            "staged_diff": self.staged_diff,
            "unstaged_diff": self.unstaged_diff,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> GitState:
        if not data:
            return cls.empty()
        return cls(
            branch=data.get("branch"),
            dirty=bool(data.get("dirty", False)),
            staged=list(data.get("staged") or []),
            unstaged=list(data.get("unstaged") or []),
            untracked=list(data.get("untracked") or []),
            staged_diff=data.get("staged_diff") or "",
            unstaged_diff=data.get("unstaged_diff") or "",
        )

    @classmethod
    def empty(cls) -> GitState:
        return cls(
            branch=None,
            dirty=False,
            staged=[],
            unstaged=[],
            untracked=[],
            staged_diff="",
            unstaged_diff="",
        )


@dataclass
class EngineSnapshot:
    session_id: str
    workspace: str
    messages: list[ChatMessage]
    ended: bool
    open_files: list[str]
    file_tree: list[FileTreeNode]
    git: GitState
    language: str | None = None
    language_supported: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "type": "EngineSnapshot",
            "session_id": self.session_id,
            "workspace": self.workspace,
            "messages": [message.to_json() for message in self.messages],
            "ended": self.ended,
            "open_files": list(self.open_files),
            "file_tree": [node.to_json() for node in self.file_tree],
            "git": self.git.to_json(),
            "language": self.language,
            "language_supported": self.language_supported,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> EngineSnapshot:
        return cls(
            session_id=data["session_id"],
            workspace=data["workspace"],
            messages=[
                item if isinstance(item, ChatMessage) else ChatMessage.from_json(item)
                for item in data.get("messages", [])
            ],
            ended=bool(data.get("ended", False)),
            open_files=list(data.get("open_files") or []),
            file_tree=[
                item if isinstance(item, FileTreeNode) else FileTreeNode.from_json(item)
                for item in data.get("file_tree") or []
            ],
            git=GitState.from_json(data.get("git")),
            language=data.get("language"),
            language_supported=bool(data.get("language_supported", False)),
        )
