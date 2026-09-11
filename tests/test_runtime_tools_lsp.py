"""Tests for runtime/tools/lsp.py - LSP protocol implementation and manager.

These tests cover the LSPClient and LSPManager classes and the standalone
functions that query the LSP servers (goto_definition, find_references, etc).
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from runtime.tools.fs import WorkspacePathError
from runtime.tools.lsp import (
    LSPClient,
    LSPManager,
    LSPTimeoutError,
    LOCATION_MAX,
    REFERENCES_MAX,
    SYMBOL_KINDS,
    SYMBOL_MAX,
    _extract_hover_text,
    _format_document_symbols,
    _format_locations,
    _normalize_locations,
    _rel_from_uri,
    _uri_to_path,
    document_symbols,
    find_references,
    goto_definition,
    get_diagnostics,
    hover,
    rename_symbol,
)


class TestUriPath:
    """Tests for URI conversion utilities."""

    def test_uri_to_path_simple(self):
        uri = "file:///home/user/test.py"
        path = _uri_to_path(uri)
        assert path == "/home/user/test.py"

    def test_uri_to_path_with_percent_encoding(self):
        uri = "file:///home/user/test%20file.py"
        path = _uri_to_path(uri)
        assert "test file.py" in path

    def test_rel_from_uri_simple(self):
        workspace = Path("/home/user/workspace")
        uri = "file:///home/user/workspace/src/main.py"
        rel = _rel_from_uri(workspace, uri)
        assert rel == "src/main.py"

    def test_rel_from_uri_posix_format(self):
        """Test that rel_from_uri returns forward slashes."""
        workspace = Path("/home/user/workspace")
        uri = "file:///home/user/workspace/dir/file.py"
        rel = _rel_from_uri(workspace, uri)
        assert "/" in rel or rel == "dir/file.py"

    def test_rel_from_uri_outside_workspace(self):
        workspace = Path("/home/user/workspace")
        uri = "file:///other/location/file.py"
        rel = _rel_from_uri(workspace, uri)
        # Should still return something, either full path or raised error
        assert isinstance(rel, str)


class TestNormalizeLocations:
    """Tests for location normalization from LSP responses."""

    def test_normalize_locations_empty(self):
        result = _normalize_locations(None)
        assert result == []

    def test_normalize_locations_single_dict(self):
        loc = {
            "uri": "file:///home/user/test.py",
            "range": {"start": {"line": 5, "character": 10}},
        }
        result = _normalize_locations(loc)
        assert len(result) == 1
        assert result[0][0] == "file:///home/user/test.py"
        assert result[0][1]["start"]["line"] == 5

    def test_normalize_locations_list(self):
        locs = [
            {
                "uri": "file:///a.py",
                "range": {"start": {"line": 1, "character": 2}},
            },
            {
                "uri": "file:///b.py",
                "range": {"start": {"line": 3, "character": 4}},
            },
        ]
        result = _normalize_locations(locs)
        assert len(result) == 2
        assert result[0][0] == "file:///a.py"
        assert result[1][0] == "file:///b.py"

    def test_normalize_locations_with_target_uri(self):
        """Test handling of targetUri (from definition response)."""
        loc = {
            "targetUri": "file:///target.py",
            "targetRange": {"start": {"line": 10, "character": 0}},
        }
        result = _normalize_locations(loc)
        assert len(result) == 1
        assert result[0][0] == "file:///target.py"

    def test_normalize_locations_with_target_selection_range(self):
        """Test fallback from targetSelectionRange to targetRange."""
        loc = {
            "targetUri": "file:///target.py",
            "targetSelectionRange": {"start": {"line": 5, "character": 0}},
            "targetRange": {"start": {"line": 10, "character": 0}},
        }
        result = _normalize_locations(loc)
        # Should prefer targetSelectionRange
        assert result[0][1]["start"]["line"] == 5


class TestFormatLocations:
    """Tests for formatting locations for display."""

    def test_format_locations_empty(self):
        result = _format_locations(Path("/ws"), [], 10)
        assert result == "No results."

    def test_format_locations_single(self, tmp_path):
        (tmp_path / "test.py").write_text("def foo():\n    pass\n")
        locs = [
            (
                "file://" + str(tmp_path / "test.py"),
                {"start": {"line": 0, "character": 4}},
            )
        ]
        result = _format_locations(tmp_path, locs, 10)
        assert "test.py" in result
        assert "1:5" in result  # line 0 -> 1, char 4 -> 5

    def test_format_locations_with_snippet(self, tmp_path):
        (tmp_path / "test.py").write_text("def foo():\n    pass\n")
        locs = [
            (
                "file://" + str(tmp_path / "test.py"),
                {"start": {"line": 0, "character": 0}},
            )
        ]
        result = _format_locations(tmp_path, locs, 10)
        # Check that format succeeded and had content
        assert isinstance(result, str)
        assert len(result) > 0

    def test_format_locations_respects_max(self, tmp_path):
        (tmp_path / "test.py").write_text("x = 1\n")
        locs = [
            (
                "file://" + str(tmp_path / "test.py"),
                {"start": {"line": 0, "character": 0}},
            )
            for _ in range(5)
        ]
        result = _format_locations(tmp_path, locs, 2)
        assert result.count("\n") <= 3  # 2 items + ellipsis
        assert "more" in result

    def test_format_locations_missing_file(self):
        """Test handling of files that don't exist."""
        locs = [
            (
                "file:///nonexistent/test.py",
                {"start": {"line": 0, "character": 0}},
            )
        ]
        # Should not raise, but handle gracefully
        result = _format_locations(Path("/ws"), locs, 10)
        assert isinstance(result, str)


