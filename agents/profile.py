from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AgentProfile:
    name: str
    description: str
    system_prompt: str
    tool_names: list[str]
    model: str = "gpt-4o"
    max_turns: int = 30
    temperature: float = 0.2


class ProfileRegistry:
    def __init__(self) -> None:
        self._profiles: dict[str, AgentProfile] = {}

    def register(self, profile: AgentProfile) -> None:
        if "spawn_subagent" in profile.tool_names or "write_context" in profile.tool_names:
            raise ValueError(f"profile {profile.name!r} cannot include admin tools")
        self._profiles[profile.name] = profile

    def get(self, name: str) -> AgentProfile:
        if name not in self._profiles:
            raise KeyError(f"unknown profile: {name}")
        return self._profiles[name]

    def list(self) -> list[AgentProfile]:
        return list(self._profiles.values())


def built_in_registry() -> ProfileRegistry:
    from agents.profiles.ask import PROFILE as ask
    from agents.profiles.editor import PROFILE as editor
    from agents.profiles.linter import PROFILE as linter
    from agents.profiles.reviewer import PROFILE as reviewer

    registry = ProfileRegistry()
    for profile in (ask, linter, editor, reviewer):
        registry.register(profile)
    return registry
