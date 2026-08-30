from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from protocol.codec import from_dict, to_dict


@dataclass
class ChatMessage:
    role: str
    content: str
    agent_id: str | None = None
    profile: str | None = None

    def to_json(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ChatMessage:
        return from_dict(cls, data)


@dataclass
class FileTreeNode:
    name: str
    path: str
    is_dir: bool
    children: list[FileTreeNode] | None = None

    def to_json(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> FileTreeNode:
        payload = dict(data)
        raw_children = payload.get("children")
        if raw_children is not None:
            payload["children"] = [
                child if isinstance(child, FileTreeNode) else FileTreeNode.from_json(child)
                for child in raw_children
            ]
        return from_dict(cls, payload)


@dataclass
class OpenFileState:
    path: str
    content: str

    def to_json(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> OpenFileState:
        return from_dict(cls, data)


@dataclass
class AgentRow:
    id: str
    role: str
    status: str
    profile: str | None = None
    current_tool: str | None = None
    parent_id: str | None = None

    def to_json(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> AgentRow:
        return from_dict(cls, data)


@dataclass
class Stats:
    tokens_in: int = 0
    tokens_out: int = 0
    elapsed_s: float = 0.0
    active_agents: int = 0
    cost: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Stats:
        return from_dict(cls, data)


@dataclass
class GitState:
    branch: str = ""
    dirty: bool = False
    staged_diff: str = ""
    unstaged_diff: str = ""
    status_lines: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> GitState:
        return from_dict(cls, data)


@dataclass
class PendingPrompt:
    id: str
    question: str

    def to_json(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> PendingPrompt:
        return from_dict(cls, data)


@dataclass
class EngineSnapshot:
    workspace: str
    messages: list[ChatMessage] = field(default_factory=list)
    open_files: list[OpenFileState] = field(default_factory=list)
    file_tree: list[FileTreeNode] = field(default_factory=list)
    agents: list[AgentRow] = field(default_factory=list)
    stats: Stats = field(default_factory=Stats)
    git: GitState = field(default_factory=GitState)
    pending_prompt: PendingPrompt | None = None

    def to_json(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> EngineSnapshot:
        payload = dict(data)
        payload["messages"] = [
            item if isinstance(item, ChatMessage) else ChatMessage.from_json(item)
            for item in payload.get("messages") or []
        ]
        payload["open_files"] = [
            item if isinstance(item, OpenFileState) else OpenFileState.from_json(item)
            for item in payload.get("open_files") or []
        ]
        payload["file_tree"] = [
            item if isinstance(item, FileTreeNode) else FileTreeNode.from_json(item)
            for item in payload.get("file_tree") or []
        ]
        payload["agents"] = [
            item if isinstance(item, AgentRow) else AgentRow.from_json(item)
            for item in payload.get("agents") or []
        ]
        if isinstance(payload.get("stats"), dict):
            payload["stats"] = Stats.from_json(payload["stats"])
        if isinstance(payload.get("git"), dict):
            payload["git"] = GitState.from_json(payload["git"])
        if isinstance(payload.get("pending_prompt"), dict):
            payload["pending_prompt"] = PendingPrompt.from_json(payload["pending_prompt"])
        return from_dict(cls, payload)
