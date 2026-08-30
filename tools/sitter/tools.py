from __future__ import annotations

import json

from tools.base import BaseTool, ToolContext, ToolResult
from tools.paths import relative_to_workspace, resolve_workspace_path
from tools.sitter.engine import infer_language, list_symbols, node_at, parse_source, query_source


def _read(ctx: ToolContext, raw: str):
    path = resolve_workspace_path(ctx.workspace, raw)
    if not path.is_file():
        raise FileNotFoundError(raw)
    return path, path.read_text(encoding="utf-8", errors="replace")


class ParseFile(BaseTool):
    name = "parse_file"
    description = "Parse a file into a syntax tree. Uses tree-sitter when installed."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "language": {"type": "string"},
        },
        "required": ["path"],
    }
    allowed_roles = {"orchestrator", "subagent"}

    async def execute(self, ctx: ToolContext, **kwargs) -> ToolResult:
        try:
            path, source = _read(ctx, str(kwargs["path"]))
        except FileNotFoundError:
            return ToolResult(ok=False, content=f"file not found: {kwargs['path']}")
        parsed = parse_source(path, source, kwargs.get("language"))
        return ToolResult(ok=True, content=json.dumps(parsed, indent=2)[:8000], data=parsed)


class QueryTree(BaseTool):
    name = "query_tree"
    description = "Run a tree-sitter query against a file and return captures."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "query": {"type": "string"},
            "language": {"type": "string"},
        },
        "required": ["path", "query"],
    }
    allowed_roles = {"orchestrator", "subagent"}

    async def execute(self, ctx: ToolContext, **kwargs) -> ToolResult:
        try:
            path, source = _read(ctx, str(kwargs["path"]))
        except FileNotFoundError:
            return ToolResult(ok=False, content=f"file not found: {kwargs['path']}")
        captures = query_source(path, source, str(kwargs["query"]), kwargs.get("language"))
        rows = [capture.__dict__ for capture in captures]
        if not rows:
            note = "no captures (tree-sitter language may be missing)"
            return ToolResult(ok=True, content=note, data={"captures": []})
        return ToolResult(ok=True, content=json.dumps(rows, indent=2)[:8000], data={"captures": rows})


class GetNodeAt(BaseTool):
    name = "get_node_at"
    description = "Return the syntax node at a 1-based line and 0-based column."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "line": {"type": "integer"},
            "col": {"type": "integer", "default": 0},
            "language": {"type": "string"},
        },
        "required": ["path", "line"],
    }
    allowed_roles = {"orchestrator", "subagent"}

    async def execute(self, ctx: ToolContext, **kwargs) -> ToolResult:
        try:
            path, source = _read(ctx, str(kwargs["path"]))
        except FileNotFoundError:
            return ToolResult(ok=False, content=f"file not found: {kwargs['path']}")
        found = node_at(source, int(kwargs["line"]), int(kwargs.get("col") or 0), path, kwargs.get("language"))
        if found is None:
            return ToolResult(ok=True, content="no tree-sitter node (fallback inactive)")
        return ToolResult(ok=True, content=json.dumps(found, indent=2), data=found)


class ListSymbols(BaseTool):
    name = "list_symbols"
    description = "List local symbols (functions, classes, types) from a file."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "language": {"type": "string"},
        },
        "required": ["path"],
    }
    allowed_roles = {"orchestrator", "subagent"}

    async def execute(self, ctx: ToolContext, **kwargs) -> ToolResult:
        try:
            path, source = _read(ctx, str(kwargs["path"]))
        except FileNotFoundError:
            return ToolResult(ok=False, content=f"file not found: {kwargs['path']}")
        symbols = list_symbols(path, source, kwargs.get("language") or infer_language(path))
        for item in symbols:
            item["path"] = relative_to_workspace(ctx.workspace, path)
        return ToolResult(
            ok=True,
            content=json.dumps(symbols, indent=2) or "no symbols",
            data={"symbols": symbols},
        )


def sitter_tools() -> list[BaseTool]:
    return [ParseFile(), QueryTree(), GetNodeAt(), ListSymbols()]
