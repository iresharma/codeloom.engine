from __future__ import annotations

import time
from dataclasses import dataclass, field

from protocol.snapshot import (
    AgentRow,
    ChatMessage,
    EngineSnapshot,
    FileTreeNode,
    GitState,
    OpenFileState,
    PendingPrompt,
    Stats,
)


@dataclass
class SessionState:
    workspace: str
    started_at: float = field(default_factory=time.monotonic)
    messages: list[ChatMessage] = field(default_factory=list)
    open_files: dict[str, OpenFileState] = field(default_factory=dict)
    file_tree: list[FileTreeNode] = field(default_factory=list)
    agents: dict[str, AgentRow] = field(default_factory=dict)
    git: GitState = field(default_factory=GitState)
    pending_prompt: PendingPrompt | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0

    def stats(self) -> Stats:
        active = sum(1 for agent in self.agents.values() if agent.status in {"running", "waiting_user"})
        return Stats(
            tokens_in=self.tokens_in,
            tokens_out=self.tokens_out,
            elapsed_s=max(0.0, time.monotonic() - self.started_at),
            active_agents=active,
            cost=self.cost,
        )

    def snapshot(self) -> EngineSnapshot:
        return EngineSnapshot(
            workspace=self.workspace,
            messages=list(self.messages),
            open_files=list(self.open_files.values()),
            file_tree=list(self.file_tree),
            agents=list(self.agents.values()),
            stats=self.stats(),
            git=self.git,
            pending_prompt=self.pending_prompt,
        )
