from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from llm.provider import Usage


@dataclass
class AgentHooks:
    on_tool: Callable[[str, dict, str], None] | None = None
    on_tool_start: Callable[[str, str, dict], None] | None = None
    on_delta: Callable[[str, str, str], None] | None = None
    on_message_start: Callable[[str], None] | None = None
    on_usage: Callable[[Usage], None] | None = None
    on_state: Callable[[str, int, int], None] | None = None
    on_compact: Callable[[dict], None] | None = None