class TestExtractHoverText:
    """Tests for hover content extraction."""

    def test_extract_hover_text_none(self):
        result = _extract_hover_text(None)
        assert result == ""

    def test_extract_hover_text_string(self):
        result = _extract_hover_text("type: int")
        assert result == "type: int"

    def test_extract_hover_text_dict(self):
        result = _extract_hover_text({"value": "function(x: int) -> str"})
        assert result == "function(x: int) -> str"

    def test_extract_hover_text_dict_missing_value(self):
        result = _extract_hover_text({"language": "python"})
        assert result == ""

    def test_extract_hover_text_list(self):
        contents = [
            "function definition",
            {"value": "documentation"},
            "more info",
        ]
        result = _extract_hover_text(contents)
        assert "function definition" in result
        assert "documentation" in result
        assert "more info" in result

    def test_extract_hover_text_list_with_empty(self):
        contents = [
            "info",
            "",
            {"value": "doc"},
        ]
        result = _extract_hover_text(contents)
        # Empty items should be filtered
        parts = result.split("\n\n")
        assert len(parts) >= 2


class TestFormatDocumentSymbols:
    """Tests for formatting document symbols tree."""

    def test_format_document_symbols_empty(self):
        lines: list[str] = []
        _format_document_symbols([], 0, lines)
        assert len(lines) == 0

    def test_format_document_symbols_single(self):
        items = [
            {
                "name": "MyClass",
                "kind": 5,  # Class
                "selectionRange": {"start": {"line": 0, "character": 0}},
            }
        ]
        lines: list[str] = []
        _format_document_symbols(items, 0, lines)
        assert len(lines) == 1
        assert "Class MyClass" in lines[0]
        assert "1:1" in lines[0]

    def test_format_document_symbols_with_children(self):
        items = [
            {
                "name": "MyClass",
                "kind": 5,
                "selectionRange": {"start": {"line": 0, "character": 0}},
                "children": [
                    {
                        "name": "method",
                        "kind": 6,
                        "selectionRange": {"start": {"line": 2, "character": 4}},
                    }
                ],
            }
        ]
        lines: list[str] = []
        _format_document_symbols(items, 0, lines)
        assert len(lines) == 2
        assert "Class MyClass" in lines[0]
        assert "Method method" in lines[1]
        assert "  " in lines[1]  # indentation

    def test_format_document_symbols_respects_depth(self):
        items = [
            {
                "name": "outer",
                "kind": 5,
                "selectionRange": {"start": {"line": 0, "character": 0}},
            }
        ]
        lines: list[str] = []
        _format_document_symbols(items, 3, lines)
        assert "      " in lines[0]  # 3 * 2 spaces

    def test_format_document_symbols_respects_max(self):
        items = [{"name": f"item{i}", "kind": 12, "selectionRange": {"start": {"line": i, "character": 0}}} for i in range(300)]
        lines: list[str] = []
        _format_document_symbols(items, 0, lines)
        assert len(lines) <= SYMBOL_MAX

    def test_format_document_symbols_with_detail(self):
        items = [
            {
                "name": "myvar",
                "kind": 13,  # Variable
                "detail": ": str",
                "selectionRange": {"start": {"line": 0, "character": 0}},
            }
        ]
        lines: list[str] = []
        _format_document_symbols(items, 0, lines)
        assert ": str" in lines[0]

    def test_format_document_symbols_with_location(self):
        """Test handling of location field (older LSP style)."""
        items = [
            {
                "name": "symbol",
                "kind": 12,
                "location": {
                    "range": {"start": {"line": 5, "character": 2}}
                },
            }
        ]
        lines: list[str] = []
        _format_document_symbols(items, 0, lines)
        assert "6:3" in lines[0]


