from runtime.tools.fs import DEFAULT_READ_LIMIT, read_window
from tools.base import ToolContext, tool


def _as_int(value, default: int) -> int:
    if value is None or value == "":
        return default
    return int(value)


@tool(
    description=(
        "Read a UTF-8 text file as a numbered line window. "
        "Default is 200 lines from offset 1. Use offset from the header to page."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path relative to the workspace root",
            },
            "offset": {
                "type": "integer",
                "description": "1-based start line (default 1)",
            },
            "limit": {
                "type": "integer",
                "description": "Number of lines to return (default 200, max 400)",
            },
        },
        "required": ["path"],
    },
)
def read_file(ctx: ToolContext, path: str, offset=1, limit=DEFAULT_READ_LIMIT) -> str:
    return read_window(
        ctx.workspace,
        path,
        offset=_as_int(offset, 1),
        limit=_as_int(limit, DEFAULT_READ_LIMIT),
    )
