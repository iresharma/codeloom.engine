from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from protocol.events import Event

EmitFn = Callable[[Event], None]
RefreshFn = Callable[[], Awaitable[None]]


@dataclass
class ToolResult:
    ok: bool
    content: str
    data: dict[str, Any] = field(default_factory=dict)
    files_touched: list[str] = field(default_factory=list)


@dataclass
class ToolContext:
    workspace: Path
    agent_id: str
    role: Literal["orchestrator", "subagent"]
    emit: EmitFn
    orchestrator: Any | None = None
    session: Any | None = None
    refresh: RefreshFn | None = None


class BaseTool:
    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {}
    allowed_roles: set[str] = {"orchestrator", "subagent"}

    async def execute(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        raise NotImplementedError