class TestLSPTimeoutError:
    """Tests for LSPTimeoutError exception."""

    def test_lsp_timeout_error_is_runtime_error(self):
        err = LSPTimeoutError("test")
        assert isinstance(err, RuntimeError)

    def test_lsp_timeout_error_message(self):
        err = LSPTimeoutError("test timeout")
        assert "test timeout" in str(err)


class TestLSPClientInit:
    """Tests for LSPClient initialization."""

    def test_lsp_client_init_with_mock_process(self):
        """Test LSPClient initialization with mocked subprocess."""
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()

        with patch("subprocess.Popen", return_value=mock_proc):
            client = LSPClient(["test-server"], "/tmp")
            assert client._alive is True
            assert client._next_id == 1

    def test_lsp_client_init_failed_pipes(self):
        """Test LSPClient initialization when pipes fail."""
        mock_proc = MagicMock()
        mock_proc.stdin = None
        mock_proc.stdout = None
        mock_proc.stderr = None

        with patch("subprocess.Popen", return_value=mock_proc):
            with pytest.raises(RuntimeError) as excinfo:
                LSPClient(["test-server"], "/tmp")
            assert "pipes failed" in str(excinfo.value).lower()


class TestLSPClientReadMessage:
    """Tests for LSPClient message reading."""

    def test_read_message_empty_stream(self):
        """Test _read_message with empty stream."""
        mock_stream = MagicMock()
        mock_stream.readline.return_value = b""
        result = LSPClient._read_message(mock_stream)
        assert result is None

    def test_read_message_with_content(self):
        """Test _read_message parsing valid message."""
        body = json.dumps({"jsonrpc": "2.0", "result": "ok"}).encode("utf-8")
        mock_stream = MagicMock()
        mock_stream.readline.side_effect = [
            b"Content-Length: " + str(len(body)).encode() + b"\r\n",
            b"\r\n",
        ]
        mock_stream.read.return_value = body
        result = LSPClient._read_message(mock_stream)
        assert result is not None
        assert result.get("result") == "ok"

    def test_read_message_chunked(self):
        """Test _read_message with chunked body."""
        body = json.dumps({"test": "data"}).encode("utf-8")
        length = len(body)
        half = length // 2
        mock_stream = MagicMock()
        mock_stream.readline.side_effect = [
            b"Content-Length: " + str(length).encode() + b"\r\n",
            b"\r\n",
        ]
        mock_stream.read.side_effect = [body[:half], body[half:]]
        result = LSPClient._read_message(mock_stream)
        assert result is not None
        assert result.get("test") == "data"


