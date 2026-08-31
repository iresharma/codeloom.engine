from __future__ import annotations

from runtime.tools.edits import apply_edit
from runtime.tools.edits import insert_at_line as insert_at_line_impl
from runtime.tools.edits import replace_lines as replace_lines_impl
from runtime.tools.edits import str_replace as str_replace_impl
from runtime.tools.fileid import FileSource
from tools.base import ToolContext, tool


def _as_int(value, default: int) -> int:
    if value is None or value == "":
        return default
    return int(value)


@tool(
    description=(
        "Replace one unique exact substring in a file. The match must occur "
        "exactly once; zero or multiple matches fail instead of guessing. "
        "Read the file first. Include enough surrounding context to be unique."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path relative to the workspace root",
            },
            "old_string": {
                "type": "string",
                "description": "Exact text to find; must be unique in the file",
            },
            "new_string": {
                "type": "string",
                "description": "Replacement text",
            },
        },
        "required": ["path", "old_string", "new_string"],
    },
)
async def str_replace(
    ctx: ToolContext, path: str, old_string: str, new_string: str
) -> str:
    def mutate(src: FileSource) -> str:
        return str_replace_impl(src.text, old_string, new_string)

    return await apply_edit(ctx, path, mutate, "str_replace")


@tool(
    description=(
        "Replace an inclusive 1-based line range in a file. Mirrors the "
        "read_file window (start-end). Read the file first."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path relative to the workspace root",
            },
            "start": {
                "type": "integer",
                "description": "1-based first line to replace",
            },
            "end": {
                "type": "integer",
                "description": "1-based last line to replace (inclusive)",
            },
            "new_text": {
                "type": "string",
                "description": "Replacement text for that range",
            },
        },
        "required": ["path", "start", "end", "new_text"],
    },
)
async def replace_lines(
    ctx: ToolContext, path: str, start, end, new_text: str
) -> str:
    def mutate(src: FileSource) -> str:
        return replace_lines_impl(
            src.text, _as_int(start, 1), _as_int(end, 1), new_text
        )

    return await apply_edit(ctx, path, mutate, "replace_lines")


@tool(
    description=(
        "Insert text before a 1-based line (or at end+1 to append). "
        "Read the file first."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path relative to the workspace root",
            },
            "line": {
                "type": "integer",
                "description": "1-based line to insert before; len+1 appends",
            },
            "new_text": {
                "type": "string",
                "description": "Text to insert",
            },
        },
        "required": ["path", "line", "new_text"],
    },
)
async def insert_at_line(
    ctx: ToolContext, path: str, line, new_text: str
) -> str:
    def mutate(src: FileSource) -> str:
        return insert_at_line_impl(src.text, _as_int(line, 1), new_text)

    return await apply_edit(ctx, path, mutate, "insert_at_line")


@tool(
    description=(
        "Create a new text file. Fails if the path already exists. "
        "Parent directories are created as needed."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path relative to the workspace root",
            },
            "content": {
                "type": "string",
                "description": "Full file contents",
            },
        },
        "required": ["path", "content"],
    },
)
async def create_file(ctx: ToolContext, path: str, content: str) -> str:
    def mutate(src: FileSource) -> str:
        return content

    return await apply_edit(ctx, path, mutate, "create_file", creating=True)
