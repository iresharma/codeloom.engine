from __future__ import annotations

from dataclasses import dataclass, field

from protocol.snapshot import ChatMessage, EngineSnapshot


@dataclass
class SessionState:
    session_id: str | None = None
    messages: list[ChatMessage] = field(default_factory=list)
    ended: bool = False

    def snapshot(self, workspace: str) -> EngineSnapshot:
        if self.session_id is None:
            raise RuntimeError("no active session")
        return EngineSnapshot(
            session_id=self.session_id,
            workspace=workspace,
            messages=list(self.messages),
            ended=self.ended,
        )

    @classmethod
    def from_snapshot(cls, snap: EngineSnapshot) -> SessionState:
        return cls(
            session_id=snap.session_id,
            messages=list(snap.messages),
            ended=False,
        )
