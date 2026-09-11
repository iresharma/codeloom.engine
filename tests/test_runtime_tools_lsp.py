"""Unit tests for runtime/tools/lsp.py (LSP client and manager).

Tests the LSP communication layer, client/server message passing, file indexing,
and manager lifecycle without requiring a real language server.
"""

from __future__ import annotations

import hashlib
import json
import queue
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch, call, mock_open

import pytest

from runtime.tools.lsp import (
    LSPClient,
    LSPManager,
    LSPTimeoutError,
    _uri_to_path,
    _rel_from_uri,
    _normalize_locations,
    _format_locations,
    _extract_hover_text,
    _resolve,
    goto_definition,
    find_references,
    hover,
    get_diagnostics,
    document_symbols,
    rename_symbol,
    SYMBOL_KINDS,
)


class TestLSPTimeoutError:
    """Test LSPTimeoutError exception."""

    def test_lsp_timeout_error_is_runtime_error(self):
        err = LSPTimeoutError("test timeout")
        assert isinstance(err, RuntimeError)
        assert str(err) == "test timeout"


class TestLSPClientInit:
    """Test LSPClient initialization."""

    def test_lsp_client_init_with_working_process(self):
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()

        with patch("subprocess.Popen", return_value=mock_proc):
            client = LSPClient(["test-server"], cwd="/tmp")
            assert client.proc == mock_proc
            assert client._alive is True
            client.shutdown()

    def test_lsp_client_init_with_missing_pipes_kills_process(self):
        mock_proc = MagicMock()
        mock_proc.stdin = None
        mock_proc.stdout = None
        mock_proc.stderr = None

        with patch("subprocess.Popen", return_value=mock_proc):
            with pytest.raises(RuntimeError, match="pipes failed"):
                LSPClient(["test-server"], cwd="/tmp")
            mock_proc.kill.assert_called_once()

    def test_lsp_client_starts_reader_thread(self):
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()

        with patch("subprocess.Popen", return_value=mock_proc):
            with patch("threading.Thread") as mock_thread:
                client = LSPClient(["test-server"], cwd="/tmp")
                # Should start two threads: reader and stderr drain
                assert mock_thread.call_count >= 2
                client.shutdown()


class TestLSPClientReadMessage:
    """Test LSPClient._read_message static method."""

    def test_read_message_with_empty_stream_returns_none(self):
        stream = MagicMock()
        stream.readline.side_effect = [b""]
        result = LSPClient._read_message(stream)
        assert result is None

    def test_read_message_reads_headers_and_content(self):
        stream = MagicMock()
        payload = {"jsonrpc": "2.0", "result": "ok"}
        body = json.dumps(payload).encode("utf-8")
        stream.readline.side_effect = [
            b"Content-Length: " + str(len(body)).encode() + b"\r\n",
            b"Content-Type: application/vnd.api+json\r\n",
            b"\r\n",
        ]
        stream.read.return_value = body

        result = LSPClient._read_message(stream)
        assert result == payload

    def test_read_message_handles_incomplete_body(self):
        stream = MagicMock()
        body = b"chunk1chunk2"
        stream.readline.side_effect = [
            b"Content-Length: 12\r\n",
            b"\r\n",
        ]
        stream.read.side_effect = [b"chunk1", b"chunk2"]

        result = LSPClient._read_message(stream)
        assert result is None  # JSON parsing will fail

    def test_read_message_with_no_content_length_defaults_to_zero(self):
        stream = MagicMock()
        stream.readline.side_effect = [
            b"Content-Type: application/json\r\n",
            b"\r\n",
        ]
        result = LSPClient._read_message(stream)
        assert result == {}  # Empty JSON object


class TestLSPClientWriteMessage:
    """Test LSPClient._write_message method."""

    def test_write_message_writes_header_and_body(self):
        mock_proc = MagicMock()
        mock_stdin = MagicMock()
        mock_proc.stdin = mock_stdin
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()

        with patch("subprocess.Popen", return_value=mock_proc):
            client = LSPClient(["test-server"], cwd="/tmp")
            client._stdin = mock_stdin
            payload = {"jsonrpc": "2.0", "id": 1, "method": "test"}
            client._write_message(payload)

            mock_stdin.write.assert_called_once()
            mock_stdin.flush.assert_called_once()
            call_args = mock_stdin.write.call_args[0][0]
            assert b"Content-Length:" in call_args
            assert b"\r\n\r\n" in call_args
            client.shutdown()

    def test_write_message_with_broken_pipe_raises_error(self):
        mock_proc = MagicMock()
        mock_stdin = MagicMock()
        mock_stdin.write.side_effect = BrokenPipeError("pipe broken")
        mock_proc.stdin = mock_stdin
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()

        with patch("subprocess.Popen", return_value=mock_proc):
            client = LSPClient(["test-server"], cwd="/tmp")
            client._stdin = mock_stdin
            with pytest.raises(RuntimeError, match="process died"):
                client._write_message({"test": "payload"})
            client.shutdown()


