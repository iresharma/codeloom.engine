from __future__ import annotations

import asyncio

from runtime.tools import lsp as run_lsp
from runtime.tools.edits import apply_workspace_edit, normalize_workspace_edit
from tools.base import ToolContext, tool

_LSP_MISSING = (
    "error: LSP is not available for this workspace "
    "(need python, go, or javascript/typescript)"
)


def _as_int(value, default: int) -> int:
    if value is None or value == "":
        return default
    return int(value)


def _require_lsp(ctx: ToolContext) -> str | None:
    if ctx.lsp is None:
        return _LSP_MISSING
    return None


@tool(
    description=(
        "Jump to the definition of the symbol at a 1-based line/character "
        "using the language server. Understands imports and types, unlike "
        "find_symbol. Get coordinates from find_symbol or read_file."
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
                "description": "1-based line number containing the symbol",
            },
            "character": {
                "type": "integer",
                "description": "1-based column of, or within, the symbol name",
            },
        },
        "required": ["path", "line", "character"],
    },
)
async def goto_definition(ctx: ToolContext, path: str, line, character) -> str:
    err = _require_lsp(ctx)
    if err:
        return err
    return await asyncio.to_thread(
        run_lsp.goto_definition,
        ctx.workspace,
        ctx.lsp,
        path,
        _as_int(line, 1),
        _as_int(character, 1),
    )


@tool(
    description=(
        "Find every usage of the symbol at a 1-based position across the "
        "indexed workspace, including the declaration. Use before changing "
        "or renaming something."
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
                "description": "1-based line number containing the symbol",
            },
            "character": {
                "type": "integer",
                "description": "1-based column of, or within, the symbol name",
            },
        },
        "required": ["path", "line", "character"],
    },
)
async def find_references(ctx: ToolContext, path: str, line, character) -> str:
    err = _require_lsp(ctx)
    if err:
        return err
    return await asyncio.to_thread(
        run_lsp.find_references,
        ctx.workspace,
        ctx.lsp,
        path,
        _as_int(line, 1),
        _as_int(character, 1),
    )


@tool(
    description=(
        "Type signature and documentation for the symbol at a 1-based "
        "position, like hovering in an editor. Use get_node_at for local "
        "syntax instead of types."
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
                "description": "1-based line number containing the symbol",
            },
            "character": {
                "type": "integer",
                "description": "1-based column of, or within, the symbol name",
            },
        },
        "required": ["path", "line", "character"],
    },
)
async def hover(ctx: ToolContext, path: str, line, character) -> str:
    err = _require_lsp(ctx)
    if err:
        return err
    return await asyncio.to_thread(
        run_lsp.hover,
        ctx.workspace,
        ctx.lsp,
        path,
        _as_int(line, 1),
        _as_int(character, 1),
    )


@tool(
    description=(
        "Compiler/type-checker diagnostics for a file (errors, warnings, "
        "hints). Same information as editor squiggles."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path relative to the workspace root",
            },
        },
        "required": ["path"],
    },
)
async def get_diagnostics(ctx: ToolContext, path: str) -> str:
    err = _require_lsp(ctx)
    if err:
        return err
    return await asyncio.to_thread(
        run_lsp.get_diagnostics, ctx.workspace, ctx.lsp, path
    )


@tool(
    description=(
        "Language-server outline of a file with SymbolKind (includes "
        "interfaces and enums tree-sitter may miss). Prefer list_symbols "
        "first; use this when that outline looks incomplete."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path relative to the workspace root",
            },
        },
        "required": ["path"],
    },
)
async def document_symbols(ctx: ToolContext, path: str) -> str:
    err = _require_lsp(ctx)
    if err:
        return err
    return await asyncio.to_thread(
        run_lsp.document_symbols, ctx.workspace, ctx.lsp, path
    )


@tool(
    description=(
        "Rename a symbol at a 1-based position via the language server. "
        "Updates references correctly, unlike search-and-replace on the name. "
        "Read the file first. Get coordinates from find_symbol or read_file."
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
                "description": "1-based line number containing the symbol",
            },
            "character": {
                "type": "integer",
                "description": "1-based column of, or within, the symbol name",
            },
            "new_name": {
                "type": "string",
                "description": "New identifier",
            },
        },
        "required": ["path", "line", "character", "new_name"],
    },
)
async def rename_symbol(ctx: ToolContext, path: str, line, character, new_name: str) -> str:
    err = _require_lsp(ctx)
    if err:
        return err
    payload = await asyncio.to_thread(
        run_lsp.rename_symbol,
        ctx.workspace,
        ctx.lsp,
        path,
        _as_int(line, 1),
        _as_int(character, 1),
        new_name,
    )
    if isinstance(payload, str):
        return payload
    try:
        grouped = normalize_workspace_edit(ctx.workspace, payload)
    except Exception as exc:  # noqa: BLE001
        return f"error: {exc}"
    if not grouped:
        return f"error: language server returned no file edits for rename of {path}"
    return await apply_workspace_edit(ctx, grouped, "rename_symbol")
