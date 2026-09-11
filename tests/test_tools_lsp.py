"""Tests for tools/lsp.py - the async tool wrapper layer.

These tests mock out the runtime LSP implementation to focus on the wrapper's
parameter handling, async-to-thread marshaling, and error cases.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.base import ToolContext
from tools.lsp import (
    _as_int,
    _require_lsp,
    document_symbols,
    find_references,
    get_diagnostics,
    goto_definition,
    hover,
    rename_symbol,
)


class TestAsInt:
    """Tests for _as_int parameter normalization helper."""

    def test_as_int_with_none(self):
        assert _as_int(None, 10) == 10

    def test_as_int_with_empty_string(self):
        assert _as_int("", 5) == 5

    def test_as_int_with_valid_string(self):
        assert _as_int("42", 99) == 42

    def test_as_int_with_integer(self):
        assert _as_int(1, 99) == 1

    def test_as_int_with_zero(self):
        assert _as_int(0, 99) == 0

    def test_as_int_with_negative(self):
        assert _as_int("-5", 99) == -5


class TestRequireLsp:
    """Tests for _require_lsp LSP availability check."""

    def test_require_lsp_when_available(self, tmp_path):
        ctx = ToolContext(
            workspace=tmp_path,
            files=None,
            journal=None,
            session_id="test",
            config=None,
        )
        ctx.lsp = MagicMock()
        assert _require_lsp(ctx) is None

    def test_require_lsp_when_missing(self, tmp_path):
        ctx = ToolContext(
            workspace=tmp_path,
            files=None,
            journal=None,
            session_id="test",
            config=None,
        )
        ctx.lsp = None
        result = _require_lsp(ctx)
        assert result is not None
        assert "LSP is not available" in result


class TestGotoDefinition:
    """Tests for goto_definition tool wrapper."""

    def test_goto_definition_without_lsp(self, tmp_path):
        ctx = ToolContext(
            workspace=tmp_path,
            files=None,
            journal=None,
            session_id="test",
            config=None,
        )
        ctx.lsp = None

        async def run():
            return await goto_definition(ctx, "a.py", 1, 1)

        result = asyncio.run(run())
        assert result.startswith("error:")
        assert "LSP is not available" in result

    def test_goto_definition_with_lsp(self, tmp_path):
        ctx = ToolContext(
            workspace=tmp_path,
            files=None,
            journal=None,
            session_id="test",
            config=None,
        )
        ctx.lsp = MagicMock()

        async def run():
            with patch("tools.lsp.run_lsp.goto_definition") as mock_run:
                mock_run.return_value = "result from goto_definition"
                return await goto_definition(ctx, "a.py", 1, 1)

        result = asyncio.run(run())
        assert result == "result from goto_definition"

    def test_goto_definition_normalizes_line_character(self, tmp_path):
        ctx = ToolContext(
            workspace=tmp_path,
            files=None,
            journal=None,
            session_id="test",
            config=None,
        )
        ctx.lsp = MagicMock()

        async def run():
            with patch("tools.lsp.run_lsp.goto_definition") as mock_run:
                mock_run.return_value = "ok"
                await goto_definition(ctx, "a.py", None, "")
                args = mock_run.call_args[0]
                # line and character should default to 1 (converted to 0-based)
                assert args[3] == 1
                assert args[4] == 1

        asyncio.run(run())

    def test_goto_definition_with_string_coords(self, tmp_path):
        ctx = ToolContext(
            workspace=tmp_path,
            files=None,
            journal=None,
            session_id="test",
            config=None,
        )
        ctx.lsp = MagicMock()

        async def run():
            with patch("tools.lsp.run_lsp.goto_definition") as mock_run:
                mock_run.return_value = "ok"
                await goto_definition(ctx, "a.py", "10", "20")
                args = mock_run.call_args[0]
                assert args[3] == 10
                assert args[4] == 20

        asyncio.run(run())


class TestFindReferences:
    """Tests for find_references tool wrapper."""

    def test_find_references_without_lsp(self, tmp_path):
        ctx = ToolContext(
            workspace=tmp_path,
            files=None,
            journal=None,
            session_id="test",
            config=None,
        )
        ctx.lsp = None

        async def run():
            return await find_references(ctx, "a.py", 1, 1)

        result = asyncio.run(run())
        assert result.startswith("error:")
        assert "LSP is not available" in result

    def test_find_references_with_lsp(self, tmp_path):
        ctx = ToolContext(
            workspace=tmp_path,
            files=None,
            journal=None,
            session_id="test",
            config=None,
        )
        ctx.lsp = MagicMock()

        async def run():
            with patch("tools.lsp.run_lsp.find_references") as mock_run:
                mock_run.return_value = "found 3 references"
                return await find_references(ctx, "a.py", 5, 10)

        result = asyncio.run(run())
        assert result == "found 3 references"

    def test_find_references_defaults(self, tmp_path):
        ctx = ToolContext(
            workspace=tmp_path,
            files=None,
            journal=None,
            session_id="test",
            config=None,
        )
        ctx.lsp = MagicMock()

        async def run():
            with patch("tools.lsp.run_lsp.find_references") as mock_run:
                mock_run.return_value = "ok"
                await find_references(ctx, "test.py", None, "")
                args = mock_run.call_args[0]
                assert args[2] == "test.py"
                assert args[3] == 1  # default line
                assert args[4] == 1  # default character

        asyncio.run(run())


class TestHover:
    """Tests for hover tool wrapper."""

    def test_hover_without_lsp(self, tmp_path):
        ctx = ToolContext(
            workspace=tmp_path,
            files=None,
            journal=None,
            session_id="test",
            config=None,
        )
        ctx.lsp = None

        async def run():
            return await hover(ctx, "a.py", 1, 1)

        result = asyncio.run(run())
        assert result.startswith("error:")
        assert "LSP is not available" in result

    def test_hover_with_lsp(self, tmp_path):
        ctx = ToolContext(
            workspace=tmp_path,
            files=None,
            journal=None,
            session_id="test",
            config=None,
        )
        ctx.lsp = MagicMock()

        async def run():
            with patch("tools.lsp.run_lsp.hover") as mock_run:
                mock_run.return_value = "type: str"
                return await hover(ctx, "a.py", 3, 7)

        result = asyncio.run(run())
        assert result == "type: str"

    def test_hover_with_string_coords(self, tmp_path):
        ctx = ToolContext(
            workspace=tmp_path,
            files=None,
            journal=None,
            session_id="test",
            config=None,
        )
        ctx.lsp = MagicMock()

        async def run():
            with patch("tools.lsp.run_lsp.hover") as mock_run:
                mock_run.return_value = "ok"
                await hover(ctx, "a.py", "15", "25")
                args = mock_run.call_args[0]
                assert args[3] == 15
                assert args[4] == 25

        asyncio.run(run())


class TestGetDiagnostics:
    """Tests for get_diagnostics tool wrapper."""

    def test_get_diagnostics_without_lsp(self, tmp_path):
        ctx = ToolContext(
            workspace=tmp_path,
            files=None,
            journal=None,
            session_id="test",
            config=None,
        )
        ctx.lsp = None

        async def run():
            return await get_diagnostics(ctx, "a.py")

        result = asyncio.run(run())
        assert result.startswith("error:")
        assert "LSP is not available" in result

    def test_get_diagnostics_with_lsp(self, tmp_path):
        ctx = ToolContext(
            workspace=tmp_path,
            files=None,
            journal=None,
            session_id="test",
            config=None,
        )
        ctx.lsp = MagicMock()

        async def run():
            with patch("tools.lsp.run_lsp.get_diagnostics") as mock_run:
                mock_run.return_value = "No diagnostics for 'a.py' (clean)."
                return await get_diagnostics(ctx, "a.py")

        result = asyncio.run(run())
        assert "No diagnostics" in result or "a.py" in result

    def test_get_diagnostics_passes_path(self, tmp_path):
        ctx = ToolContext(
            workspace=tmp_path,
            files=None,
            journal=None,
            session_id="test",
            config=None,
        )
        ctx.lsp = MagicMock()

        async def run():
            with patch("tools.lsp.run_lsp.get_diagnostics") as mock_run:
                mock_run.return_value = "ok"
                await get_diagnostics(ctx, "test/file.py")
                args = mock_run.call_args[0]
                assert args[2] == "test/file.py"

        asyncio.run(run())


class TestDocumentSymbols:
    """Tests for document_symbols tool wrapper."""

    def test_document_symbols_without_lsp(self, tmp_path):
        ctx = ToolContext(
            workspace=tmp_path,
            files=None,
            journal=None,
            session_id="test",
            config=None,
        )
        ctx.lsp = None

        async def run():
            return await document_symbols(ctx, "a.py")

        result = asyncio.run(run())
        assert result.startswith("error:")
        assert "LSP is not available" in result

    def test_document_symbols_with_lsp(self, tmp_path):
        ctx = ToolContext(
            workspace=tmp_path,
            files=None,
            journal=None,
            session_id="test",
            config=None,
        )
        ctx.lsp = MagicMock()

        async def run():
            with patch("tools.lsp.run_lsp.document_symbols") as mock_run:
                mock_run.return_value = "2 symbol(s)\n  Function foo  1:1\n  Function bar  5:1"
                return await document_symbols(ctx, "a.py")

        result = asyncio.run(run())
        assert "symbol" in result.lower() or result.startswith("No") or result.startswith("2")

    def test_document_symbols_with_nested_path(self, tmp_path):
        ctx = ToolContext(
            workspace=tmp_path,
            files=None,
            journal=None,
            session_id="test",
            config=None,
        )
        ctx.lsp = MagicMock()

        async def run():
            with patch("tools.lsp.run_lsp.document_symbols") as mock_run:
                mock_run.return_value = "ok"
                await document_symbols(ctx, "src/foo/bar.py")
                args = mock_run.call_args[0]
                assert args[2] == "src/foo/bar.py"

        asyncio.run(run())


class TestRenameSymbol:
    """Tests for rename_symbol tool wrapper."""

    def test_rename_symbol_without_lsp(self, tmp_path):
        ctx = ToolContext(
            workspace=tmp_path,
            files=None,
            journal=None,
            session_id="test",
            config=None,
        )
        ctx.lsp = None

        async def run():
            return await rename_symbol(ctx, "a.py", 1, 1, "new_name")

        result = asyncio.run(run())
        assert result.startswith("error:")
        assert "LSP is not available" in result

    def test_rename_symbol_returns_string_error(self, tmp_path):
        ctx = ToolContext(
            workspace=tmp_path,
            files=None,
            journal=None,
            session_id="test",
            config=None,
        )
        ctx.lsp = MagicMock()

        async def run():
            with patch("tools.lsp.run_lsp.rename_symbol") as mock_run:
                mock_run.return_value = "error: rename failed"
                return await rename_symbol(ctx, "a.py", 1, 1, "new_name")

        result = asyncio.run(run())
        assert result == "error: rename failed"

    def test_rename_symbol_passes_all_params(self, tmp_path):
        ctx = ToolContext(
            workspace=tmp_path,
            files=None,
            journal=None,
            session_id="test",
            config=None,
        )
        ctx.lsp = MagicMock()

        async def run():
            with patch("tools.lsp.run_lsp.rename_symbol") as mock_run:
                mock_run.return_value = "ok"
                await rename_symbol(ctx, "test.py", 10, 20, "renamed_var")
                args = mock_run.call_args[0]
                assert args[2] == "test.py"
                assert args[3] == 10
                assert args[4] == 20
                assert args[5] == "renamed_var"

        asyncio.run(run())

    def test_rename_symbol_with_workspace_edit_payload(self, tmp_path):
        """Test when rename_symbol returns a workspace edit dict."""
        ctx = ToolContext(
            workspace=tmp_path,
            files=None,
            journal=None,
            session_id="test",
            config=None,
        )
        ctx.lsp = MagicMock()

        async def run():
            with patch("tools.lsp.run_lsp.rename_symbol") as mock_run, \
                 patch("tools.lsp.normalize_workspace_edit") as mock_norm, \
                 patch("tools.lsp.apply_workspace_edit") as mock_apply:
                # Return a dict (workspace edit) instead of string
                mock_run.return_value = {"changes": {}}
                mock_norm.return_value = {}
                mock_apply.return_value = "ok: renamed"
                result = await rename_symbol(ctx, "a.py", 1, 1, "new_name")
                # Should try to normalize and apply
                assert mock_norm.called

        asyncio.run(run())

    def test_rename_symbol_normalize_error(self, tmp_path):
        """Test when normalize_workspace_edit raises an exception."""
        ctx = ToolContext(
            workspace=tmp_path,
            files=None,
            journal=None,
            session_id="test",
            config=None,
        )
        ctx.lsp = MagicMock()

        async def run():
            with patch("tools.lsp.run_lsp.rename_symbol") as mock_run, \
                 patch("tools.lsp.normalize_workspace_edit") as mock_norm:
                mock_run.return_value = {"changes": {}}
                mock_norm.side_effect = ValueError("bad workspace edit")
                result = await rename_symbol(ctx, "a.py", 1, 1, "new_name")
                assert result.startswith("error:")
                assert "bad workspace edit" in result

        asyncio.run(run())

    def test_rename_symbol_empty_edit(self, tmp_path):
        """Test when normalize_workspace_edit returns empty dict."""
        ctx = ToolContext(
            workspace=tmp_path,
            files=None,
            journal=None,
            session_id="test",
            config=None,
        )
        ctx.lsp = MagicMock()

        async def run():
            with patch("tools.lsp.run_lsp.rename_symbol") as mock_run, \
                 patch("tools.lsp.normalize_workspace_edit") as mock_norm:
                mock_run.return_value = {"changes": {}}
                mock_norm.return_value = {}  # empty
                result = await rename_symbol(ctx, "a.py", 1, 1, "new_name")
                assert result.startswith("error:")
                assert "no file edits" in result

        asyncio.run(run())

    def test_rename_symbol_with_string_coords(self, tmp_path):
        ctx = ToolContext(
            workspace=tmp_path,
            files=None,
            journal=None,
            session_id="test",
            config=None,
        )
        ctx.lsp = MagicMock()

        async def run():
            with patch("tools.lsp.run_lsp.rename_symbol") as mock_run:
                mock_run.return_value = "ok"
                await rename_symbol(ctx, "a.py", "30", "40", "new_name")
                args = mock_run.call_args[0]
                assert args[3] == 30
                assert args[4] == 40

        asyncio.run(run())


class TestGotoDefinitionEdgeCases:
    """Edge cases and error handling for goto_definition."""

    def test_goto_definition_with_int_zero(self, tmp_path):
        ctx = ToolContext(
            workspace=tmp_path,
            files=None,
            journal=None,
            session_id="test",
            config=None,
        )
        ctx.lsp = MagicMock()

        async def run():
            with patch("tools.lsp.run_lsp.goto_definition") as mock_run:
                mock_run.return_value = "ok"
                await goto_definition(ctx, "a.py", 0, 0)
                args = mock_run.call_args[0]
                # 0 is treated as 0, not defaulted
                assert args[3] == 0
                assert args[4] == 0

        asyncio.run(run())


class TestFindReferencesEdgeCases:
    """Edge cases and error handling for find_references."""

    def test_find_references_empty_string_defaults(self, tmp_path):
        ctx = ToolContext(
            workspace=tmp_path,
            files=None,
            journal=None,
            session_id="test",
            config=None,
        )
        ctx.lsp = MagicMock()

        async def run():
            with patch("tools.lsp.run_lsp.find_references") as mock_run:
                mock_run.return_value = "ok"
                await find_references(ctx, "a.py", "", None)
                args = mock_run.call_args[0]
                assert args[3] == 1  # default
                assert args[4] == 1  # default

        asyncio.run(run())


class TestHoverEdgeCases:
    """Edge cases and error handling for hover."""

    def test_hover_negative_coords(self, tmp_path):
        ctx = ToolContext(
            workspace=tmp_path,
            files=None,
            journal=None,
            session_id="test",
            config=None,
        )
        ctx.lsp = MagicMock()

        async def run():
            with patch("tools.lsp.run_lsp.hover") as mock_run:
                mock_run.return_value = "ok"
                await hover(ctx, "a.py", -1, -5)
                args = mock_run.call_args[0]
                assert args[3] == -1
                assert args[4] == -5

        asyncio.run(run())
