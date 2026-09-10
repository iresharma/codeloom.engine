from __future__ import annotations

from tools.base import tool
from runtime.tools.web import web_fetch as fetch_impl
from runtime.tools.web import web_search as search_impl


@tool(
    description=(
        "HTTP GET a URL and return an LLM-readable document (HTML to markdown). "
        "GitHub.com URLs are refused — use github_repo / github_tree / github_file. "
        "http/https only."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "http or https URL to fetch.",
            }
        },
        "required": ["url"],
    },
)
def web_fetch(url: str) -> str:
    return fetch_impl(url)


@tool(
    description=(
        "Web search. Requires BRAVE_API_KEY. If missing, returns an error; "
        "use web_fetch with a known URL instead."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "count": {
                "type": "integer",
                "description": "Number of results (default 5, max 10).",
            },
        },
        "required": ["query"],
    },
)
def web_search(query: str, count: int = 5) -> str:
    return search_impl(query, count=count)
