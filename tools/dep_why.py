from __future__ import annotations

from tools.base import ToolContext, tool
from runtime.tools.depwhy import dep_why as dep_why_impl


@tool(
    description=(
        "Explain why a dependency is in the tree. "
        "ecosystem: npm (npm ls), go (go mod why), pypi (pip show), crates (cargo tree)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ecosystem": {
                "type": "string",
                "description": "npm, go, pypi, or crates.",
            },
            "name": {"type": "string", "description": "Package or module name."},
        },
        "required": ["ecosystem", "name"],
    },
)
def dep_why(ctx: ToolContext, ecosystem: str, name: str) -> str:
    return dep_why_impl(ctx.workspace, ecosystem, name)
