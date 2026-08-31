from __future__ import annotations

from runtime.tools.edits import apply_edit, apply_patch_text
from runtime.tools.fileid import FileSource
from tools.base import ToolContext, tool


@tool(
    description=(
        "Apply a unified diff to a file. Every hunk's context must match "
        "(small line-number drift is tolerated). The whole patch is refused "
        "if any hunk fails. Read the file first."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path relative to the workspace root",
            },
            "patch": {
                "type": "string",
                "description": "Unified diff (---/+++ headers optional; @@ hunks required)",
            },
        },
        "required": ["path", "patch"],
    },
)
async def apply_patch(ctx: ToolContext, path: str, patch: str) -> str:
    def mutate(src: FileSource) -> str:
        return apply_patch_text(src.text, patch)

    return await apply_edit(ctx, path, mutate, "apply_patch")