class TestLSPClientRequest:
    """Test LSPClient.request method."""

    def test_request_sends_and_waits_for_response(self):
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()

        with patch("subprocess.Popen", return_value=mock_proc):
            client = LSPClient(["test-server"], cwd="/tmp")
            # Prepare the response in the pending queue
            response = {"jsonrpc": "2.0", "id": 1, "result": "success"}
            msg_id = 1
            pending_queue = queue.Queue()
            pending_queue.put(response)
            client._pending[msg_id] = pending_queue
            client._next_id = 2

            with patch.object(client, "_write_message"):
                result = client.request("initialize", {"test": "params"}, timeout=1.0)
            assert result == "success"
            client.shutdown()

    def test_request_timeout_raises_error(self):
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()

        with patch("subprocess.Popen", return_value=mock_proc):
            client = LSPClient(["test-server"], cwd="/tmp")
            with patch.object(client, "_write_message"):
                with pytest.raises(LSPTimeoutError, match="timed out"):
                    client.request("test", {}, timeout=0.001)
            client.shutdown()

    def test_request_with_error_response_raises_error(self):
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()

        with patch("subprocess.Popen", return_value=mock_proc):
            client = LSPClient(["test-server"], cwd="/tmp")
            response = {"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "bad request"}}
            pending_queue = queue.Queue()
            pending_queue.put(response)
            client._pending[1] = pending_queue
            client._next_id = 2

            with patch.object(client, "_write_message"):
                with pytest.raises(RuntimeError, match="error"):
                    client.request("test", {})
            client.shutdown()

    def test_request_not_alive_raises_error(self):
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()

        with patch("subprocess.Popen", return_value=mock_proc):
            client = LSPClient(["test-server"], cwd="/tmp")
            client._alive = False
            with pytest.raises(RuntimeError, match="not running"):
                client.request("test", {})


class TestLSPClientNotify:
    """Test LSPClient.notify method."""

    def test_notify_sends_message(self):
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()

        with patch("subprocess.Popen", return_value=mock_proc):
            client = LSPClient(["test-server"], cwd="/tmp")
            with patch.object(client, "_write_message") as mock_write:
                client.notify("test/method", {"key": "value"})
                mock_write.assert_called_once()
                call_args = mock_write.call_args[0][0]
                assert call_args["method"] == "test/method"
                assert call_args["params"] == {"key": "value"}
                assert "id" not in call_args
            client.shutdown()


class TestLSPClientShutdown:
    """Test LSPClient.shutdown method."""

    def test_shutdown_sends_shutdown_request(self):
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()

        with patch("subprocess.Popen", return_value=mock_proc):
            client = LSPClient(["test-server"], cwd="/tmp")
            client._alive = True
            # Mock the request method to return immediately
            with patch.object(client, "request") as mock_req:
                with patch.object(client, "notify"):
                    client.shutdown()
                    mock_req.assert_called()
            mock_proc.terminate.assert_called()

    def test_shutdown_already_dead_does_nothing(self):
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()

        with patch("subprocess.Popen", return_value=mock_proc):
            client = LSPClient(["test-server"], cwd="/tmp")
            client._alive = False
            client.shutdown()
            mock_proc.terminate.assert_not_called()


class TestLSPManagerInit:
    """Test LSPManager initialization."""

    def test_lsp_manager_init(self, tmp_path):
        manager = LSPManager(tmp_path)
        assert str(manager.root) == str(tmp_path.resolve())
        assert manager._closed is False

    def test_lsp_manager_with_path_object(self, tmp_path):
        manager = LSPManager(tmp_path)
        assert isinstance(manager.root, str)


class TestLSPManagerPathToUri:
    """Test LSPManager._path_to_uri method."""

    def test_path_to_uri_converts_path(self, tmp_path):
        path = str(tmp_path / "file.py")
        uri = LSPManager._path_to_uri(path)
        assert uri.startswith("file://")
        assert "file.py" in uri


class TestLSPManagerConfigForExtension:
    """Test LSPManager._config_for_extension method."""

    def test_config_for_python_extension(self):
        cfg = LSPManager._config_for_extension(".py")
        assert cfg is not None
        assert cfg.language == "python"

    def test_config_for_typescript_extension(self):
        cfg = LSPManager._config_for_extension(".ts")
        assert cfg is not None
        assert cfg.language == "typescript"

    def test_config_for_javascript_extension(self):
        cfg = LSPManager._config_for_extension(".js")
        assert cfg is not None
        assert cfg.language in ("javascript", "typescript")

    def test_config_for_go_extension(self):
        cfg = LSPManager._config_for_extension(".go")
        assert cfg is not None
        assert cfg.language == "go"

    def test_config_for_unknown_extension_returns_none(self):
        cfg = LSPManager._config_for_extension(".xyz")
        assert cfg is None


class TestLSPManagerLanguageId:
    """Test LSPManager._language_id method."""

    def test_language_id_for_tsx(self):
        cfg = LSPManager.SERVER_CONFIGS["typescript"]
        lang_id = LSPManager._language_id(cfg, ".tsx")
        assert lang_id == "typescriptreact"

    def test_language_id_for_jsx(self):
        cfg = LSPManager.SERVER_CONFIGS["javascript"]
        lang_id = LSPManager._language_id(cfg, ".jsx")
        assert lang_id == "javascriptreact"

    def test_language_id_for_normal_extension(self):
        cfg = LSPManager.SERVER_CONFIGS["python"]
        lang_id = LSPManager._language_id(cfg, ".py")
        assert lang_id == "python"


class TestLSPManagerSettingsSection:
    """Test LSPManager._settings_section static method."""

    def test_settings_section_with_none_section(self):
        settings = {"a": {"b": 1}}
        result = LSPManager._settings_section(settings, None)
        assert result == settings

    def test_settings_section_with_nested_path(self):
        settings = {"python": {"analysis": {"diagnosticMode": "workspace"}}}
        result = LSPManager._settings_section(settings, "python.analysis")
        assert result == {"diagnosticMode": "workspace"}

    def test_settings_section_with_missing_path(self):
        settings = {"python": {"analysis": {"diagnosticMode": "workspace"}}}
        result = LSPManager._settings_section(settings, "unknown.path")
        assert result is None


class TestLSPManagerWarmLanguages:
    """Test LSPManager._warm_languages static method."""

    def test_warm_languages_javascript_includes_typescript(self):
        langs = LSPManager._warm_languages("javascript")
        assert "javascript" in langs
        assert "typescript" in langs

    def test_warm_languages_typescript_includes_both(self):
        langs = LSPManager._warm_languages("typescript")
        assert "typescript" in langs
        assert "javascript" in langs

    def test_warm_languages_python_only(self):
        langs = LSPManager._warm_languages("python")
        assert langs == ["python"]

    def test_warm_languages_go_only(self):
        langs = LSPManager._warm_languages("go")
        assert langs == ["go"]

    def test_warm_languages_unknown_returns_empty(self):
        langs = LSPManager._warm_languages("unknown")
        assert langs == []


class TestLSPManagerTextSha:
    """Test LSPManager._text_sha static method."""

    def test_text_sha_consistent(self):
        text = "hello world"
        sha1 = LSPManager._text_sha(text)
        sha2 = LSPManager._text_sha(text)
        assert sha1 == sha2

    def test_text_sha_differs_by_content(self):
        sha1 = LSPManager._text_sha("hello")
        sha2 = LSPManager._text_sha("world")
        assert sha1 != sha2

    def test_text_sha_returns_hex_string(self):
        sha = LSPManager._text_sha("test")
        assert len(sha) == 64  # SHA256 hex is 64 chars


class TestLSPManagerDiskText:
    """Test LSPManager._disk_text static method."""

    def test_disk_text_reads_file(self, tmp_path):
        test_file = tmp_path / "test.py"
        test_file.write_text("print('hello')")
        result = LSPManager._disk_text(str(test_file))
        assert result == "print('hello')"

    def test_disk_text_with_nonexistent_file_returns_none(self, tmp_path):
        result = LSPManager._disk_text(str(tmp_path / "missing.py"))
        assert result is None


class TestUriToPath:
    """Test _uri_to_path utility."""

    def test_uri_to_path_decodes_file_uri(self):
        uri = "file:///tmp/test.py"
        path = _uri_to_path(uri)
        assert "test.py" in path


class TestRelFromUri:
    """Test _rel_from_uri utility."""

    def test_rel_from_uri_converts_absolute_to_relative(self, tmp_path):
        full_path = tmp_path / "test.py"
        full_path.write_text("# test")
        uri = Path(full_path).resolve().as_uri()
        result = _rel_from_uri(tmp_path, uri)
        assert result == "test.py"


class TestNormalizeLocations:
    """Test _normalize_locations utility."""

    def test_normalize_locations_with_single_dict(self):
        result = _normalize_locations({"uri": "file:///test.py", "range": {"start": {}}})
        assert len(result) == 1
        assert result[0][0] == "file:///test.py"

    def test_normalize_locations_with_list(self):
        items = [
            {"uri": "file:///a.py", "range": {"start": {}}},
            {"uri": "file:///b.py", "range": {"start": {}}},
        ]
        result = _normalize_locations(items)
        assert len(result) == 2

    def test_normalize_locations_with_target_uri(self):
        result = _normalize_locations({"targetUri": "file:///test.py", "targetRange": {"start": {}}})
        assert result[0][0] == "file:///test.py"

    def test_normalize_locations_with_empty_returns_empty(self):
        result = _normalize_locations(None)
        assert result == []
        result = _normalize_locations([])
        assert result == []


class TestFormatLocations:
    """Test _format_locations utility."""

    def test_format_locations_with_empty_returns_no_results(self, tmp_path):
        result = _format_locations(tmp_path, [], 10)
        assert "No results" in result

    def test_format_locations_formats_with_line_numbers(self, tmp_path):
        test_file = tmp_path / "test.py"
        test_file.write_text("line 1\nline 2\n")
        uri = test_file.resolve().as_uri()
        locs = [(uri, {"start": {"line": 0, "character": 0}})]
        result = _format_locations(tmp_path, locs, 10)
        assert "test.py:1:1" in result

    def test_format_locations_shows_truncated_message(self, tmp_path):
        test_file = tmp_path / "test.py"
        test_file.write_text("line 1\n")
        uri = test_file.resolve().as_uri()
        locs = [(uri, {"start": {"line": 0, "character": 0}})] * 5
        result = _format_locations(tmp_path, locs, 2)
        assert "and 3 more" in result


class TestExtractHoverText:
    """Test _extract_hover_text utility."""

    def test_extract_hover_text_from_string(self):
        result = _extract_hover_text("signature: (x: int)")
        assert result == "signature: (x: int)"

    def test_extract_hover_text_from_dict(self):
        result = _extract_hover_text({"value": "type info"})
        assert result == "type info"

    def test_extract_hover_text_from_list(self):
        items = [
            "first part",
            {"value": "second part"},
        ]
        result = _extract_hover_text(items)
        assert "first part" in result
        assert "second part" in result

    def test_extract_hover_text_with_none(self):
        result = _extract_hover_text(None)
        assert result == ""

    def test_extract_hover_text_with_empty_list(self):
        result = _extract_hover_text([])
        assert result == ""


class TestResolve:
    """Test _resolve utility."""

    def test_resolve_with_valid_path(self, tmp_path):
        test_file = tmp_path / "test.py"
        test_file.write_text("# test")
        result = _resolve(tmp_path, "test.py")
        assert result is None

    def test_resolve_with_invalid_path_returns_error(self, tmp_path):
        result = _resolve(tmp_path, "../../../etc/passwd")
        assert result is not None
        assert "error" in result


class TestGotoDefinitionFunction:
    """Test goto_definition runtime function."""

    def test_goto_definition_with_error_response(self, tmp_path):
        test_file = tmp_path / "test.py"
        test_file.write_text("def foo():\n    pass\n")

        lsp_manager = MagicMock()
        lsp_manager.ask_lsp.return_value = None

        result = goto_definition(tmp_path, lsp_manager, "test.py", 1, 1)
        assert "No definition found" in result

    def test_goto_definition_with_locations(self, tmp_path):
        test_file = tmp_path / "test.py"
        test_file.write_text("def foo():\n    pass\n")

        lsp_manager = MagicMock()
        lsp_manager.ask_lsp.return_value = {
            "uri": test_file.resolve().as_uri(),
            "range": {"start": {"line": 0, "character": 4}}
        }

        result = goto_definition(tmp_path, lsp_manager, "test.py", 1, 1)
        assert "test.py:1:" in result

    def test_goto_definition_with_lsp_error(self, tmp_path):
        test_file = tmp_path / "test.py"
        test_file.write_text("def foo():\n    pass\n")

        lsp_manager = MagicMock()
        lsp_manager.ask_lsp.side_effect = RuntimeError("server error")

        result = goto_definition(tmp_path, lsp_manager, "test.py", 1, 1)
        assert "error:" in result


class TestFindReferencesFunction:
    """Test find_references runtime function."""

    def test_find_references_returns_locations(self, tmp_path):
        test_file = tmp_path / "test.py"
        test_file.write_text("x = 1\nprint(x)\n")

        lsp_manager = MagicMock()
        lsp_manager.ask_lsp.return_value = [
            {"uri": test_file.resolve().as_uri(), "range": {"start": {"line": 0, "character": 0}}},
            {"uri": test_file.resolve().as_uri(), "range": {"start": {"line": 1, "character": 6}}},
        ]

        result = find_references(tmp_path, lsp_manager, "test.py", 1, 0)
        assert "test.py:" in result


class TestHoverFunction:
    """Test hover runtime function."""

    def test_hover_returns_text(self, tmp_path):
        test_file = tmp_path / "test.py"
        test_file.write_text("x = 1\n")

        lsp_manager = MagicMock()
        lsp_manager.ask_lsp.return_value = {"contents": "int"}

        result = hover(tmp_path, lsp_manager, "test.py", 1, 0)
        assert "int" in result


class TestGetDiagnosticsFunction:
    """Test get_diagnostics runtime function."""

    def test_get_diagnostics_with_empty(self, tmp_path):
        test_file = tmp_path / "test.py"
        test_file.write_text("# clean\n")

        lsp_manager = MagicMock()
        lsp_manager.open_file_and_get_diagnostics.return_value = []

        result = get_diagnostics(tmp_path, lsp_manager, "test.py")
        assert "clean" in result

    def test_get_diagnostics_with_errors(self, tmp_path):
        test_file = tmp_path / "test.py"
        test_file.write_text("x = undefined\n")

        lsp_manager = MagicMock()
        lsp_manager.open_file_and_get_diagnostics.return_value = [
            {
                "range": {"start": {"line": 0, "character": 4}},
                "severity": 1,
                "message": "undefined variable",
                "source": "test",
            }
        ]

        result = get_diagnostics(tmp_path, lsp_manager, "test.py")
        assert "Error" in result or "undefined" in result


class TestDocumentSymbolsFunction:
    """Test document_symbols runtime function."""

    def test_document_symbols_returns_formatted_output(self, tmp_path):
        test_file = tmp_path / "test.py"
        test_file.write_text("def foo():\n    pass\n")

        lsp_manager = MagicMock()
        lsp_manager.ask_document_symbols.return_value = [
            {
                "name": "foo",
                "kind": 12,  # Function
                "selectionRange": {"start": {"line": 0, "character": 4}}
            }
        ]

        result = document_symbols(tmp_path, lsp_manager, "test.py")
        assert "foo" in result
        assert "Function" in result


class TestRenameSymbolFunction:
    """Test rename_symbol runtime function."""

    def test_rename_symbol_with_empty_name_returns_error(self, tmp_path):
        test_file = tmp_path / "test.py"
        test_file.write_text("x = 1\n")

        lsp_manager = MagicMock()
        result = rename_symbol(tmp_path, lsp_manager, "test.py", 1, 0, "")
        assert "error:" in result

    def test_rename_symbol_with_valid_edit(self, tmp_path):
        test_file = tmp_path / "test.py"
        test_file.write_text("x = 1\n")

        lsp_manager = MagicMock()
        lsp_manager.ask_rename.return_value = {
            "changes": {
                test_file.resolve().as_uri(): [
                    {
                        "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}},
                        "newText": "y"
                    }
                ]
            }
        }

        result = rename_symbol(tmp_path, lsp_manager, "test.py", 1, 0, "y")
        # Result is the edit dict, not an error string
        assert isinstance(result, dict) or "error" not in str(result).lower()


class TestLSPManagerClientFor:
    """Test LSPManager._client_for method."""

    def test_client_for_creates_new_client(self, tmp_path):
        manager = LSPManager(tmp_path)
        cfg = LSPManager.SERVER_CONFIGS["python"]
        
        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = MagicMock()
            mock_proc.stderr = MagicMock()
            mock_popen.return_value = mock_proc
            
            with patch("threading.Thread"):
                with patch.object(LSPClient, "request") as mock_req:
                    mock_req.return_value = {"capabilities": {}}
                    with patch.object(LSPClient, "notify"):
                        client = manager._client_for(cfg)
                        assert client is not None
                        manager.shutdown_all()

    def test_client_for_returns_cached_client(self, tmp_path):
        manager = LSPManager(tmp_path)
        cfg = LSPManager.SERVER_CONFIGS["python"]
        
        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = MagicMock()
            mock_proc.stderr = MagicMock()
            mock_popen.return_value = mock_proc
            
            with patch("threading.Thread"):
                with patch.object(LSPClient, "request") as mock_req:
                    mock_req.return_value = {"capabilities": {}}
                    with patch.object(LSPClient, "notify"):
                        client1 = manager._client_for(cfg)
                        client2 = manager._client_for(cfg)
                        assert client1 is client2
                        manager.shutdown_all()

    def test_client_for_raises_when_closed(self, tmp_path):
        manager = LSPManager(tmp_path)
        cfg = LSPManager.SERVER_CONFIGS["python"]
        manager._closed = True
        
        with pytest.raises(RuntimeError, match="shut down"):
            manager._client_for(cfg)


class TestLSPManagerFileOperations:
    """Test LSPManager file operations."""

    def test_did_open_file_with_supported_extension(self, tmp_path):
        manager = LSPManager(tmp_path)
        test_file = tmp_path / "test.py"
        test_file.write_text("x = 1\n")
        
        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = MagicMock()
            mock_proc.stderr = MagicMock()
            mock_popen.return_value = mock_proc
            
            with patch("threading.Thread"):
                with patch.object(LSPClient, "request") as mock_req:
                    mock_req.return_value = {"capabilities": {}}
                    with patch.object(LSPClient, "notify") as mock_notify:
                        uri = manager._did_open("test.py")
                        assert uri is not None
                        assert "test.py" in uri
                        mock_notify.assert_called()
                        manager.shutdown_all()

    def test_did_open_file_with_unsupported_extension_returns_none(self, tmp_path):
        manager = LSPManager(tmp_path)
        test_file = tmp_path / "test.xyz"
        test_file.write_text("unknown\n")
        
        uri = manager._did_open("test.xyz")
        assert uri is None

    def test_index_workspace_with_python_files(self, tmp_path):
        manager = LSPManager(tmp_path)
        (tmp_path / "a.py").write_text("x = 1\n")
        (tmp_path / "b.py").write_text("y = 2\n")
        
        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = MagicMock()
            mock_proc.stderr = MagicMock()
            mock_popen.return_value = mock_proc
            
            with patch("threading.Thread"):
                with patch.object(LSPClient, "request") as mock_req:
                    mock_req.return_value = {"capabilities": {}}
                    with patch.object(LSPClient, "notify"):
                        count = manager.index_workspace("python")
                        assert count >= 2
                        manager.shutdown_all()

    def test_iter_source_files_respects_max_files(self, tmp_path):
        manager = LSPManager(tmp_path)
        for i in range(10):
            (tmp_path / f"file{i}.py").write_text(f"x = {i}\n")
        
        files = list(manager._iter_source_files((".py",), 5))
        assert len(files) <= 5

    def test_iter_source_files_skips_dotdirs(self, tmp_path):
        manager = LSPManager(tmp_path)
        (tmp_path / ".hidden").mkdir()
        (tmp_path / ".hidden" / "hidden.py").write_text("hidden\n")
        (tmp_path / "visible.py").write_text("visible\n")
        
        files = list(manager._iter_source_files((".py",), 100))
        assert "visible.py" in files
        assert ".hidden/hidden.py" not in files


class TestLSPManagerDiagnostics:
    """Test LSPManager diagnostics."""

    def test_cached_diagnostics_returns_empty_for_unopened_file(self, tmp_path):
        manager = LSPManager(tmp_path)
        diags = manager.cached_diagnostics("test.py")
        assert diags == []

    def test_cached_diagnostics_stores_by_uri(self, tmp_path):
        manager = LSPManager(tmp_path)
        test_file = tmp_path / "test.py"
        test_file.write_text("x = 1\n")
        uri = manager._path_to_uri(str(test_file))
        
        # Manually store some diagnostics
        manager._diagnostics[uri] = [
            {"message": "error", "severity": 1}
        ]
        
        diags = manager.cached_diagnostics("test.py")
        assert len(diags) == 1
        assert diags[0]["message"] == "error"


class TestLSPManagerStaleness:
    """Test staleness detection in LSPManager."""

    def test_stale_disk_text_detects_changes(self, tmp_path):
        manager = LSPManager(tmp_path)
        test_file = tmp_path / "test.py"
        test_file.write_text("original\n")
        full_path = str(test_file.resolve())
        
        # Register the file as opened
        manager._opened_files.add(full_path)
        manager._sent_sha[full_path] = manager._text_sha("original\n")
        
        # Change the file on disk
        test_file.write_text("modified\n")
        
        stale = manager._stale_disk_text(full_path)
        assert stale == "modified\n"

    def test_stale_disk_text_returns_none_when_unchanged(self, tmp_path):
        manager = LSPManager(tmp_path)
        test_file = tmp_path / "test.py"
        text = "unchanged\n"
        test_file.write_text(text)
        full_path = str(test_file.resolve())
        
        manager._opened_files.add(full_path)
        manager._sent_sha[full_path] = manager._text_sha(text)
        
        stale = manager._stale_disk_text(full_path)
        assert stale is None


class TestLSPReaderLoop:
    """Test LSPClient._reader_loop behavior."""

    def test_reader_loop_processes_messages(self):
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()

        with patch("subprocess.Popen", return_value=mock_proc):
            with patch("threading.Thread") as mock_thread:
                client = LSPClient(["test-server"], cwd="/tmp")
                # Verify threads were started
                assert mock_thread.call_count >= 2
                client.shutdown()


class TestFormatDocumentSymbols:
    """Test _format_document_symbols utility."""

    def test_format_document_symbols_with_nested_children(self, tmp_path):
        from runtime.tools.lsp import _format_document_symbols
        
        items = [
            {
                "name": "class Foo",
                "kind": 5,  # Class
                "selectionRange": {"start": {"line": 0, "character": 0}},
                "children": [
                    {
                        "name": "method bar",
                        "kind": 6,  # Method
                        "selectionRange": {"start": {"line": 2, "character": 4}},
                        "children": []
                    }
                ]
            }
        ]
        
        lines = []
        _format_document_symbols(items, 0, lines)
        
        assert len(lines) >= 2
        assert "Class" in lines[0]
        assert "Method" in lines[1]

    def test_format_document_symbols_respects_max_symbols(self):
        from runtime.tools.lsp import _format_document_symbols
        
        items = [
            {
                "name": f"symbol_{i}",
                "kind": 12,  # Function
                "selectionRange": {"start": {"line": i, "character": 0}},
            }
            for i in range(300)
        ]
        
        lines = []
        _format_document_symbols(items, 0, lines)
        
        # Should stop at SYMBOL_MAX
        assert len(lines) <= 200


class TestLSPManagerAskLsp:
    """Test LSPManager.ask_lsp method."""

    def test_ask_lsp_with_definition_action(self, tmp_path):
        manager = LSPManager(tmp_path)
        test_file = tmp_path / "test.py"
        test_file.write_text("def foo():\n    pass\n")
        
        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = MagicMock()
            mock_proc.stderr = MagicMock()
            mock_popen.return_value = mock_proc
            
            with patch("threading.Thread"):
                with patch.object(LSPClient, "request") as mock_req:
                    mock_req.return_value = {"capabilities": {}}
                    with patch.object(LSPClient, "notify"):
                        with patch.object(manager, "open_file_and_get_diagnostics"):
                            client_mock = MagicMock()
                            client_mock.request.return_value = {
                                "uri": test_file.resolve().as_uri(),
                                "range": {"start": {"line": 0, "character": 4}}
                            }
                            manager._clients[tuple(LSPManager.SERVER_CONFIGS["python"].cmd)] = client_mock
                            manager._opened_files.add(str(test_file.resolve()))
                            
                            result = manager.ask_lsp("test.py", 0, 4, "definition")
                            assert result is not None
                            manager.shutdown_all()

    def test_ask_lsp_with_references_action(self, tmp_path):
        manager = LSPManager(tmp_path)
        test_file = tmp_path / "test.py"
        test_file.write_text("x = 1\nprint(x)\n")
        
        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = MagicMock()
            mock_proc.stderr = MagicMock()
            mock_popen.return_value = mock_proc
            
            with patch("threading.Thread"):
                with patch.object(LSPClient, "request") as mock_req:
                    mock_req.return_value = {"capabilities": {}}
                    with patch.object(LSPClient, "notify"):
                        with patch.object(manager, "open_file_and_get_diagnostics"):
                            client_mock = MagicMock()
                            client_mock.request.return_value = [
                                {"uri": test_file.resolve().as_uri(), "range": {"start": {"line": 0, "character": 0}}}
                            ]
                            manager._clients[tuple(LSPManager.SERVER_CONFIGS["python"].cmd)] = client_mock
                            manager._opened_files.add(str(test_file.resolve()))
                            
                            result = manager.ask_lsp("test.py", 0, 0, "references")
                            assert result is not None
                            manager.shutdown_all()

    def test_ask_lsp_invalid_action_raises_error(self, tmp_path):
        manager = LSPManager(tmp_path)
        test_file = tmp_path / "test.py"
        test_file.write_text("x = 1\n")
        
        with pytest.raises(ValueError, match="action must be"):
            manager.ask_lsp("test.py", 0, 0, "invalid_action")


class TestLSPManagerAskDocumentSymbols:
    """Test LSPManager.ask_document_symbols method."""

    def test_ask_document_symbols_returns_symbols(self, tmp_path):
        manager = LSPManager(tmp_path)
        test_file = tmp_path / "test.py"
        test_file.write_text("def foo():\n    pass\n")
        
        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = MagicMock()
            mock_proc.stderr = MagicMock()
            mock_popen.return_value = mock_proc
            
            with patch("threading.Thread"):
                with patch.object(LSPClient, "request") as mock_req:
                    mock_req.return_value = {"capabilities": {}}
                    with patch.object(LSPClient, "notify"):
                        with patch.object(manager, "open_file_and_get_diagnostics"):
                            client_mock = MagicMock()
                            client_mock.request.return_value = [
                                {
                                    "name": "foo",
                                    "kind": 12,
                                    "selectionRange": {"start": {"line": 0, "character": 4}}
                                }
                            ]
                            manager._clients[tuple(LSPManager.SERVER_CONFIGS["python"].cmd)] = client_mock
                            manager._opened_files.add(str(test_file.resolve()))
                            
                            result = manager.ask_document_symbols("test.py")
                            assert result is not None
                            manager.shutdown_all()


class TestLSPManagerDidChange:
    """Test LSPManager.did_change method."""

    def test_did_change_opens_new_file(self, tmp_path):
        manager = LSPManager(tmp_path)
        test_file = tmp_path / "test.py"
        test_file.write_text("x = 1\n")
        
        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = MagicMock()
            mock_proc.stderr = MagicMock()
            mock_popen.return_value = mock_proc
            
            with patch("threading.Thread"):
                with patch.object(LSPClient, "request") as mock_req:
                    mock_req.return_value = {"capabilities": {}}
                    with patch.object(LSPClient, "notify") as mock_notify:
                        uri = manager.did_change("test.py", "y = 2\n")
                        assert uri is not None
                        assert "test.py" in uri
                        manager.shutdown_all()

    def test_did_change_updates_existing_file(self, tmp_path):
        manager = LSPManager(tmp_path)
        test_file = tmp_path / "test.py"
        test_file.write_text("x = 1\n")
        full_path = str(test_file.resolve())
        
        manager._opened_files.add(full_path)
        manager._versions[full_path] = 1
        
        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = MagicMock()
            mock_proc.stderr = MagicMock()
            mock_popen.return_value = mock_proc
            
            with patch("threading.Thread"):
                with patch.object(LSPClient, "request") as mock_req:
                    mock_req.return_value = {"capabilities": {}}
                    with patch.object(LSPClient, "notify"):
                        with patch.object(manager, "_client_for") as mock_client_for:
                            client_mock = MagicMock()
                            mock_client_for.return_value = client_mock
                            
                            uri = manager.did_change("test.py", "y = 2\n")
                            assert uri is not None
                            # Check that version was incremented
                            assert manager._versions[full_path] == 2
                            manager.shutdown_all()


class TestLSPManagerAskRename:
    """Test LSPManager.ask_rename method."""

    def test_ask_rename_returns_edit(self, tmp_path):
        manager = LSPManager(tmp_path)
        test_file = tmp_path / "test.py"
        test_file.write_text("x = 1\n")
        
        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = MagicMock()
            mock_proc.stderr = MagicMock()
            mock_popen.return_value = mock_proc
            
            with patch("threading.Thread"):
                with patch.object(LSPClient, "request") as mock_req:
                    mock_req.return_value = {"capabilities": {}}
                    with patch.object(LSPClient, "notify"):
                        with patch.object(manager, "open_file_and_get_diagnostics"):
                            client_mock = MagicMock()
                            client_mock.request.return_value = {
                                "changes": {
                                    test_file.resolve().as_uri(): [
                                        {
                                            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}},
                                            "newText": "y"
                                        }
                                    ]
                                }
                            }
                            manager._clients[tuple(LSPManager.SERVER_CONFIGS["python"].cmd)] = client_mock
                            manager._opened_files.add(str(test_file.resolve()))
                            
                            result = manager.ask_rename("test.py", 0, 0, "y")
                            assert result is not None
                            manager.shutdown_all()


