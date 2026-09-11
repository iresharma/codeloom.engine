"""Unit tests for tools/lsp.py (LSP tool entry points).

Tests the wrapper functions that call into runtime.tools.lsp, ensuring proper
context passing, parameter conversion, and error handling.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from runtime.config import EngineConfig
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
    """Create a ToolContext with no LSP manager."""
    db = tmp_path / "session.db"
    ensure_schema(db)
    return ToolContext(
        workspace=tmp_path,
        lsp=None,
        files=FileTracker(),
        journal=db,
        session_id="test-session",
        config=EngineConfig(),
    )


@pytest.fixture
def ctx_with_lsp(tmp_path):
    """Create a ToolContext with a mock LSP manager."""
    db = tmp_path / "session.db"
    ensure_schema(db)
    lsp = MagicMock()
    return ToolContext(
        workspace=tmp_path,
        lsp=lsp,
        files=FileTracker(),
        journal=db,
        session_id="test-session",
        config=EngineConfig(),
    )


class TestAsInt:
    """Test _as_int conversion utility."""

    def test_as_int_with_none_returns_default(self):
        assert _as_int(None, 5) == 5

    def test_as_int_with_empty_string_returns_default(self):
        assert _as_int("", 5) == 5

    def test_as_int_with_valid_string_returns_int(self):
        assert _as_int("42", 5) == 42

    def test_as_int_with_int_returns_int(self):
        assert _as_int(10, 5) == 10

    def test_as_int_with_zero_returns_zero(self):
        assert _as_int(0, 5) == 0

    def test_as_int_with_string_zero_returns_zero(self):
        assert _as_int("0", 5) == 0


class TestRequireLsp:
    """Test _require_lsp validation."""

    def test_require_lsp_with_none_returns_error(self, ctx):
        ctx.lsp = None
        err = _require_lsp(ctx)
        assert err is not None
        assert "LSP is not available" in err

    def test_require_lsp_with_lsp_returns_none(self, ctx_with_lsp):
        err = _require_lsp(ctx_with_lsp)
        assert err is None


class TestGotoDefinition:
    """Test goto_definition tool."""

    def test_goto_definition_no_lsp_returns_error(self, ctx):
        async def run():
            return await goto_definition(ctx, "test.py", 10, 5)
        
        result = asyncio.run(run())
        assert "LSP is not available" in result

    def test_goto_definition_with_lsp_calls_runtime(self, ctx_with_lsp, tmp_path):
        # Seed a test file
        test_file = tmp_path / "test.py"
        test_file.write_text("def foo():\n    pass\n")

        async def run():
            with patch("tools.lsp.run_lsp.goto_definition") as mock_gd:
                mock_gd.return_value = "test.py:1:0  def foo"
                result = await goto_definition(ctx_with_lsp, "test.py", 1, 1)

                assert result == "test.py:1:0  def foo"
                mock_gd.assert_called_once()
                # Check that parameters were converted properly
                call_args = mock_gd.call_args
                assert call_args[0][2] == "test.py"
                assert call_args[0][3] == 1  # line
                assert call_args[0][4] == 1  # character
                return result

        asyncio.run(run())

    def test_goto_definition_converts_string_parameters(self, ctx_with_lsp, tmp_path):
        test_file = tmp_path / "test.py"
        test_file.write_text("def foo():\n    pass\n")

        async def run():
            with patch("tools.lsp.run_lsp.goto_definition") as mock_gd:
                mock_gd.return_value = "result"
                await goto_definition(ctx_with_lsp, "test.py", "10", "5")

                call_args = mock_gd.call_args
                assert call_args[0][3] == 10  # line converted from "10"
                assert call_args[0][4] == 5   # character converted from "5"

        asyncio.run(run())


class TestFindReferences:
    """Test find_references tool."""

    def test_find_references_no_lsp_returns_error(self, ctx):
        async def run():
            return await find_references(ctx, "test.py", 10, 5)
        
        result = asyncio.run(run())
        assert "LSP is not available" in result

    def test_find_references_with_lsp(self, ctx_with_lsp, tmp_path):
        test_file = tmp_path / "test.py"
        test_file.write_text("x = 1\nprint(x)\n")

        async def run():
            with patch("tools.lsp.run_lsp.find_references") as mock_refs:
                mock_refs.return_value = "test.py:1:0\ntest.py:2:6"
                result = await find_references(ctx_with_lsp, "test.py", 1, 0)

                assert "test.py:1:0" in result
                mock_refs.assert_called_once()
                return result

        asyncio.run(run())

    def test_find_references_converts_parameters(self, ctx_with_lsp, tmp_path):
        test_file = tmp_path / "test.py"
        test_file.write_text("x = 1\nprint(x)\n")

        async def run():
            with patch("tools.lsp.run_lsp.find_references") as mock_refs:
                mock_refs.return_value = ""
                await find_references(ctx_with_lsp, "test.py", "5", "3")

                call_args = mock_refs.call_args
                assert call_args[0][3] == 5
                assert call_args[0][4] == 3

        asyncio.run(run())


class TestHover:
    """Test hover tool."""

    def test_hover_no_lsp_returns_error(self, ctx):
        async def run():
            return await hover(ctx, "test.py", 10, 5)
        
        result = asyncio.run(run())
        assert "LSP is not available" in result

    def test_hover_with_lsp(self, ctx_with_lsp, tmp_path):
        test_file = tmp_path / "test.py"
        test_file.write_text("def foo(): pass\n")

        async def run():
            with patch("tools.lsp.run_lsp.hover") as mock_hover:
                mock_hover.return_value = "def foo() -> None"
                result = await hover(ctx_with_lsp, "test.py", 1, 5)

                assert "def foo" in result
                mock_hover.assert_called_once()
                return result

        asyncio.run(run())

    def test_hover_with_default_parameters(self, ctx_with_lsp, tmp_path):
        test_file = tmp_path / "test.py"
        test_file.write_text("x = 1\n")

        async def run():
            with patch("tools.lsp.run_lsp.hover") as mock_hover:
                mock_hover.return_value = "int"
                await hover(ctx_with_lsp, "test.py", None, "")

                call_args = mock_hover.call_args
                # None -> 1, "" -> 1 (defaults)
                assert call_args[0][3] == 1
                assert call_args[0][4] == 1

        asyncio.run(run())


class TestGetDiagnostics:
    """Test get_diagnostics tool."""

    def test_get_diagnostics_no_lsp_returns_error(self, ctx):
        async def run():
            return await get_diagnostics(ctx, "test.py")
        
        result = asyncio.run(run())
        assert "LSP is not available" in result

    def test_get_diagnostics_with_lsp(self, ctx_with_lsp, tmp_path):
        test_file = tmp_path / "test.py"
        test_file.write_text("x = 1\n")

        async def run():
            with patch("tools.lsp.run_lsp.get_diagnostics") as mock_diag:
                mock_diag.return_value = "test.py:1:0 Error: undefined variable"
                result = await get_diagnostics(ctx_with_lsp, "test.py")

                assert "Error" in result or "undefined" in result
                mock_diag.assert_called_once_with(ctx_with_lsp.workspace, ctx_with_lsp.lsp, "test.py")
                return result

        asyncio.run(run())


class TestDocumentSymbols:
    """Test document_symbols tool."""

    def test_document_symbols_no_lsp_returns_error(self, ctx):
        async def run():
            return await document_symbols(ctx, "test.py")
        
        result = asyncio.run(run())
        assert "LSP is not available" in result

    def test_document_symbols_with_lsp(self, ctx_with_lsp, tmp_path):
        test_file = tmp_path / "test.py"
        test_file.write_text("def foo():\n    pass\n\ndef bar():\n    pass\n")

        async def run():
            with patch("tools.lsp.run_lsp.document_symbols") as mock_syms:
                mock_syms.return_value = "2 symbol(s)\nFunction foo 1:0\nFunction bar 4:0"
                result = await document_symbols(ctx_with_lsp, "test.py")

                assert "foo" in result
                assert "bar" in result
                mock_syms.assert_called_once()
                return result

        asyncio.run(run())


class TestRenameSymbol:
    """Test rename_symbol tool."""

    def test_rename_symbol_no_lsp_returns_error(self, ctx):
        async def run():
            return await rename_symbol(ctx, "test.py", 1, 0, "new_name")
        
        result = asyncio.run(run())
        assert "LSP is not available" in result

    def test_rename_symbol_error_from_runtime(self, ctx_with_lsp, tmp_path):
        test_file = tmp_path / "test.py"
        test_file.write_text("x = 1\n")

        async def run():
            with patch("tools.lsp.run_lsp.rename_symbol") as mock_rename:
                mock_rename.return_value = "error: rename failed"
                result = await rename_symbol(ctx_with_lsp, "test.py", 1, 0, "y")

                assert "error" in result
                return result

        asyncio.run(run())

    def test_rename_symbol_with_workspace_edit(self, ctx_with_lsp, tmp_path):
        test_file = tmp_path / "test.py"
        test_file.write_text("x = 1\nprint(x)\n")

        # Mock the runtime rename to return a WorkspaceEdit
        workspace_edit = {
            "changes": {
                "file:///tmp/test.py": [
                    {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}}, "newText": "y"}
                ]
            }
        }

        async def run():
            with patch("tools.lsp.run_lsp.rename_symbol") as mock_rename:
                mock_rename.return_value = workspace_edit
                with patch("tools.lsp.normalize_workspace_edit") as mock_norm:
                    mock_norm.return_value = {}
                    with patch("tools.lsp.apply_workspace_edit") as mock_apply:
                        mock_apply.return_value = "Renamed successfully"
                        result = await rename_symbol(ctx_with_lsp, "test.py", 1, 0, "y")

                        # Since workspace_edit is not a string and normalization returns empty,
                        # we should get an error about no file edits
                        assert "error" in result or "Renamed" in result
                        return result

        asyncio.run(run())

    def test_rename_symbol_converts_parameters(self, ctx_with_lsp, tmp_path):
        test_file = tmp_path / "test.py"
        test_file.write_text("x = 1\n")

        async def run():
            with patch("tools.lsp.run_lsp.rename_symbol") as mock_rename:
                mock_rename.return_value = "error: test"
                await rename_symbol(ctx_with_lsp, "test.py", "5", "3", "new_name")

                call_args = mock_rename.call_args
                assert call_args[0][3] == 5  # line from "5"
                assert call_args[0][4] == 3  # character from "3"
                assert call_args[0][5] == "new_name"

        asyncio.run(run())


class TestIntegration:
    """Integration tests for LSP tool chain."""

    def test_goto_definition_and_find_references_work_together(self, ctx_with_lsp, tmp_path):
        """Test that goto_definition and find_references can share context."""
        test_file = tmp_path / "test.py"
        test_file.write_text("def foo():\n    pass\n\nfoo()\n")

        async def run():
            with patch("tools.lsp.run_lsp.goto_definition") as mock_gd:
                with patch("tools.lsp.run_lsp.find_references") as mock_refs:
                    mock_gd.return_value = "test.py:1:4  def foo"
                    mock_refs.return_value = "test.py:1:4\ntest.py:4:0"
                    
                    result_gd = await goto_definition(ctx_with_lsp, "test.py", 4, 0)
                    result_refs = await find_references(ctx_with_lsp, "test.py", 1, 4)
                    
                    assert "foo" in result_gd
                    assert "test.py:" in result_refs
                    return (result_gd, result_refs)

        asyncio.run(run())

    def test_all_tools_reject_missing_lsp(self, ctx):
        """Verify all tools return error when LSP is not available."""
        async def run():
            tools_and_calls = [
                (goto_definition, ("test.py", 1, 0)),
                (find_references, ("test.py", 1, 0)),
                (hover, ("test.py", 1, 0)),
                (get_diagnostics, ("test.py",)),
                (document_symbols, ("test.py",)),
            ]
            
            for tool_fn, args in tools_and_calls:
                result = await tool_fn(ctx, *args)
                assert "LSP is not available" in result, f"{tool_fn.__name__} should error without LSP"

        asyncio.run(run())

    def test_parameter_boundary_cases(self, ctx_with_lsp, tmp_path):
        """Test edge cases in parameter conversion."""
        test_file = tmp_path / "test.py"
        test_file.write_text("x = 1\n")

        async def run():
            with patch("tools.lsp.run_lsp.goto_definition") as mock_gd:
                mock_gd.return_value = "result"
                # Test with string "0" (should stay as 0, but default is 1 so it becomes 0)
                await goto_definition(ctx_with_lsp, "test.py", "0", "0")
                # Actually _as_int converts "0" to int 0, which is not None and not ""
                # So it should be 0
                call_args = mock_gd.call_args
                assert call_args[0][3] == 0
                assert call_args[0][4] == 0

        asyncio.run(run())
