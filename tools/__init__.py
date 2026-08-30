from tools.admin import admin_tools
from tools.base import BaseTool, ToolContext, ToolResult
from tools.executor import executor_tools
from tools.files import file_tools
from tools.git import git_tools
from tools.lsp import lsp_tools
from tools.registry import ToolRegistry
from tools.sitter import sitter_tools

ORCHESTRATOR_TOOL_NAMES = [
    "list_tree",
    "read_file",
    "write_file",
    "edit_file",
    "search_files",
    "git_status",
    "git_diff",
    "git_log",
    "git_branch",
    "git_add",
    "git_commit",
    "parse_file",
    "query_tree",
    "get_node_at",
    "list_symbols",
    "get_diagnostics",
    "go_to_definition",
    "find_references",
    "hover",
    "document_symbols",
    "ask_user",
    "spawn_subagent",
    "list_profiles",
    "write_context",
    "list_agents",
    "abort_agent",
    "run_command",
]


def build_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for group in (
        file_tools(),
        git_tools(),
        sitter_tools(),
        lsp_tools(),
        admin_tools(),
        executor_tools(),
    ):
        for tool in group:
            registry.register(tool)
    return registry


__all__ = [
    "BaseTool",
    "ORCHESTRATOR_TOOL_NAMES",
    "ToolContext",
    "ToolRegistry",
    "ToolResult",
    "build_registry",
]
