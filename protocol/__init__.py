from protocol.codec import ProtocolError, decode_command, decode_event, encode
from protocol.commands import COMMANDS, Command
from protocol.events import EVENTS, Event
from protocol.message import ProtocolMessage
from protocol.snapshot import (
    ChatMessage,
    EngineSnapshot,
    FileTreeNode,
    GitState,
    PendingPrompt,
    SessionSummary,
    Stats,
)

globals().update(COMMANDS)
globals().update(EVENTS)

__all__ = [
    "COMMANDS",
    "EVENTS",
    "ChatMessage",
    "Command",
    "EngineSnapshot",
    "Event",
    "FileTreeNode",
    "GitState",
    "PendingPrompt",
    "ProtocolError",
    "ProtocolMessage",
    "SessionSummary",
    "Stats",
    "decode_command",
    "decode_event",
    "encode",
]
__all__.extend(COMMANDS)
__all__.extend(EVENTS)
