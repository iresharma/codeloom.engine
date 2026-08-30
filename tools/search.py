from runtime.tools.search import DEFAULT_MAX_MATCHES
from runtime.tools.search import search as run_search
from tools.base import ToolContext, tool


def _as_int(value, default: int) -> int:
    if value is None or value == "":
        return default
    return int(value)


@tool(
    description=(
        "Search the workspace with ripgrep. "
        "Returns path:line:text. Prefer this over listing files and guessing."
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "ripgrep pattern (regex)",
            },
            "path": {
                "type": "string",
                "description": "Optional file or directory to limit the search",
            },
            "glob": {
                "type": "string",
                "description": "Optional glob, e.g. *.py",
            },
            "max_matches": {
                "type": "integer",
                "description": "Max matches to return (default 80, max 200)",
            },
        },
        "required": ["pattern"],
    },
)
def search(
    ctx: ToolContext,
    pattern: str,
    path: str = "",
    glob: str = "",
    max_matches=DEFAULT_MAX_MATCHES,
) -> str:
    return run_search(
        ctx.workspace,
        pattern,
        path=path or "",
        glob=glob or "",
        max_matches=_as_int(max_matches, DEFAULT_MAX_MATCHES),
    )
