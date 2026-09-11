"""Unit tests for tools/lsp.py with mocked LSP client.

Tests cover the tool-layer wrapper functions that delegate to runtime.tools.lsp.
These tests mock the underlying LSP interactions to provide coverage without
requiring a live language server or the @pytest.mark.lsp marker.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
try:
    import pytest_asyncio
    has_asyncio = True
except ImportError:
    has_asyncio = False

from runtime.store.edits import ensure_schema
from runtime.tools.fileid import read_source
from runtime.tools.tracker import FileTracker
from tools.base import ToolContext
from tools.lsp import (
    _as_int,
    _require_lsp,
    find_references,
    get_diagnostics,
    goto_definition,
    hover,
    document_symbols,
    rename_symbol,
)


@pytest.fixture
def ctx(tmp_path):
    """Tool context with no LSP manager."""
    db = tmp_path / "session.db"
    ensure_schema(db)
    return ToolContext(
        workspace=tmp_path,
        lsp=None,
        files=FileTracker(),
        journal=db,
        session_id="test-lsp",
    )


@pytest.fixture
def ctx_with_lsp(tmp_path):
    """Tool context with a mocked LSP manager."""
    db = tmp_path / "session.db"
    ensure_schema(db)
    mock_lsp = MagicMock()
    return ToolContext(
        workspace=tmp_path,
        lsp=mock_lsp,
        files=FileTracker(),
        journal=db,
        session_id="test-lsp",
    )


class TestAsInt:
    """Tests for _as_int helper function."""

    def test_as_int_with_int(self):
        assert _as_int(42, 1) == 42

    def test_as_int_with_string_number(self):
        assert _as_int("42", 1) == 42

    def test_as_int_with_none_returns_default(self):
        assert _as_int(None, 99) == 99

    def test_as_int_with_empty_string_returns_default(self):
        assert _as_int("", 50) == 50

    def test_as_int_with_zero(self):
        assert _as_int(0, 1) == 0

    def test_as_int_with_string_zero(self):
        assert _as_int("0", 1) == 0


class TestRequireLsp:
    """Tests for _require_lsp helper function."""

    def test_require_lsp_with_no_lsp(self, ctx):
        err = _require_lsp(ctx)
        assert err is not None
        assert "LSP is not available" in err

    def test_require_lsp_with_lsp(self, ctx_with_lsp):
        err = _require_lsp(ctx_with_lsp)
        assert err is None


class TestGotoDefinition:
    """Tests for goto_definition tool."""

    def test_goto_definition_without_lsp(self, ctx):
        async def run():
            result = await goto_definition(ctx, "test.py", 1, 1)
            assert "LSP is not available" in result
        asyncio.run(run())

    def test_goto_definition_converts_int_params(self, ctx_with_lsp):
        async def run():
            with patch("tools.lsp.run_lsp.goto_definition") as mock_fn:
                mock_fn.return_value = "result"
                await goto_definition(ctx_with_lsp, "test.py", "5", "10")
                mock_fn.assert_called_once_with(
                    ctx_with_lsp.workspace,
                    ctx_with_lsp.lsp,
                    "test.py",
                    5,
                    10,
                )
        asyncio.run(run())

    def test_goto_definition_with_none_params(self, ctx_with_lsp):
        async def run():
            with patch("tools.lsp.run_lsp.goto_definition") as mock_fn:
                mock_fn.return_value = "result"
                await goto_definition(ctx_with_lsp, "test.py", None, None)
                mock_fn.assert_called_once_with(
                    ctx_with_lsp.workspace,
                    ctx_with_lsp.lsp,
                    "test.py",
                    1,
                    1,
                )
        asyncio.run(run())

    def test_goto_definition_returns_result(self, ctx_with_lsp):
        async def run():
            expected = "file.py:10:5  def my_func():"
            with patch("tools.lsp.run_lsp.goto_definition") as mock_fn:
                mock_fn.return_value = expected
                result = await goto_definition(ctx_with_lsp, "test.py", 1, 1)
                assert result == expected
        asyncio.run(run())

    def test_goto_definition_handles_default_line_char(self, ctx_with_lsp):
        async def run():
            with patch("tools.lsp.run_lsp.goto_definition") as mock_fn:
                mock_fn.return_value = "result"
                await goto_definition(ctx_with_lsp, "test.py", "", "")
                mock_fn.assert_called_once_with(
                    ctx_with_lsp.workspace,
                    ctx_with_lsp.lsp,
                    "test.py",
                    1,
                    1,
                )
        asyncio.run(run())


class TestFindReferences:
    """Tests for find_references tool."""

    def test_find_references_without_lsp(self, ctx):
        async def run():
            result = await find_references(ctx, "test.py", 1, 1)
            assert "LSP is not available" in result
        asyncio.run(run())

    def test_find_references_converts_int_params(self, ctx_with_lsp):
        async def run():
            with patch("tools.lsp.run_lsp.find_references") as mock_fn:
                mock_fn.return_value = "result"
                await find_references(ctx_with_lsp, "test.py", "15", "20")
                mock_fn.assert_called_once_with(
                    ctx_with_lsp.workspace,
                    ctx_with_lsp.lsp,
                    "test.py",
                    15,
                    20,
                )
        asyncio.run(run())

    def test_find_references_with_none_params(self, ctx_with_lsp):
        async def run():
            with patch("tools.lsp.run_lsp.find_references") as mock_fn:
                mock_fn.return_value = "result"
                await find_references(ctx_with_lsp, "test.py", None, None)
                mock_fn.assert_called_once_with(
                    ctx_with_lsp.workspace,
                    ctx_with_lsp.lsp,
                    "test.py",
                    1,
                    1,
                )
        asyncio.run(run())

    def test_find_references_returns_result(self, ctx_with_lsp):
        async def run():
            expected = "a.py:5:2\nb.py:10:8"
            with patch("tools.lsp.run_lsp.find_references") as mock_fn:
                mock_fn.return_value = expected
                result = await find_references(ctx_with_lsp, "test.py", 1, 1)
                assert result == expected
        asyncio.run(run())


class TestHover:
    """Tests for hover tool."""

    def test_hover_without_lsp(self, ctx):
        async def run():
            result = await hover(ctx, "test.py", 1, 1)
            assert "LSP is not available" in result
        asyncio.run(run())

    def test_hover_converts_int_params(self, ctx_with_lsp):
        async def run():
            with patch("tools.lsp.run_lsp.hover") as mock_fn:
                mock_fn.return_value = "hover text"
                await hover(ctx_with_lsp, "test.py", "8", "12")
                mock_fn.assert_called_once_with(
                    ctx_with_lsp.workspace,
                    ctx_with_lsp.lsp,
                    "test.py",
                    8,
                    12,
                )
        asyncio.run(run())

    def test_hover_with_empty_string_params(self, ctx_with_lsp):
        async def run():
            with patch("tools.lsp.run_lsp.hover") as mock_fn:
                mock_fn.return_value = "hover info"
                await hover(ctx_with_lsp, "test.py", "", "")
                mock_fn.assert_called_once_with(
                    ctx_with_lsp.workspace,
                    ctx_with_lsp.lsp,
                    "test.py",
                    1,
                    1,
                )
        asyncio.run(run())

    def test_hover_returns_result(self, ctx_with_lsp):
        async def run():
            expected = "def my_func() -> str"
            with patch("tools.lsp.run_lsp.hover") as mock_fn:
                mock_fn.return_value = expected
                result = await hover(ctx_with_lsp, "test.py", 1, 1)
                assert result == expected
        asyncio.run(run())


class TestGetDiagnostics:
    """Tests for get_diagnostics tool."""

    def test_get_diagnostics_without_lsp(self, ctx):
        async def run():
            result = await get_diagnostics(ctx, "test.py")
            assert "LSP is not available" in result
        asyncio.run(run())

    def test_get_diagnostics_calls_runtime_function(self, ctx_with_lsp):
        async def run():
            with patch("tools.lsp.run_lsp.get_diagnostics") as mock_fn:
                mock_fn.return_value = "No diagnostics"
                await get_diagnostics(ctx_with_lsp, "test.py")
                mock_fn.assert_called_once_with(
                    ctx_with_lsp.workspace,
                    ctx_with_lsp.lsp,
                    "test.py",
                )
        asyncio.run(run())

    def test_get_diagnostics_returns_result(self, ctx_with_lsp):
        async def run():
            expected = "test.py:5:2 Error: undefined variable"
            with patch("tools.lsp.run_lsp.get_diagnostics") as mock_fn:
                mock_fn.return_value = expected
                result = await get_diagnostics(ctx_with_lsp, "test.py")
                assert result == expected
        asyncio.run(run())


class TestDocumentSymbols:
    """Tests for document_symbols tool."""

    def test_document_symbols_without_lsp(self, ctx):
        async def run():
            result = await document_symbols(ctx, "test.py")
            assert "LSP is not available" in result
        asyncio.run(run())

    def test_document_symbols_calls_runtime_function(self, ctx_with_lsp):
        async def run():
            with patch("tools.lsp.run_lsp.document_symbols") as mock_fn:
                mock_fn.return_value = "5 symbol(s)"
                await document_symbols(ctx_with_lsp, "test.py")
                mock_fn.assert_called_once_with(
                    ctx_with_lsp.workspace,
                    ctx_with_lsp.lsp,
                    "test.py",
                )
        asyncio.run(run())

    def test_document_symbols_returns_result(self, ctx_with_lsp):
        async def run():
            expected = "Function foo  10:2"
            with patch("tools.lsp.run_lsp.document_symbols") as mock_fn:
                mock_fn.return_value = expected
                result = await document_symbols(ctx_with_lsp, "test.py")
                assert result == expected
        asyncio.run(run())


class TestRenameSymbol:
    """Tests for rename_symbol tool."""

    def test_rename_symbol_without_lsp(self, ctx):
        async def run():
            result = await rename_symbol(ctx, "test.py", 1, 1, "new_name")
            assert "LSP is not available" in result
        asyncio.run(run())

    def test_rename_symbol_when_runtime_returns_string(self, ctx_with_lsp):
        async def run():
            with patch("tools.lsp.run_lsp.rename_symbol") as mock_fn:
                mock_fn.return_value = "error: something went wrong"
                result = await rename_symbol(ctx_with_lsp, "test.py", 1, 1, "new_name")
                assert result == "error: something went wrong"
        asyncio.run(run())

    def test_rename_symbol_converts_int_params(self, ctx_with_lsp):
        async def run():
            with patch("tools.lsp.run_lsp.rename_symbol") as mock_fn:
                mock_fn.return_value = "error: test"
                await rename_symbol(ctx_with_lsp, "test.py", "5", "10", "new_name")
                mock_fn.assert_called_once_with(
                    ctx_with_lsp.workspace,
                    ctx_with_lsp.lsp,
                    "test.py",
                    5,
                    10,
                    "new_name",
                )
        asyncio.run(run())

    def test_rename_symbol_handles_workspace_edit(self, ctx_with_lsp):
        async def run():
            # Mock the runtime function to return a workspace edit payload
            workspace_edit = {
                "changes": {
                    "file:///test/file.py": [
                        {
                            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 3}},
                            "newText": "new_name",
                        }
                    ]
                }
            }
            with patch("tools.lsp.run_lsp.rename_symbol") as mock_fn:
                mock_fn.return_value = workspace_edit
                with patch("tools.lsp.normalize_workspace_edit") as mock_normalize:
                    mock_normalize.return_value = {"test.py": ["edit"]}
                    with patch("tools.lsp.apply_workspace_edit") as mock_apply:
                        mock_apply.return_value = "applied"
                        result = await rename_symbol(ctx_with_lsp, "test.py", 1, 1, "new_name")
                        assert result == "applied"
        asyncio.run(run())

    def test_rename_symbol_handles_normalize_exception(self, ctx_with_lsp):
        async def run():
            workspace_edit = {"changes": {}}
            with patch("tools.lsp.run_lsp.rename_symbol") as mock_fn:
                mock_fn.return_value = workspace_edit
                with patch("tools.lsp.normalize_workspace_edit") as mock_normalize:
                    mock_normalize.side_effect = ValueError("invalid edit")
                    result = await rename_symbol(ctx_with_lsp, "test.py", 1, 1, "new_name")
                    assert "error: invalid edit" in result
        asyncio.run(run())

    def test_rename_symbol_handles_empty_grouped_edits(self, ctx_with_lsp):
        async def run():
            workspace_edit = {"changes": {}}
            with patch("tools.lsp.run_lsp.rename_symbol") as mock_fn:
                mock_fn.return_value = workspace_edit
                with patch("tools.lsp.normalize_workspace_edit") as mock_normalize:
                    mock_normalize.return_value = {}
                    result = await rename_symbol(ctx_with_lsp, "test.py", 1, 1, "new_name")
                    assert "error: language server returned no file edits" in result
        asyncio.run(run())

    def test_rename_symbol_with_empty_string_params(self, ctx_with_lsp):
        async def run():
            with patch("tools.lsp.run_lsp.rename_symbol") as mock_fn:
                mock_fn.return_value = "error: test"
                await rename_symbol(ctx_with_lsp, "test.py", "", "", "new_name")
                mock_fn.assert_called_once_with(
                    ctx_with_lsp.workspace,
                    ctx_with_lsp.lsp,
                    "test.py",
                    1,
                    1,
                    "new_name",
                )
        asyncio.run(run())