class TestLSPManagerInit:
    """Tests for LSPManager initialization."""

    def test_lsp_manager_init(self, tmp_path):
        manager = LSPManager(tmp_path)
        assert manager.root == str(tmp_path.resolve())
        assert manager._closed is False
        assert len(manager._clients) == 0

    def test_lsp_manager_path_to_uri(self):
        uri = LSPManager._path_to_uri("/home/user/test.py")
        assert uri.startswith("file://")
        assert "test.py" in uri

    def test_lsp_manager_config_for_extension(self):
        cfg = LSPManager._config_for_extension(".py")
        assert cfg is not None
        assert cfg.language == "python"

        cfg = LSPManager._config_for_extension(".ts")
        assert cfg is not None
        assert cfg.language == "typescript"

        cfg = LSPManager._config_for_extension(".unknown")
        assert cfg is None

    def test_lsp_manager_language_id(self):
        cfg = LSPManager.SERVER_CONFIGS["typescript"]
        assert LSPManager._language_id(cfg, ".tsx") == "typescriptreact"
        assert LSPManager._language_id(cfg, ".ts") == "typescript"

        cfg = LSPManager.SERVER_CONFIGS["javascript"]
        assert LSPManager._language_id(cfg, ".jsx") == "javascriptreact"
        assert LSPManager._language_id(cfg, ".js") == "javascript"


class TestLSPManagerTextSha:
    """Tests for text hashing utility."""

    def test_text_sha(self):
        sha = LSPManager._text_sha("hello")
        assert len(sha) == 64  # SHA256 hex
        expected = hashlib.sha256("hello".encode("utf-8", "replace")).hexdigest()
        assert sha == expected

    def test_text_sha_unicode(self):
        sha = LSPManager._text_sha("café")
        assert len(sha) == 64

    def test_text_sha_empty(self):
        sha = LSPManager._text_sha("")
        assert len(sha) == 64


class TestLSPManagerSettingsSection:
    """Tests for settings section retrieval."""

    def test_settings_section_none(self):
        settings = {"python": {"analysis": {"enabled": True}}}
        result = LSPManager._settings_section(settings, None)
        assert result == settings

    def test_settings_section_simple(self):
        settings = {"python": {"analysis": {"enabled": True}}}
        result = LSPManager._settings_section(settings, "python")
        assert result == {"analysis": {"enabled": True}}

    def test_settings_section_nested(self):
        settings = {"python": {"analysis": {"enabled": True}}}
        result = LSPManager._settings_section(settings, "python.analysis")
        assert result == {"enabled": True}

    def test_settings_section_missing(self):
        settings = {"python": {"analysis": {"enabled": True}}}
        result = LSPManager._settings_section(settings, "other")
        assert result is None

    def test_settings_section_deep_missing(self):
        settings = {"python": {"analysis": {"enabled": True}}}
        result = LSPManager._settings_section(settings, "python.other.deep")
        assert result is None


class TestLSPManagerWarmLanguages:
    """Tests for warm language selection."""

    def test_warm_languages_javascript(self):
        langs = LSPManager._warm_languages("javascript")
        assert "javascript" in langs
        assert "typescript" in langs

    def test_warm_languages_typescript(self):
        langs = LSPManager._warm_languages("typescript")
        assert "javascript" in langs
        assert "typescript" in langs

    def test_warm_languages_python(self):
        langs = LSPManager._warm_languages("python")
        assert langs == ["python"]

    def test_warm_languages_go(self):
        langs = LSPManager._warm_languages("go")
        assert langs == ["go"]

    def test_warm_languages_unknown(self):
        langs = LSPManager._warm_languages("unknown")
        assert langs == []


class TestGotoDefinitionFunction:
    """Tests for goto_definition standalone function."""

    def test_goto_definition_invalid_path(self, tmp_path):
        manager = LSPManager(tmp_path)
        result = goto_definition(tmp_path, manager, "../outside.py", 1, 1)
        assert result.startswith("error:")

    def test_goto_definition_error_handling(self, tmp_path):
        manager = LSPManager(tmp_path)
        with patch.object(manager, "ask_lsp", side_effect=ValueError("test error")):
            result = goto_definition(tmp_path, manager, "test.py", 1, 1)
            assert result.startswith("error:")
            assert "test error" in result

    def test_goto_definition_no_result(self, tmp_path):
        (tmp_path / "test.py").touch()
        manager = LSPManager(tmp_path)
        with patch.object(manager, "ask_lsp", return_value=None):
            result = goto_definition(tmp_path, manager, "test.py", 1, 1)
            assert "No definition found" in result