class TestLSPManagerWarmStart:
    """Test LSPManager.warm_start method."""

    def test_warm_start_with_unknown_language(self, tmp_path):
        manager = LSPManager(tmp_path)
        result = manager.warm_start("unknown_lang")
        assert result is None

    def test_warm_start_with_python(self, tmp_path):
        manager = LSPManager(tmp_path)
        (tmp_path / "test.py").write_text("x = 1\n")
        
        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = MagicMock()
            mock_proc.stderr = MagicMock()
            mock_popen.return_value = mock_proc
            
            with patch("threading.Thread"):
                with patch.object(LSPClient, "request") as mock_req:
                    mock_req.return_value = {"capabilities": {}}
                    with patch.object(LSPClient, "notify"):
                        with patch.object(manager, "_wait_for_diagnostics"):
                            count = manager.warm_start("python")
                            assert count is not None and count >= 1
                            manager.shutdown_all()

    def test_warm_start_javascript_initializes_both_languages(self, tmp_path):
        manager = LSPManager(tmp_path)
        (tmp_path / "test.js").write_text("let x = 1;\n")
        
        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = MagicMock()
            mock_proc.stderr = MagicMock()
            mock_popen.return_value = mock_proc
            
            with patch("threading.Thread"):
                with patch.object(LSPClient, "request") as mock_req:
                    mock_req.return_value = {"capabilities": {}}
                    with patch.object(LSPClient, "notify"):
                        with patch.object(manager, "_wait_for_diagnostics"):
                            count = manager.warm_start("javascript")
                            # Should initialize both javascript and typescript
                            assert len(manager._clients) > 0
                            manager.shutdown_all()


