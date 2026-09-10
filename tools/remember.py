from __future__ import annotations

from runtime.store.memory import remember as store_remember
from tools.base import ToolContext, tool


@tool(
    description=(
        "Store a lasting workspace fact. section=files requires path and "
        "records a short blurb about that file (purpose, entry points, "
        "constraints) keyed to its current hash. engineering / product / "
        "cicd / other append a decision. Not for play-by-play or transcripts."
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
                "description": "A short factual blurb or decision to keep.",
            },
            "path": {
                "type": "string",
                "description": "Workspace-relative path; required when section is files.",
            },
        },
        "required": ["section", "note"],
    },
)
def remember(ctx: ToolContext, section: str, note: str, path: str = "") -> str:
    try:
        return store_remember(ctx.workspace, section, note, path=path or "")
    except OSError as exc:
        return f"error: {exc}"
