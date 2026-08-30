from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Union

from protocol.codec import from_dict, to_dict


@dataclass
class StartSession:
    workspace: str
    id: str | None = None
    cmd: str = field(default="start_session", init=False)

    def to_json(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> StartSession:
        return from_dict(cls, data)


@dataclass
class SubmitUserMessage:
    text: str
    id: str | None = None
    cmd: str = field(default="submit_user_message", init=False)

    def to_json(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> SubmitUserMessage:
        return from_dict(cls, data)


@dataclass
class AnswerPrompt:
    prompt_id: str
    text: str
    id: str | None = None
    cmd: str = field(default="answer_prompt", init=False)

    def to_json(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> AnswerPrompt:
        return from_dict(cls, data)


@dataclass
class OpenFile:
    path: str
    id: str | None = None
    cmd: str = field(default="open_file", init=False)

    def to_json(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> OpenFile:
        return from_dict(cls, data)


@dataclass
class CloseFile:
    path: str
    id: str | None = None
    cmd: str = field(default="close_file", init=False)

    def to_json(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> CloseFile:
        return from_dict(cls, data)


@dataclass
class RequestSnapshot:
    id: str | None = None
    cmd: str = field(default="request_snapshot", init=False)

    def to_json(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> RequestSnapshot:
        return from_dict(cls, data)


@dataclass
class AbortAgent:
    agent_id: str | None = None
    id: str | None = None
    cmd: str = field(default="abort_agent", init=False)

    def to_json(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> AbortAgent:
        return from_dict(cls, data)


@dataclass
class Shutdown:
    id: str | None = None
    cmd: str = field(default="shutdown", init=False)

    def to_json(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Shutdown:
        return from_dict(cls, data)


Command = Union[
    StartSession,
    SubmitUserMessage,
    AnswerPrompt,
    OpenFile,
    CloseFile,
    RequestSnapshot,
    AbortAgent,
    Shutdown,
]

_COMMANDS: dict[str, type] = {
    "start_session": StartSession,
    "submit_user_message": SubmitUserMessage,
    "answer_prompt": AnswerPrompt,
    "open_file": OpenFile,
    "close_file": CloseFile,
    "request_snapshot": RequestSnapshot,
    "abort_agent": AbortAgent,
    "shutdown": Shutdown,
}


def parse_command(data: dict[str, Any]) -> Command:
    name = data.get("cmd") or data.get("type")
    if name not in _COMMANDS:
        raise ValueError(f"unknown command: {name!r}")
    return _COMMANDS[name].from_json(data)
