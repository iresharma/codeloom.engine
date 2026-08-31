from __future__ import annotations

from runtime.tools.edits import apply_edit
from runtime.tools.fileid import FileSource
from runtime.tools.sitter import insert_after_imports_in_text, replace_symbol_in_text
from tools.base import ToolContext, tool


@tool(
    description=(
        "Replace an entire function/class/type definition identified by name "
        "using tree-sitter. More robust than string match when whitespace "
        "differs. Read the file first. new_body must be the full node text."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path relative to the workspace root",
            },
            "symbol": {
                "type": "string",
                "description": "Definition name to replace",
            },
            "new_body": {
                "type": "string",
                "description": "Full replacement text for that definition",
            },
            "language": {
                "type": "string",
                "description": "Optional grammar override: python, go, javascript, typescript",
            },
        },
        "required": ["path", "symbol", "new_body"],
    },
)
async def replace_symbol(
    ctx: ToolContext, path: str, symbol: str, new_body: str, language: str = ""
) -> str:
    def mutate(src: FileSource) -> str:
        return replace_symbol_in_text(src.rel, src.text, symbol, new_body, language)

    return await apply_edit(ctx, path, mutate, "replace_symbol")


@tool(
    description=(
        "Insert text after the last import block in a file (or at the top "
        "if there are no imports). Read the file first."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path relative to the workspace root",
            },
            "snippet": {
                "type": "string",
                "description": "Text to insert after imports",
            },
            "language": {
                "type": "string",
                "description": "Optional grammar override",
            },
        },
        "required": ["path", "snippet"],
    },
)
async def insert_after_imports(
    ctx: ToolContext, path: str, snippet: str, language: str = ""
) -> str:
    def mutate(src: FileSource) -> str:
        return insert_after_imports_in_text(src.rel, src.text, snippet, language)

    return await apply_edit(ctx, path, mutate, "insert_after_imports")
