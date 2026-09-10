from __future__ import annotations

from dataclasses import dataclass

from protocol.message import ProtocolMessage

COMMANDS: dict[str, type[ProtocolMessage]] = {}


def command(cls: type[ProtocolMessage]) -> type[ProtocolMessage]:
    COMMANDS[cls.__name__] = cls
    return cls


Command = ProtocolMessage


@command
@dataclass
class StartSession(ProtocolMessage):
    workspace: str
    session_id: str | None = None


@command
@dataclass
class ListSessions(ProtocolMessage):
    pass


@command
@dataclass
class SubmitUserMessage(ProtocolMessage):
    text: str


@command
@dataclass
class RequestSnapshot(ProtocolMessage):
    replay: bool = True


@command
@dataclass
class RequestOrchContext(ProtocolMessage):
    pass


@command
@dataclass
class OpenFile(ProtocolMessage):
    path: str


@command
@dataclass
class CloseFile(ProtocolMessage):
    path: str


@command
@dataclass
class RequestGit(ProtocolMessage):
    pass


@command
@dataclass
class UndoLastEdit(ProtocolMessage):
    pass


@command
@dataclass
class AbortAgent(ProtocolMessage):
    agent_id: str | None = None


@command
@dataclass
class AnswerPrompt(ProtocolMessage):
    prompt_id: str
    text: str


@command
@dataclass
class Shutdown(ProtocolMessage):
    pass


@command
@dataclass
class ReloadIntegrations(ProtocolMessage):
    pass


@command
@dataclass
class SetMcpEnabled(ProtocolMessage):
    name: str
    enabled: bool


@command
@dataclass
class ActivateSkill(ProtocolMessage):
    name: str


@command
@dataclass
class CompleteMcpAuth(ProtocolMessage):
    server: str
    token: str
