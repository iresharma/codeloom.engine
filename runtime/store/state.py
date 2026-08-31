from __future__ import annotations

from dataclasses import dataclass, field

from protocol.snapshot import ChatMessage, EngineSnapshot, FileTreeNode, GitState


@dataclass
class SessionState:
    session_id: str | None = None
    messages: list[ChatMessage] = field(default_factory=list)
    open_files: list[str] = field(default_factory=list)
    ended: bool = False

    def snapshot(
        self,
        workspace: str,
        file_tree: list[FileTreeNode],
        git: GitState,
        language: str | None = None,
        language_supported: bool = False,
    ) -> EngineSnapshot:
        if self.session_id is None:
            raise RuntimeError("no active session")
        return EngineSnapshot(
            session_id=self.session_id,
            workspace=workspace,
            messages=list(self.messages),
            ended=self.ended,
            open_files=list(self.open_files),
            file_tree=file_tree,
            git=git,
            language=language,
            language_supported=language_supported,
            message_count=len(self.messages),
        )

    @classmethod
    def from_snapshot(cls, snap: EngineSnapshot) -> SessionState:
        return cls(
            session_id=snap.session_id,
            messages=list(snap.messages),
            open_files=list(snap.open_files),
            ended=False,
        )
