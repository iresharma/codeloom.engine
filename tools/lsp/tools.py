from __future__ import annotations

from tools.base import BaseTool, ToolContext, ToolResult
from tools.lsp.client import LspManager, dump


def _manager(ctx: ToolContext) -> LspManager:
    if ctx.session is not None and getattr(ctx.session, "lsp", None) is not None:
        return ctx.session.lsp
    return LspManager()


def _unavailable(manager: LspManager, extra: str = "") -> ToolResult:
    message = manager.reason
    if extra:
        message = f"{message}; {extra}"
    return ToolResult(ok=True, content=message, data={"available": False})


class GetDiagnostics(BaseTool):
    name = "get_diagnostics"
    description = "Get LSP diagnostics for a file."
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }
    allowed_roles = {"orchestrator", "subagent"}

    async def execute(self, ctx: ToolContext, **kwargs) -> ToolResult:
        manager = _manager(ctx)
        if not manager.available:
            return _unavailable(manager)
        rows = await manager.diagnostics(str(kwargs["path"]))
        return ToolResult(ok=True, content=dump(rows) if rows else "no diagnostics", data={"diagnostics": rows})


class GoToDefinition(BaseTool):
    name = "go_to_definition"
    description = "Go to definition at a file position (1-based line)."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "line": {"type": "integer"},
            "col": {"type": "integer", "default": 0},
        },
        "required": ["path", "line"],
    }
    allowed_roles = {"orchestrator", "subagent"}

    async def execute(self, ctx: ToolContext, **kwargs) -> ToolResult:
        manager = _manager(ctx)
        if not manager.available:
            return _unavailable(manager)
        rows = await manager.definition(str(kwargs["path"]), int(kwargs["line"]), int(kwargs.get("col") or 0))
        payload = [item.__dict__ for item in rows]
        return ToolResult(ok=True, content=dump(payload) if payload else "no definition", data={"locations": payload})


class FindReferences(BaseTool):
    name = "find_references"
    description = "Find references at a file position (1-based line)."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "line": {"type": "integer"},
            "col": {"type": "integer", "default": 0},
        },
        "required": ["path", "line"],
    }
    allowed_roles = {"orchestrator", "subagent"}

    async def execute(self, ctx: ToolContext, **kwargs) -> ToolResult:
        manager = _manager(ctx)
        if not manager.available:
            return _unavailable(manager)
        rows = await manager.references(str(kwargs["path"]), int(kwargs["line"]), int(kwargs.get("col") or 0))
        payload = [item.__dict__ for item in rows]
        return ToolResult(ok=True, content=dump(payload) if payload else "no references", data={"locations": payload})


class Hover(BaseTool):
    name = "hover"
    description = "Hover documentation at a file position (1-based line)."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "line": {"type": "integer"},
            "col": {"type": "integer", "default": 0},
        },
        "required": ["path", "line"],
    }
    allowed_roles = {"orchestrator", "subagent"}

    async def execute(self, ctx: ToolContext, **kwargs) -> ToolResult:
        manager = _manager(ctx)
        if not manager.available:
            return _unavailable(manager)
        text = await manager.hover(str(kwargs["path"]), int(kwargs["line"]), int(kwargs.get("col") or 0))
        return ToolResult(ok=True, content=text or "no hover")


class DocumentSymbols(BaseTool):
    name = "document_symbols"
    description = "List document symbols from the language server."
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }
    allowed_roles = {"orchestrator", "subagent"}

    async def execute(self, ctx: ToolContext, **kwargs) -> ToolResult:
        manager = _manager(ctx)
        if not manager.available:
            return _unavailable(manager)
        rows = await manager.document_symbols(str(kwargs["path"]))
        return ToolResult(ok=True, content=dump(rows) if rows else "no symbols", data={"symbols": rows})


def lsp_tools() -> list[BaseTool]:
    return [GetDiagnostics(), GoToDefinition(), FindReferences(), Hover(), DocumentSymbols()]
