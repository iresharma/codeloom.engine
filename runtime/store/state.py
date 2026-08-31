from __future__ import annotations

from dataclasses import dataclass, field

from protocol.snapshot import (
    ChatMessage,
    EngineSnapshot,
    FileTreeNode,
    GitState,
    PendingPrompt,
    Stats,
)


@dataclass
class SessionState:
    session_id: str | None = None
    messages: list[ChatMessage] = field(default_factory=list)
    open_files: list[str] = field(default_factory=list)
    ended: bool = False
    stats: Stats = field(default_factory=Stats)
    pending_prompt: PendingPrompt | None = None

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
            file_tree_count=_tree_count(file_tree),
            stats=self.stats,
            pending_prompt=self.pending_prompt,
        )

    @classmethod
    def from_snapshot(cls, snap: EngineSnapshot) -> SessionState:
        return cls(
            session_id=snap.session_id,
            messages=list(snap.messages),
            open_files=list(snap.open_files),
            ended=False,
            stats=snap.stats or Stats(),
            pending_prompt=None,
        )


def _tree_count(nodes: list[FileTreeNode]) -> int:
    total = 0
    for node in nodes:
        total += 1
        if node.children:
            total += _tree_count(node.children)
    return total
