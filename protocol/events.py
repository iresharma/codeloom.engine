from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Union

from protocol.codec import from_dict, to_dict
from protocol.snapshot import (
    AgentRow,
    ChatMessage,
    FileTreeNode,
    GitState,
    PendingPrompt,
    Stats,
)


@dataclass
class ChatMessageAdded:
    message: ChatMessage
    event: str = field(default="chat_message_added", init=False)

    def to_json(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ChatMessageAdded:
        payload = dict(data)
        if isinstance(payload.get("message"), dict):
            payload["message"] = ChatMessage.from_json(payload["message"])
        return from_dict(cls, payload)


@dataclass
class UserPromptRequested:
    prompt: PendingPrompt
    event: str = field(default="user_prompt_requested", init=False)

    def to_json(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> UserPromptRequested:
        payload = dict(data)
        if isinstance(payload.get("prompt"), dict):
            payload["prompt"] = PendingPrompt.from_json(payload["prompt"])
        return from_dict(cls, payload)


@dataclass
class FileTreeUpdated:
    tree: list[FileTreeNode]
    event: str = field(default="file_tree_updated", init=False)

    def to_json(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> FileTreeUpdated:
        payload = dict(data)
        raw_tree = payload.get("tree") or []
        payload["tree"] = [
            node if isinstance(node, FileTreeNode) else FileTreeNode.from_json(node)
            for node in raw_tree
        ]
        return from_dict(cls, payload)


@dataclass
class FileContent:
    path: str
    content: str
    event: str = field(default="file_content", init=False)

    def to_json(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> FileContent:
        return from_dict(cls, data)


@dataclass
class FileChanged:
    path: str
    diff: str
    content: str | None = None
    event: str = field(default="file_changed", init=False)

    def to_json(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> FileChanged:
        return from_dict(cls, data)


@dataclass
class AgentStarted:
    agent: AgentRow
    event: str = field(default="agent_started", init=False)

    def to_json(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> AgentStarted:
        payload = dict(data)
        if isinstance(payload.get("agent"), dict):
            payload["agent"] = AgentRow.from_json(payload["agent"])
        return from_dict(cls, payload)


@dataclass
class AgentUpdated:
    agent: AgentRow
    event: str = field(default="agent_updated", init=False)

    def to_json(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> AgentUpdated:
        payload = dict(data)
        if isinstance(payload.get("agent"), dict):
            payload["agent"] = AgentRow.from_json(payload["agent"])
        return from_dict(cls, payload)


@dataclass
class AgentFinished:
    agent: AgentRow
    final_text: str = ""
    event: str = field(default="agent_finished", init=False)

    def to_json(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> AgentFinished:
        payload = dict(data)
        if isinstance(payload.get("agent"), dict):
            payload["agent"] = AgentRow.from_json(payload["agent"])
        return from_dict(cls, payload)


@dataclass
class GitStateUpdated:
    git: GitState
    event: str = field(default="git_state_updated", init=False)

    def to_json(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> GitStateUpdated:
        payload = dict(data)
        if isinstance(payload.get("git"), dict):
            payload["git"] = GitState.from_json(payload["git"])
        return from_dict(cls, payload)


@dataclass
class StatsUpdated:
    stats: Stats
    event: str = field(default="stats_updated", init=False)

    def to_json(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> StatsUpdated:
        payload = dict(data)
        if isinstance(payload.get("stats"), dict):
            payload["stats"] = Stats.from_json(payload["stats"])
        return from_dict(cls, payload)


@dataclass
class ContextFileUpdated:
    path: str
    content: str
    event: str = field(default="context_file_updated", init=False)

    def to_json(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ContextFileUpdated:
        return from_dict(cls, data)


@dataclass
class ErrorOccurred:
    message: str
    agent_id: str | None = None
    event: str = field(default="error_occurred", init=False)

    def to_json(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ErrorOccurred:
        return from_dict(cls, data)


@dataclass
class SessionEnded:
    reason: str = "shutdown"
    event: str = field(default="session_ended", init=False)

    def to_json(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> SessionEnded:
        return from_dict(cls, data)


Event = Union[
    ChatMessageAdded,
    UserPromptRequested,
    FileTreeUpdated,
    FileContent,
    FileChanged,
    AgentStarted,
    AgentUpdated,
    AgentFinished,
    GitStateUpdated,
    StatsUpdated,
    ContextFileUpdated,
    ErrorOccurred,
    SessionEnded,
]

_EVENTS: dict[str, type] = {
    "chat_message_added": ChatMessageAdded,
    "user_prompt_requested": UserPromptRequested,
    "file_tree_updated": FileTreeUpdated,
    "file_content": FileContent,
    "file_changed": FileChanged,
    "agent_started": AgentStarted,
    "agent_updated": AgentUpdated,
    "agent_finished": AgentFinished,
    "git_state_updated": GitStateUpdated,
    "stats_updated": StatsUpdated,
    "context_file_updated": ContextFileUpdated,
    "error_occurred": ErrorOccurred,
    "session_ended": SessionEnded,
}


def parse_event(data: dict[str, Any]) -> Event:
    name = data.get("event") or data.get("type")
    if name not in _EVENTS:
        raise ValueError(f"unknown event: {name!r}")
    return _EVENTS[name].from_json(data)