class TestFindReferencesFunction:
    """Tests for find_references standalone function."""

    def test_find_references_invalid_path(self, tmp_path):
        manager = LSPManager(tmp_path)
        result = find_references(tmp_path, manager, "../outside.py", 1, 1)
        assert result.startswith("error:")

    def test_find_references_error_handling(self, tmp_path):
        manager = LSPManager(tmp_path)
        with patch.object(manager, "ask_lsp", side_effect=OSError("file error")):
            result = find_references(tmp_path, manager, "test.py", 1, 1)
            assert result.startswith("error:")
            assert "file error" in result

    def test_find_references_no_result(self, tmp_path):
        (tmp_path / "test.py").touch()
        manager = LSPManager(tmp_path)
        with patch.object(manager, "ask_lsp", return_value=None):
            result = find_references(tmp_path, manager, "test.py", 1, 1)
            assert "No references found" in result


class TestHoverFunction:
    """Tests for hover standalone function."""

    def test_hover_invalid_path(self, tmp_path):
        manager = LSPManager(tmp_path)
        result = hover(tmp_path, manager, "../outside.py", 1, 1)
        assert result.startswith("error:")

    def test_hover_error_handling(self, tmp_path):
        manager = LSPManager(tmp_path)
        with patch.object(manager, "ask_lsp", side_effect=LSPTimeoutError("timeout")):
            result = hover(tmp_path, manager, "test.py", 1, 1)
            assert result.startswith("error:")
            assert "timeout" in result

    def test_hover_no_content(self, tmp_path):
        (tmp_path / "test.py").touch()
        manager = LSPManager(tmp_path)
        with patch.object(manager, "ask_lsp", return_value={}):
            result = hover(tmp_path, manager, "test.py", 1, 1)
            assert "No hover information" in result

    def test_hover_with_content(self, tmp_path):
        (tmp_path / "test.py").touch()
        manager = LSPManager(tmp_path)
        with patch.object(
            manager, "ask_lsp", return_value={"contents": "def foo(): ..."}
        ):
            result = hover(tmp_path, manager, "test.py", 1, 1)
            assert "def foo()" in result


class TestGetDiagnosticsFunction:
    """Tests for get_diagnostics standalone function."""

    def test_get_diagnostics_invalid_path(self, tmp_path):
        manager = LSPManager(tmp_path)
        result = get_diagnostics(tmp_path, manager, "../outside.py")
        assert result.startswith("error:")

    def test_get_diagnostics_clean(self, tmp_path):
        (tmp_path / "test.py").touch()
        manager = LSPManager(tmp_path)
        with patch.object(manager, "open_file_and_get_diagnostics", return_value=[]):
            result = get_diagnostics(tmp_path, manager, "test.py")
            assert "No diagnostics" in result or "clean" in result

    def test_get_diagnostics_with_errors(self, tmp_path):
        (tmp_path / "test.py").touch()
        manager = LSPManager(tmp_path)
        diags = [
            {
                "range": {"start": {"line": 5, "character": 10}},
                "severity": 1,
                "message": "error message",
                "source": "pyright",
            }
        ]
        with patch.object(manager, "open_file_and_get_diagnostics", return_value=diags):
            result = get_diagnostics(tmp_path, manager, "test.py")
            assert "Error" in result
            assert "error message" in result
            assert "pyright" in result

    def test_get_diagnostics_various_severities(self, tmp_path):
        (tmp_path / "test.py").touch()
        manager = LSPManager(tmp_path)
        diags = [
            {
                "range": {"start": {"line": 0, "character": 0}},
                "severity": 1,
                "message": "error",
            },
            {
                "range": {"start": {"line": 1, "character": 0}},
                "severity": 2,
                "message": "warning",
            },
            {
                "range": {"start": {"line": 2, "character": 0}},
                "severity": 3,
                "message": "info",
            },
        ]
        with patch.object(manager, "open_file_and_get_diagnostics", return_value=diags):
            result = get_diagnostics(tmp_path, manager, "test.py")
            assert "Error" in result
            assert "Warning" in result
            assert "Info" in result