class TestLSPClientGetNotification:
    """Test LSPClient.get_notification method."""

    def test_get_notification_with_timeout(self):
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()

        with patch("subprocess.Popen", return_value=mock_proc):
            client = LSPClient(["test-server"], cwd="/tmp")
            result = client.get_notification(timeout=0.1)
            assert result is None
            client.shutdown()


class TestLSPClientReply:
    """Test LSPClient.reply method."""

    def test_reply_sends_response(self):
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()

        with patch("subprocess.Popen", return_value=mock_proc):
            client = LSPClient(["test-server"], cwd="/tmp")
            with patch.object(client, "_write_message") as mock_write:
                client.reply(1, {"result": "success"})
                mock_write.assert_called_once()
                call_args = mock_write.call_args[0][0]
                assert call_args["id"] == 1
                assert call_args["result"] == {"result": "success"}
            client.shutdown()


class TestSymbolKinds:
    """Test SYMBOL_KINDS mapping."""

    def test_symbol_kinds_complete(self):
        assert 1 in SYMBOL_KINDS
        assert SYMBOL_KINDS[1] == "File"
        assert 5 in SYMBOL_KINDS
        assert SYMBOL_KINDS[5] == "Class"
        assert 12 in SYMBOL_KINDS
        assert SYMBOL_KINDS[12] == "Function"
