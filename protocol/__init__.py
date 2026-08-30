from protocol.codec import ProtocolError, decode_command, decode_event, encode
from protocol.commands import COMMANDS, Command
from protocol.events import EVENTS, Event
from protocol.message import ProtocolMessage
from protocol.snapshot import ChatMessage, EngineSnapshot, FileTreeNode, GitState, SessionSummary

globals().update(COMMANDS)
globals().update(EVENTS)

__all__ = [
    "COMMANDS",
    "EVENTS",
    "Command",
    "Event",
    "ProtocolError",
    "ProtocolMessage",
    "ChatMessage",
    "EngineSnapshot",
    "FileTreeNode",
    "GitState",
    "SessionSummary",
    "decode_command",
    "decode_event",
    "encode",
    *COMMANDS,
    *EVENTS,
]
