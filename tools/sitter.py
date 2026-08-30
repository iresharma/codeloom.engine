from __future__ import annotations

from runtime.tools import sitter as run_sitter
from tools.base import ToolContext, tool


def _as_int(value, default: int) -> int:
    if value is None or value == "":
        return default
    return int(value)


@tool(
    description=(
        "Outline definitions and imports in a file using tree-sitter "
        "(functions, classes, methods, types). Instant, no language server. "
        "Use this to see what a file contains before find_symbol or read_file."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path relative to the workspace root",
            },
            "language": {
                "type": "string",
                "description": "Optional grammar override: python, go, javascript, typescript",
            },
        },
        "required": ["path"],
    },
)
def list_symbols(ctx: ToolContext, path: str, language: str = "") -> str:
    return run_sitter.list_symbols(ctx.workspace, path, language=language or "")


@tool(
    description=(
        "Find a named function, class, or method in one file with tree-sitter "
        "and return its source plus 1-based line/character of the name. "
        "Pass that position to goto_definition, find_references, or hover."
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
                "description": "Name of the function, class, or method",
            },
            "language": {
                "type": "string",
                "description": "Optional grammar override: python, go, javascript, typescript",
            },
        },
        "required": ["path", "symbol"],
    },
)
def find_symbol(ctx: ToolContext, path: str, symbol: str, language: str = "") -> str:
    return run_sitter.find_symbol(ctx.workspace, path, symbol, language=language or "")


@tool(
    description=(
        "Return the tree-sitter node at a 1-based line/character: type, "
        "name, parent, enclosing definition, named children. Local syntax, "
        "not types or docs — use hover for those."
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
                "description": "1-based line number",
            },
            "character": {
                "type": "integer",
                "description": "1-based column of, or within, the token",
            },
            "language": {
                "type": "string",
                "description": "Optional grammar override: python, go, javascript, typescript",
            },
        },
        "required": ["path", "line", "character"],
    },
)
def get_node_at(
    ctx: ToolContext,
    path: str,
    line,
    character,
    language: str = "",
) -> str:
    return run_sitter.get_node_at(
        ctx.workspace,
        path,
        _as_int(line, 1),
        _as_int(character, 1),
        language=language or "",
    )


@tool(
    description=(
        "Run a tree-sitter query on a file. Prefer preset "
        "(imports, functions, classes, methods, calls) over a raw query. "
        "Returns capped captures as path:line:col type text."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path relative to the workspace root",
            },
            "preset": {
                "type": "string",
                "enum": list(run_sitter.PRESETS),
                "description": "Named query: imports, functions, classes, methods, calls",
            },
            "query": {
                "type": "string",
                "description": "Optional raw tree-sitter S-expression query",
            },
            "language": {
                "type": "string",
                "description": "Optional grammar override: python, go, javascript, typescript",
            },
        },
        "required": ["path"],
    },
)
def query_tree(
    ctx: ToolContext,
    path: str,
    preset: str = "",
    query: str = "",
    language: str = "",
) -> str:
    return run_sitter.query_tree(
        ctx.workspace,
        path,
        preset=preset or "",
        query=query or "",
        language=language or "",
    )


@tool(
    description=(
        "Compact named-node syntax tree for a file (line ranges, nested). "
        "Use when list_symbols is too flat and you need nesting/shape, not "
        "a full CST dump."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path relative to the workspace root",
            },
            "language": {
                "type": "string",
                "description": "Optional grammar override: python, go, javascript, typescript",
            },
        },
        "required": ["path"],
    },
)
def parse_file(ctx: ToolContext, path: str, language: str = "") -> str:
    return run_sitter.parse_file(ctx.workspace, path, language=language or "")
