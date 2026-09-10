from __future__ import annotations

from tools.base import ToolContext, tool
from runtime.tools.scan import todo_scan as todo_scan_impl


@tool(
    description=(
        "Find TODO/FIXME/XXX/HACK comments. Returns path:line:text. "
        "Default 80 hits. Skips caches, .git, node_modules, venvs."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Optional file or directory to scan. Default whole workspace.",
            },
            "pattern": {
                "type": "string",
                "description": "Optional extra regex. Default is TODO|FIXME|XXX|HACK.",
            },
        },
    },
)
def todo_scan(ctx: ToolContext, path: str = "", pattern: str = "") -> str:
    return todo_scan_impl(ctx.workspace, path=path, pattern=pattern)
