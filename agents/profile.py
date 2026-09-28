from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from tools.base import Tool

SpawnFn = Callable[..., Awaitable[str]]

NAV = ["list_files", "read_file", "search"]
SKILLS = ["activate_skill", "read_skill"]
MEMORY = ["remember"]
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
GIT = [
    "git_status",
    "git_diff",
    "git_log",
    "git_show",
    "git_blame",
    "git_range",
]
SHELL = ["run_command"]
TOOLCHAIN = ["toolchain"]
WEB = ["web_search", "web_fetch"]
BROWSER = [
    "browser_open",
    "browser_console",
    "browser_screenshot",
    "browser_network",
]
SERVER = ["start_server", "stop_server", "server_logs"]
GH_SOCIAL = [
    "gh_pr_list",
    "gh_pr_view",
    "gh_pr_comments",
    "gh_pr_checks",
    "gh_issue_list",
    "gh_issue_view",
    "gh_run_list",
    "gh_run_view",
    "gh_release_list",
    "gh_release_view",
]
GH_REPO = [
    "github_compare",
    "github_search_code",
    "github_file",
    "github_repo",
    "github_tree",
]
GH_READ = GH_SOCIAL + GH_REPO
GH_WRITE = ["gh_pr_comment", "gh_issue_create"]
PKG = ["pkg_info"]
DOCS = ["docs_lookup", "tldr"]
TLDR = ["tldr"]
SEC = ["osv_query"]
HTTP = ["http_request", "openapi_ops"]
OPENAPI = ["openapi_ops"]
SCAN = ["todo_scan"]
ENV = ["runtime_info"]
DEP = ["dep_why"]
# Appended to every subagent system prompt. The child never talks to the user.
REPORT_TO_ORCH = (
    "Your only reader is the orchestrator, not a human. "
    "Do not greet, apologize, or recap the task. No markdown headings or filler. "
    "End with a complete briefing as labeled blocks: what / paths / facts / "
    "verdict / leftover. A writer also adds reasoning: (why the change is "
    "shaped this way) and test_plan: (commands, cases, expected results). "
    "Those two blocks must stay complete; do not shrink them to a slogan. "
    "Facts must be specific enough that the next agent can work without "
    "re-surveying. A filename is not an answer. Finish the work before you "
    "report. Omit empty fields. If a command or fetch failed for environment "
    "reasons (missing dependency, auth, network) rather than a code bug, say "
    "so in leftover instead of retrying blindly."
)

TEST_GLOBS = [
    "**/test_*.py",
    "**/*_test.py",
    "**/*_test.go",
    "**/tests/**",
    "**/test/**",
    "**/__tests__/**",
    "**/*.spec.*",
    "**/*.test.*",
    "**/*_spec.rb",
    "**/*Test.java",
    "**/*Test.kt",
    "**/cypress/**",
    "**/e2e/**",
]
# Source files that should come with a test when the repo already has tests.
CODE_SUFFIXES = frozenset(
    {
        ".py",
        ".go",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".rs",
        ".java",
        ".kt",
        ".rb",
        ".php",
        ".cs",
        ".swift",
        ".scala",
        ".c",
        ".cc",
        ".cpp",
        ".h",
        ".hpp",
        ".ex",
        ".exs",
    }
)
_TEST_SCAN_LIMIT = 20_000
_TEST_SCAN_SKIP = frozenset(
    {".git", ".engine", "node_modules", ".venv", "venv", "__pycache__", "dist", "build"}
)


def is_test_path(rel: str) -> bool:
    from runtime.tools.writeglob import write_allowed

    return write_allowed(rel, TEST_GLOBS)


def is_code_path(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    dot = name.rfind(".")
    return dot > 0 and name[dot:].lower() in CODE_SUFFIXES


def repo_has_tests(workspace) -> bool:
    """True when the workspace already has at least one test source file."""
    from pathlib import Path

    from runtime.tools.git import tracked_paths

    root = Path(workspace)
    paths = tracked_paths(root)
    if paths is None:
        paths = []
        for index, path in enumerate(root.rglob("*")):
            if index >= _TEST_SCAN_LIMIT:
                break
            if any(part in _TEST_SCAN_SKIP for part in path.parts):
                continue
            if path.is_file():
                paths.append(path.relative_to(root).as_posix())
    return any(is_test_path(rel) and is_code_path(rel) for rel in paths)


# Pins for Default. A model chosen in the composer replaces both for the
# rest of the session, including agents spawned after a child report.
# Ask stays unpinned so Default follows OPENROUTER_MODEL.
EXTRACTOR_MODEL = "anthropic/claude-haiku-4.5"
CODER_MODEL = "openai/gpt-5.6-luna"


@dataclass
class AgentProfile:
    name: str
    description: str
    system_prompt: str
    tool_names: list[str]
    write_globs: list[str] | None = None
    required_tools: list[str] = field(default_factory=list)
    max_turns: int = 32
    max_tool_calls: int = 50
    model: str | None = None
    temperature: float = 0.2
    needs_worktree: bool = False
    join_worktree: bool = False
    # When the repo already has tests, a source edit is not finished until a
    # test path was edited too. Checked in Subagent._finish_nudge.
    requires_tests: bool = False


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

    async def execute(ctx, task: str, continue_from: str = "") -> str:  # noqa: ARG001
        return await spawn(name, task, continue_from=continue_from)

    properties = {
        "task": {
            "type": "string",
            "description": (
                "The task for this agent. Be specific: paths, "
                "expected outcome, constraints."
            ),
        }
    }
    if profile.needs_worktree:
        properties["continue_from"] = {
            "type": "string",
            "description": (
                "Optional: an existing writer agent_id whose worktree this "
                "agent should continue in, instead of starting a fresh one. "
                "Use this for a reviewer-requested fix -- a fresh worktree "
                "branches off the original base commit and cannot see that "
                "agent's diff no matter what the task text says."
            ),
        }

    return Tool(
        name=name,
        description=profile.description,
        parameters={
            "type": "object",
            "properties": properties,
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