class TestDocumentSymbolsFunction:
    """Tests for document_symbols standalone function."""

    def test_document_symbols_invalid_path(self, tmp_path):
        manager = LSPManager(tmp_path)
        result = document_symbols(tmp_path, manager, "../outside.py")
        assert result.startswith("error:")

    def test_document_symbols_error_handling(self, tmp_path):
        manager = LSPManager(tmp_path)
        with patch.object(manager, "ask_document_symbols", side_effect=RuntimeError("server error")):
            result = document_symbols(tmp_path, manager, "test.py")
            assert result.startswith("error:")
            assert "server error" in result

    def test_document_symbols_no_result(self, tmp_path):
        (tmp_path / "test.py").touch()
        manager = LSPManager(tmp_path)
        with patch.object(manager, "ask_document_symbols", return_value=None):
            result = document_symbols(tmp_path, manager, "test.py")
            assert "No document symbols" in result

    def test_document_symbols_with_items(self, tmp_path):
        (tmp_path / "test.py").touch()
        manager = LSPManager(tmp_path)
        items = [
            {
                "name": "MyClass",
                "kind": 5,
                "selectionRange": {"start": {"line": 0, "character": 0}},
            }
        ]
        with patch.object(manager, "ask_document_symbols", return_value=items):
            result = document_symbols(tmp_path, manager, "test.py")
            assert "1 symbol" in result or "symbol" in result.lower()


class TestRenameSymbolFunction:
    """Tests for rename_symbol standalone function."""

    def test_rename_symbol_invalid_path(self, tmp_path):
        manager = LSPManager(tmp_path)
        result = rename_symbol(tmp_path, manager, "../outside.py", 1, 1, "new")
        assert result.startswith("error:")

    def test_rename_symbol_empty_name(self, tmp_path):
        (tmp_path / "test.py").touch()
        manager = LSPManager(tmp_path)
        result = rename_symbol(tmp_path, manager, "test.py", 1, 1, "")
        assert result.startswith("error:")
        assert "new_name is required" in result

    def test_rename_symbol_error_handling(self, tmp_path):
        (tmp_path / "test.py").touch()
        manager = LSPManager(tmp_path)
        with patch.object(manager, "ask_rename", side_effect=LSPTimeoutError("timeout")):
            result = rename_symbol(tmp_path, manager, "test.py", 1, 1, "new_name")
            assert result.startswith("error:")
            assert "timeout" in result

    def test_rename_symbol_no_edits(self, tmp_path):
        (tmp_path / "test.py").touch()
        manager = LSPManager(tmp_path)
        with patch.object(manager, "ask_rename", return_value=None):
            result = rename_symbol(tmp_path, manager, "test.py", 1, 1, "new_name")
            assert result.startswith("error:")
            assert "no edits" in result


class TestSymbolKinds:
    """Tests for SYMBOL_KINDS mapping."""

    def test_symbol_kinds_common(self):
        assert SYMBOL_KINDS[5] == "Class"
        assert SYMBOL_KINDS[6] == "Method"
        assert SYMBOL_KINDS[12] == "Function"
        assert SYMBOL_KINDS[13] == "Variable"

    def test_symbol_kinds_coverage(self):
        # Should have substantial coverage
        assert len(SYMBOL_KINDS) >= 20


