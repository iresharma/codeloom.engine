from __future__ import annotations

from runtime.store.memory import remember as store_remember
from tools.base import ToolContext, tool


@tool(
    description=(
        "Store a lasting workspace fact. section=files requires path and "
        "records purpose / entry_points / constraints keyed to the file's "
        "current hash. A lone note is stored as purpose. engineering / "
        "product / cicd / other append a decision. Not for play-by-play. "
        "The engine also persists ask/coder/researcher briefings on finish. "
        "For section=files, omitted fields keep their previous values."
    ),
    parameters={
        "type": "object",
        "properties": {
            "section": {
                "type": "string",
                "description": "files, engineering, product, cicd, or other",
            },
            "note": {
                "type": "string",
                "description": (
                    "Decision text, or a file blurb used as purpose when "
                    "purpose is omitted."
                ),
            },
            "path": {
                "type": "string",
                "description": "Workspace-relative path; required when section is files.",
            },
            "purpose": {
                "type": "string",
                "description": "What this file is for. Used with section=files.",
            },
            "entry_points": {
                "type": "string",
                "description": "Symbols or functions to start from. section=files.",
            },
            "constraints": {
                "type": "string",
                "description": "Invariants a later edit must keep. section=files.",
            },
        },
        "required": ["section"],
    },
)
def remember(
    ctx: ToolContext,
    section: str,
    note: str = "",
    path: str = "",
    purpose: str = "",
    entry_points: str = "",
    constraints: str = "",
) -> str:
    try:
        return store_remember(
            ctx.workspace,
            section,
            note=note or "",
            path=path or "",
            purpose=purpose or "",
            entry_points=entry_points or "",
            constraints=constraints or "",
        )
    except OSError as exc:
        return f"error: {exc}"
