from tools.base import ToolContext, tool
from runtime.fs import read_text


@tool(
    description="Read a UTF-8 text file in the workspace.",
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path relative to the workspace root",
            }
        },
        "required": ["path"],
    },
)
def read_file(ctx: ToolContext, path: str) -> str:
    rel, content = read_text(ctx.workspace, path)
    return f"{rel}\n\n{content}"
