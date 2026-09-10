from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from tools.base import Tool

SpawnFn = Callable[[str, str], Awaitable[str]]

NAV = ["list_files", "read_file", "search"]
SKILLS = ["activate_skill", "read_skill"]
MCP = ["mcp"]
SITTER = ["list_symbols", "find_symbol", "get_node_at", "query_tree", "parse_file"]
LSP = [
    "goto_definition",
    "find_references",
    "hover",
    "get_diagnostics",
    "document_symbols",
]
EDIT = [
    "str_replace",
    "replace_lines",
    "insert_at_line",
    "create_file",
    "replace_symbol",
    "insert_after_imports",
    "apply_patch",
    "rename_symbol",
    "undo_edit",
    "list_edits",
]
GIT = ["git_status", "git_diff"]
SHELL = ["run_command"]
WEB = ["web_search", "web_fetch"]
BROWSER = [
    "browser_open",
    "browser_console",
    "browser_screenshot",
    "browser_network",
]
# Appended to every subagent system prompt. The child never talks to the user.
REPORT_TO_ORCH = (
    "Your only reader is the orchestrator, not a human. "
    "Final reply: no markdown, headings, bullets, or filler. "
    "A few labeled lines — paths, facts, verdict, leftover questions. "
    "Omit empty fields. Do not explain yourself."
)

TEST_GLOBS = [
    "**/test_*.py",
    "**/*_test.py",
    "**/tests/**",
    "**/__tests__/**",
    "**/*.spec.*",
    "**/*.test.*",
    "**/cypress/**",
    "**/e2e/**",
]


@dataclass
class AgentProfile:
    name: str
    description: str
    system_prompt: str
    tool_names: list[str]
    write_globs: list[str] | None = None
    required_tools: list[str] = field(default_factory=list)
    max_turns: int = 16
    model: str | None = None
    temperature: float = 0.2
    needs_worktree: bool = False
    join_worktree: bool = False


class ProfileRegistry:
    def __init__(self) -> None:
        self._profiles: dict[str, AgentProfile] = {}
        self.errors: list[str] = []

    def register(self, profile: AgentProfile) -> None:
        if profile.name in self._profiles:
            self.errors.append(f"duplicate profile name: {profile.name}")
            return
        self._profiles[profile.name] = profile

    def get(self, name: str) -> AgentProfile:
        profile = self._profiles.get(name)
        if profile is None:
            raise KeyError(name)
        return profile

    def list(self) -> list[AgentProfile]:
        return list(self._profiles.values())

    def names(self) -> set[str]:
        return set(self._profiles)

    def as_tools(self, spawn: SpawnFn) -> list[Tool]:
        tools: list[Tool] = []
        for profile in self.list():
            tools.append(_spawn_tool(profile, spawn))
        return tools


def _spawn_tool(profile: AgentProfile, spawn: SpawnFn) -> Tool:
    name = profile.name

    async def execute(ctx, task: str) -> str:  # noqa: ARG001
        return await spawn(name, task)

    return Tool(
        name=name,
        description=profile.description,
        parameters={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "The task for this agent. Be specific: paths, "
                        "expected outcome, constraints."
                    ),
                }
            },
            "required": ["task"],
        },
        fn=execute,
    )


def discover_profiles() -> ProfileRegistry:
    import agents.profiles as pkg

    registry = ProfileRegistry()
    prefix = pkg.__name__ + "."
    skip = {pkg.__name__ + ".common"}
    for info in pkgutil.walk_packages(pkg.__path__, prefix):
        if info.name in skip or info.name.rsplit(".", 1)[-1].startswith("_"):
            continue
        try:
            module = importlib.import_module(info.name)
            module = importlib.reload(module)
        except Exception as exc:  # noqa: BLE001
            registry.errors.append(f"{info.name}: {exc}")
            continue
        profile = getattr(module, "PROFILE", None)
        if isinstance(profile, AgentProfile):
            registry.register(profile)
    return registry