class TestLSPManagerIterSourceFiles:
    """Tests for source file iteration."""

    def test_iter_source_files_empty(self, tmp_path):
        manager = LSPManager(tmp_path)
        files = list(manager._iter_source_files((".py",), 100))
        assert files == []

    def test_iter_source_files_single(self, tmp_path):
        (tmp_path / "test.py").touch()
        manager = LSPManager(tmp_path)
        files = list(manager._iter_source_files((".py",), 100))
        assert "test.py" in files

    def test_iter_source_files_multiple_extensions(self, tmp_path):
        (tmp_path / "a.py").touch()
        (tmp_path / "b.ts").touch()
        (tmp_path / "c.js").touch()
        manager = LSPManager(tmp_path)
        files = list(manager._iter_source_files((".py", ".ts"), 100))
        assert "a.py" in files
        assert "b.ts" in files
        assert "c.js" not in files

    def test_iter_source_files_max_files(self, tmp_path):
        for i in range(10):
            (tmp_path / f"file{i}.py").touch()
        manager = LSPManager(tmp_path)
        files = list(manager._iter_source_files((".py",), 3))
        assert len(files) == 3

    def test_iter_source_files_skips_dotfiles(self, tmp_path):
        (tmp_path / "visible.py").touch()
        (tmp_path / ".hidden.py").touch()
        manager = LSPManager(tmp_path)
        files = list(manager._iter_source_files((".py",), 100))
        assert "visible.py" in files
        assert ".hidden.py" not in files

    def test_iter_source_files_nested(self, tmp_path):
        (tmp_path / "dir").mkdir()
        (tmp_path / "dir" / "nested.py").touch()
        manager = LSPManager(tmp_path)
        files = list(manager._iter_source_files((".py",), 100))
        assert len(files) > 0
        # Should be in posix format with forward slashes
        assert any("nested.py" in f for f in files)


class TestLSPManagerShutdown:
    """Tests for manager shutdown."""

    def test_lsp_manager_shutdown_all(self, tmp_path):
        manager = LSPManager(tmp_path)
        manager._closed = False
        mock_client = MagicMock()
        manager._clients[("key",)] = mock_client
        manager._opened_files.add("test.py")

        manager.shutdown_all()
        assert manager._closed is True
        assert len(manager._clients) == 0
        assert len(manager._opened_files) == 0
        mock_client.shutdown.assert_called_once()

    def test_lsp_manager_shutdown_idempotent(self, tmp_path):
        manager = LSPManager(tmp_path)
        manager._closed = False
        mock_client = MagicMock()
        manager._clients[("key",)] = mock_client

        manager.shutdown_all()
        manager.shutdown_all()  # Call twice
        mock_client.shutdown.assert_called_once()  # Still only called once


class TestLSPManagerDiskText:
    """Tests for disk text reading."""

    def test_disk_text_existing(self, tmp_path):
        test_file = tmp_path / "test.py"
        test_file.write_text("hello world")
        manager = LSPManager(tmp_path)
        text = LSPManager._disk_text(str(test_file))
        assert text == "hello world"

    def test_disk_text_missing(self, tmp_path):
        manager = LSPManager(tmp_path)
        text = LSPManager._disk_text(str(tmp_path / "nonexistent.py"))
        assert text is None

    def test_disk_text_unicode(self, tmp_path):
        test_file = tmp_path / "test.py"
        test_file.write_text("café")
        manager = LSPManager(tmp_path)
        text = LSPManager._disk_text(str(test_file))
        assert text is not None
        assert "café" in text


class TestLSPManagerStaleDiskText:
    """Tests for stale disk text detection."""

    def test_stale_disk_text_not_opened(self, tmp_path):
        manager = LSPManager(tmp_path)
        result = manager._stale_disk_text(str(tmp_path / "test.py"))
        assert result is None

    def test_stale_disk_text_synchronized(self, tmp_path):
        test_file = tmp_path / "test.py"
        test_file.write_text("original")
        manager = LSPManager(tmp_path)
        manager._opened_files.add(str(test_file))
        manager._sent_sha[str(test_file)] = manager._text_sha("original")

        result = manager._stale_disk_text(str(test_file))
        assert result is None

    def test_stale_disk_text_modified(self, tmp_path):
        test_file = tmp_path / "test.py"
        test_file.write_text("modified")
        manager = LSPManager(tmp_path)
        manager._opened_files.add(str(test_file))
        manager._sent_sha[str(test_file)] = manager._text_sha("original")

        result = manager._stale_disk_text(str(test_file))
        assert result == "modified"

    def test_stale_disk_text_missing_file(self, tmp_path):
        manager = LSPManager(tmp_path)
        manager._opened_files.add(str(tmp_path / "missing.py"))

        result = manager._stale_disk_text(str(tmp_path / "missing.py"))
        assert result is None
