from __future__ import annotations

from runtime.tools.edits import list_edits_text, undo_last
from tools.base import ToolContext, tool


def _as_int(value, default: int) -> int:
    if value is None or value == "":
        return default
    return int(value)


@tool(
    description=(
        "Undo the last agent-initiated edit batch (including multi-file "
        "renames). Refuses if the file changed on disk since the edit."
    ),
)
async def undo_edit(ctx: ToolContext) -> str:
    return await undo_last(ctx)


@tool(
    description="List recent edits in this session with their diffs.",
    parameters={
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Max edits to show (default 20)",
            },
        },
    },
)
def list_edits(ctx: ToolContext, limit=20) -> str:
    return list_edits_text(ctx, limit=_as_int(limit, 20))
