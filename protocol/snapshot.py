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

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "saved_at": self.saved_at,
            "message_count": self.message_count,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> SessionSummary:
        return cls(
            id=data["id"],
            saved_at=data["saved_at"],
            message_count=int(data["message_count"]),
        )


@dataclass
class EngineSnapshot:
    session_id: str
    workspace: str
    messages: list[ChatMessage]
    ended: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "type": "EngineSnapshot",
            "session_id": self.session_id,
            "workspace": self.workspace,
            "messages": [message.to_json() for message in self.messages],
            "ended": self.ended,
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
        )
