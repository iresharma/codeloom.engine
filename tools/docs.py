from __future__ import annotations

from tools.base import tool
from runtime.tools.docs import docs_lookup as docs_lookup_impl
from runtime.tools.docs import tldr as tldr_impl


@tool(
    description=(
        "Official docs lookup. source: mdn, pypi, npm, crates, go. "
        "Prefer this over web_search for API facts."
    ),
    parameters={
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "mdn, pypi, npm, crates, or go.",
            },
            "query": {
                "type": "string",
                "description": "Search query or package / module name.",
            },
        },
        "required": ["source", "query"],
    },
)
def docs_lookup(source: str, query: str) -> str:
    return docs_lookup_impl(source, query)


@tool(
    description="tldr cheat sheet for a CLI command (flags and examples).",
    parameters={
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "description": "Command name, e.g. pytest or git.",
            }
        },
        "required": ["topic"],
    },
)
def tldr(topic: str) -> str:
    return tldr_impl(topic)
