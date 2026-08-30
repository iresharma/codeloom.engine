from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Union


@dataclass
class StartSession:
    workspace: str
    session_id: str | None = None

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": "StartSession",
            "workspace": self.workspace,
        }
        if self.session_id is not None:
            payload["session_id"] = self.session_id
        return payload

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> StartSession:
        session_id = data.get("session_id") or None
        return cls(workspace=data["workspace"], session_id=session_id)


@dataclass
class ListSessions:
    def to_json(self) -> dict[str, Any]:
        return {"type": "ListSessions"}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ListSessions:
        return cls()


@dataclass
class SubmitUserMessage:
    text: str

    def to_json(self) -> dict[str, Any]:
        return {"type": "SubmitUserMessage", "text": self.text}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> SubmitUserMessage:
        return cls(text=data["text"])


@dataclass
class RequestSnapshot:
    def to_json(self) -> dict[str, Any]:
        return {"type": "RequestSnapshot"}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> RequestSnapshot:
        return cls()


@dataclass
class OpenFile:
    path: str

    def to_json(self) -> dict[str, Any]:
        return {"type": "OpenFile", "path": self.path}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> OpenFile:
        return cls(path=data["path"])


@dataclass
class CloseFile:
    path: str

    def to_json(self) -> dict[str, Any]:
        return {"type": "CloseFile", "path": self.path}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> CloseFile:
        return cls(path=data["path"])


@dataclass
class Shutdown:
    def to_json(self) -> dict[str, Any]:
        return {"type": "Shutdown"}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Shutdown:
        return cls()


Command = Union[
    StartSession,
    ListSessions,
    SubmitUserMessage,
    RequestSnapshot,
    OpenFile,
    CloseFile,
    Shutdown,
]

COMMANDS: dict[str, type[Command]] = {
    "StartSession": StartSession,
    "ListSessions": ListSessions,
    "SubmitUserMessage": SubmitUserMessage,
    "RequestSnapshot": RequestSnapshot,
    "OpenFile": OpenFile,
    "CloseFile": CloseFile,
    "Shutdown": Shutdown,
}
