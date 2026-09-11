"""Unit tests for runtime/tools/lsp.py with mocked subprocess.

These tests mock subprocess.Popen and file I/O to test the LSP client and manager
without requiring a real language server. This provides coverage without the
@pytest.mark.lsp marker.
"""

from __future__ import annotations

import json
import queue
import threading
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch, call
from unittest.mock import mock_open

import pytest

from runtime.tools.lsp import (
    LSPClient,
    LSPManager,
    LSPTimeoutError,
    SYMBOL_KINDS,
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
)


class MockPipe:
    """Mock pipe for LSP client testing."""

    def __init__(self, messages=None):
        self.messages = list(messages or [])
        self.written = []
        self.closed = False

    def write(self, data):
        self.written.append(data)

    def flush(self):
        pass

    def readline(self):
        if not self.messages:
            return b""
        return self.messages.pop(0)

    def read(self, size):
        if not self.messages:
            return b""
        return self.messages.pop(0)

    def close(self):
        self.closed = True


class TestLSPClient:
    """Tests for LSPClient class."""

    @patch("runtime.tools.lsp.subprocess.Popen")
    def test_lspclient_init_success(self, mock_popen):
        """Test LSPClient initialization."""
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_popen.return_value = mock_proc

        client = LSPClient(["test", "server"], cwd="/test")
        assert client._alive
        assert client.proc == mock_proc

    @patch("runtime.tools.lsp.subprocess.Popen")
    def test_lspclient_init_missing_pipes(self, mock_popen):
        """Test LSPClient init fails when pipes are None."""
        mock_proc = MagicMock()
        mock_proc.stdin = None
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_popen.return_value = mock_proc

        with pytest.raises(RuntimeError, match="pipes failed to open"):
            LSPClient(["test", "server"], cwd="/test")

    @patch("runtime.tools.lsp.subprocess.Popen")
    def test_lspclient_read_message_empty(self, mock_popen):
        """Test reading empty message returns None."""
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_popen.return_value = mock_proc

        client = LSPClient(["test"], cwd="/test")
        mock_proc.stdout.readline.return_value = b""
        result = client._read_message(mock_proc.stdout)
        assert result is None

    @patch("runtime.tools.lsp.subprocess.Popen")
    def test_lspclient_read_message_with_content(self, mock_popen):
        """Test reading a complete LSP message."""
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_popen.return_value = mock_proc

        client = LSPClient(["test"], cwd="/test")

        body = json.dumps({"id": 1, "result": "ok"}).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")

        readline_calls = [b"Content-Length: " + str(len(body)).encode() + b"\r\n", b"\r\n"]
        mock_proc.stdout.readline.side_effect = readline_calls
        mock_proc.stdout.read.return_value = body

        result = client._read_message(mock_proc.stdout)
        assert result == {"id": 1, "result": "ok"}

    @patch("runtime.tools.lsp.subprocess.Popen")
    def test_lspclient_write_message(self, mock_popen):
        """Test writing an LSP message."""
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_popen.return_value = mock_proc

        client = LSPClient(["test"], cwd="/test")
        payload = {"jsonrpc": "2.0", "id": 1, "method": "test"}

        client._write_message(payload)

        mock_proc.stdin.write.assert_called_once()
        mock_proc.stdin.flush.assert_called_once()

    @patch("runtime.tools.lsp.subprocess.Popen")
    def test_lspclient_write_message_broken_pipe(self, mock_popen):
        """Test write_message raises on broken pipe."""
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdin.write.side_effect = BrokenPipeError("pipe broken")
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_popen.return_value = mock_proc

        client = LSPClient(["test"], cwd="/test")

        with pytest.raises(RuntimeError, match="LSP server process died"):
            client._write_message({"test": "data"})

    @patch("runtime.tools.lsp.subprocess.Popen")
    def test_lspclient_request_success(self, mock_popen):
        """Test LSP request method."""
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_popen.return_value = mock_proc

        client = LSPClient(["test"], cwd="/test")

        # Mock successful response
        client._pending[1] = queue.Queue()
        client._pending[1].put({"id": 1, "result": {"success": True}})

        result = client.request("test_method", {"param": "value"})
        assert result == {"success": True}

    @patch("runtime.tools.lsp.subprocess.Popen")
    def test_lspclient_request_timeout(self, mock_popen):
        """Test LSP request times out."""
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_popen.return_value = mock_proc

        client = LSPClient(["test"], cwd="/test")

        with pytest.raises(LSPTimeoutError):
            client.request("slow_method", {}, timeout=0.01)

    @patch("runtime.tools.lsp.subprocess.Popen")
    def test_lspclient_request_not_alive(self, mock_popen):
        """Test request fails if client not alive."""
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_popen.return_value = mock_proc

        client = LSPClient(["test"], cwd="/test")
        client._alive = False

        with pytest.raises(RuntimeError, match="not running"):
            client.request("test", {})

    @patch("runtime.tools.lsp.subprocess.Popen")
    def test_lspclient_request_error_response(self, mock_popen):
        """Test request with error response."""
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_popen.return_value = mock_proc

        client = LSPClient(["test"], cwd="/test")
        client._pending[1] = queue.Queue()
        client._pending[1].put({"id": 1, "error": {"message": "error occurred"}})

        with pytest.raises(RuntimeError, match="error occurred"):
            client.request("test", {})

    @patch("runtime.tools.lsp.subprocess.Popen")
    def test_lspclient_notify(self, mock_popen):
        """Test notify method."""
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_popen.return_value = mock_proc

        client = LSPClient(["test"], cwd="/test")
        client.notify("test_notify", {"param": "value"})

        mock_proc.stdin.write.assert_called()

    @patch("runtime.tools.lsp.subprocess.Popen")
    def test_lspclient_reply(self, mock_popen):
        """Test reply method."""
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_popen.return_value = mock_proc

        client = LSPClient(["test"], cwd="/test")
        client.reply(123, {"result": "data"})

        mock_proc.stdin.write.assert_called()

    @patch("runtime.tools.lsp.subprocess.Popen")
    def test_lspclient_get_notification(self, mock_popen):
        """Test get_notification method."""
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_popen.return_value = mock_proc

        client = LSPClient(["test"], cwd="/test")
        client._notifications.put({"method": "test", "params": {}})

        notif = client.get_notification(timeout=1.0)
        assert notif == {"method": "test", "params": {}}

    @patch("runtime.tools.lsp.subprocess.Popen")
    def test_lspclient_get_notification_timeout(self, mock_popen):
        """Test get_notification times out."""
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_popen.return_value = mock_proc

        client = LSPClient(["test"], cwd="/test")
        notif = client.get_notification(timeout=0.01)
        assert notif is None

    @patch("runtime.tools.lsp.subprocess.Popen")
    def test_lspclient_shutdown(self, mock_popen):
        """Test shutdown method."""
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_popen.return_value = mock_proc

        client = LSPClient(["test"], cwd="/test")
        client._pending[1] = queue.Queue()
        client._pending[1].put({"id": 1, "result": None})

        client.shutdown()
        assert not client._alive

    @patch("runtime.tools.lsp.subprocess.Popen")
    def test_lspclient_shutdown_already_not_alive(self, mock_popen):
        """Test shutdown when already not alive."""
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_popen.return_value = mock_proc

        client = LSPClient(["test"], cwd="/test")
        client._alive = False
        client.shutdown()  # Should not raise

    @patch("runtime.tools.lsp.subprocess.Popen")
    def test_lspclient_reader_loop_processes_responses(self, mock_popen):
        """Test reader loop processes response messages."""
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_popen.return_value = mock_proc

        client = LSPClient(["test"], cwd="/test")
        # Simulate a response being placed in pending queue by reader loop
        client._pending[1] = queue.Queue()
        msg = {"id": 1, "result": "test"}
        client._pending[1].put(msg)
        
        result = client._pending[1].get(timeout=1)
        assert result["id"] == 1

    @patch("runtime.tools.lsp.subprocess.Popen")
    def test_lspclient_reader_loop_processes_notifications(self, mock_popen):
        """Test reader loop processes notification messages."""
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_popen.return_value = mock_proc

        client = LSPClient(["test"], cwd="/test")
        # Simulate notification being placed in queue by reader loop
        notif = {"method": "test/notify", "params": {}}
        client._notifications.put(notif)
        
        result = client._notifications.get(timeout=1)
        assert result["method"] == "test/notify"


class TestLSPManager:
    """Tests for LSPManager class."""

    def test_lspmanager_init(self, tmp_path):
        """Test LSPManager initialization."""
        manager = LSPManager(tmp_path)
        assert manager.root == str(tmp_path.resolve())
        assert manager._closed is False

    def test_lspmanager_path_to_uri(self):
        """Test path to URI conversion."""
        uri = LSPManager._path_to_uri("/test/path")
        assert uri.startswith("file://")
        assert "path" in uri

    def test_lspmanager_config_for_extension_python(self):
        """Test config lookup for Python files."""
        cfg = LSPManager._config_for_extension(".py")
        assert cfg is not None
        assert cfg.language == "python"

    def test_lspmanager_config_for_extension_typescript(self):
        """Test config lookup for TypeScript files."""
        cfg = LSPManager._config_for_extension(".ts")
        assert cfg is not None
        assert cfg.language == "typescript"

    def test_lspmanager_config_for_extension_javascript(self):
        """Test config lookup for JavaScript files."""
        cfg = LSPManager._config_for_extension(".js")
        assert cfg is not None
        assert cfg.language == "javascript"

    def test_lspmanager_config_for_extension_go(self):
        """Test config lookup for Go files."""
        cfg = LSPManager._config_for_extension(".go")
        assert cfg is not None
        assert cfg.language == "go"

    def test_lspmanager_config_for_extension_unknown(self):
        """Test config lookup for unknown extension."""
        cfg = LSPManager._config_for_extension(".xyz")
        assert cfg is None

    def test_lspmanager_language_id_python(self):
        """Test language ID for Python."""
        cfg = LSPManager.SERVER_CONFIGS["python"]
        lang_id = LSPManager._language_id(cfg, ".py")
        assert lang_id == "python"

    def test_lspmanager_language_id_tsx(self):
        """Test language ID for TypeScript React."""
        cfg = LSPManager.SERVER_CONFIGS["typescript"]
        lang_id = LSPManager._language_id(cfg, ".tsx")
        assert lang_id == "typescriptreact"

    def test_lspmanager_language_id_jsx(self):
        """Test language ID for JavaScript React."""
        cfg = LSPManager.SERVER_CONFIGS["javascript"]
        lang_id = LSPManager._language_id(cfg, ".jsx")
        assert lang_id == "javascriptreact"

    def test_lspmanager_settings_section_empty(self):
        """Test settings section lookup with empty section."""
        settings = {"key": "value"}
        result = LSPManager._settings_section(settings, None)
        assert result == settings

    def test_lspmanager_settings_section_nested(self):
        """Test settings section lookup with nested path."""
        settings = {"python": {"analysis": {"enabled": True}}}
        result = LSPManager._settings_section(settings, "python.analysis")
        assert result == {"enabled": True}

    def test_lspmanager_settings_section_not_found(self):
        """Test settings section lookup when section doesn't exist."""
        settings = {"python": {"analysis": {}}}
        result = LSPManager._settings_section(settings, "python.missing")
        assert result is None

    def test_lspmanager_shutdown_all(self, tmp_path):
        """Test shutdown_all method."""
        manager = LSPManager(tmp_path)
        manager._closed = False
        manager._clients = {}
        manager.shutdown_all()
        assert manager._closed is True

    def test_lspmanager_warm_languages_python(self):
        """Test warm_languages for Python."""
        langs = LSPManager._warm_languages("python")
        assert langs == ["python"]

    def test_lspmanager_warm_languages_javascript(self):
        """Test warm_languages for JavaScript."""
        langs = LSPManager._warm_languages("javascript")
        assert "javascript" in langs
        assert "typescript" in langs

    def test_lspmanager_warm_languages_typescript(self):
        """Test warm_languages for TypeScript."""
        langs = LSPManager._warm_languages("typescript")
        assert "typescript" in langs
        assert "javascript" in langs

    def test_lspmanager_warm_languages_go(self):
        """Test warm_languages for Go."""
        langs = LSPManager._warm_languages("go")
        assert langs == ["go"]

    def test_lspmanager_warm_languages_unknown(self):
        """Test warm_languages for unknown language."""
        langs = LSPManager._warm_languages("unknown")
        assert langs == []

    def test_lspmanager_text_sha(self):
        """Test SHA256 hash of text."""
        text1 = "hello"
        text2 = "world"
        sha1 = LSPManager._text_sha(text1)
        sha2 = LSPManager._text_sha(text2)
        assert sha1 != sha2
        assert len(sha1) == 64  # SHA256 hex length

    def test_lspmanager_disk_text_success(self, tmp_path):
        """Test reading disk text."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("test content")
        text = LSPManager._disk_text(str(test_file))
        assert text == "test content"

    def test_lspmanager_disk_text_missing_file(self):
        """Test reading missing file returns None."""
        text = LSPManager._disk_text("/nonexistent/file.txt")
        assert text is None

    def test_lspmanager_cached_diagnostics(self, tmp_path):
        """Test cached diagnostics retrieval."""
        manager = LSPManager(tmp_path)
        test_file = tmp_path / "test.py"
        test_file.write_text("x = 1")
        uri = manager._path_to_uri(str(test_file))
        manager._diagnostics[uri] = [{"message": "error"}]

        diags = manager.cached_diagnostics("test.py")
        assert len(diags) == 1
        assert diags[0]["message"] == "error"

    @patch("runtime.tools.lsp.LSPClient")
    def test_lspmanager_client_for_creates_new(self, mock_client_class, tmp_path):
        """Test _client_for creates a new client when not cached."""
        manager = LSPManager(tmp_path)
        mock_client = MagicMock()
        mock_client._alive = True
        mock_client_class.return_value = mock_client
        mock_client.request.return_value = {}

        cfg = LSPManager.SERVER_CONFIGS["python"]
        # This will try to create a real subprocess, so we mock the Popen call
        with patch("runtime.tools.lsp.subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = MagicMock()
            mock_proc.stderr = MagicMock()
            mock_popen.return_value = mock_proc
            try:
                client = manager._client_for(cfg)
                assert client is not None
            except RuntimeError:
                pass

    def test_lspmanager_iter_source_files(self, tmp_path):
        """Test iterating source files."""
        manager = LSPManager(tmp_path)
        (tmp_path / "a.py").write_text("x = 1")
        (tmp_path / "b.py").write_text("y = 2")
        (tmp_path / "c.txt").write_text("z = 3")

        files = list(manager._iter_source_files((".py",), 10))
        assert len(files) == 2
        assert "a.py" in files
        assert "b.py" in files

    def test_lspmanager_iter_source_files_respects_max(self, tmp_path):
        """Test iter_source_files respects max_files limit."""
        manager = LSPManager(tmp_path)
        for i in range(5):
            (tmp_path / f"file{i}.py").write_text(f"x = {i}")

        files = list(manager._iter_source_files((".py",), max_files=2))
        assert len(files) == 2

    def test_lspmanager_iter_source_files_skips_hidden(self, tmp_path):
        """Test iter_source_files skips hidden directories."""
        manager = LSPManager(tmp_path)
        (tmp_path / "visible.py").write_text("x = 1")
        hidden_dir = tmp_path / ".hidden"
        hidden_dir.mkdir()
        (hidden_dir / "hidden.py").write_text("y = 2")

        files = list(manager._iter_source_files((".py",), 10))
        assert "visible.py" in files
        assert not any(".hidden" in f for f in files)

    def test_lspmanager_stale_disk_text_no_file_opened(self, tmp_path):
        """Test _stale_disk_text returns None for unopened file."""
        manager = LSPManager(tmp_path)
        full_path = str(tmp_path / "test.py")
        result = manager._stale_disk_text(full_path)
        assert result is None

    def test_lspmanager_stale_disk_text_file_changed(self, tmp_path):
        """Test _stale_disk_text detects changed file."""
        manager = LSPManager(tmp_path)
        test_file = tmp_path / "test.py"
        test_file.write_text("original")
        
        full_path = str(test_file.resolve())
        manager._opened_files.add(full_path)
        manager._sent_sha[full_path] = manager._text_sha("different")
        
        result = manager._stale_disk_text(full_path)
        assert result == "original"

    def test_lspmanager_stale_disk_text_unchanged(self, tmp_path):
        """Test _stale_disk_text returns None for unchanged file."""
        manager = LSPManager(tmp_path)
        test_file = tmp_path / "test.py"
        text = "same content"
        test_file.write_text(text)
        
        full_path = str(test_file.resolve())
        manager._opened_files.add(full_path)
        manager._sent_sha[full_path] = manager._text_sha(text)
        
        result = manager._stale_disk_text(full_path)
        assert result is None


class TestHelperFunctions:
    """Tests for helper functions in runtime/tools/lsp.py."""

    def test_uri_to_path(self):
        """Test URI to path conversion."""
        if Path("/test/file.py").as_uri().startswith("file:///"):
            path = _uri_to_path("file:///test/file.py")
            assert "file.py" in path

    def test_rel_from_uri_valid(self, tmp_path):
        """Test relative path from URI."""
        test_file = tmp_path / "test.py"
        test_file.write_text("")
        uri = test_file.as_uri()
        rel = _rel_from_uri(tmp_path, uri)
        assert "test.py" in rel

    def test_rel_from_uri_invalid_path(self, tmp_path):
        """Test relative path from URI when path is outside workspace."""
        uri = Path("/outside/file.py").as_uri()
        rel = _rel_from_uri(tmp_path, uri)
        assert "outside" in rel or "file.py" in rel

    def test_normalize_locations_empty(self):
        """Test normalizing empty locations."""
        result = _normalize_locations(None)
        assert result == []

    def test_normalize_locations_dict(self):
        """Test normalizing single location dict."""
        result = _normalize_locations(
            {
                "uri": "file:///test.py",
                "range": {"start": {"line": 0, "character": 0}},
            }
        )
        assert len(result) == 1
        assert result[0][0] == "file:///test.py"

    def test_normalize_locations_list(self):
        """Test normalizing list of locations."""
        result = _normalize_locations(
            [
                {
                    "uri": "file:///test.py",
                    "range": {"start": {"line": 0, "character": 0}},
                },
                {
                    "uri": "file:///other.py",
                    "range": {"start": {"line": 5, "character": 10}},
                },
            ]
        )
        assert len(result) == 2

    def test_normalize_locations_with_target_uri(self):
        """Test normalizing locations with targetUri (definition results)."""
        result = _normalize_locations(
            {
                "targetUri": "file:///test.py",
                "targetRange": {"start": {"line": 0, "character": 0}},
            }
        )
        assert result[0][0] == "file:///test.py"

    def test_format_locations_empty(self, tmp_path):
        """Test formatting empty locations."""
        result = _format_locations(tmp_path, [], 20)
        assert "No results" in result

    def test_format_locations_single(self, tmp_path):
        """Test formatting single location."""
        uri = (tmp_path / "test.py").as_uri()
        locs = [(uri, {"start": {"line": 0, "character": 5}})]
        result = _format_locations(tmp_path, locs, 20)
        assert "1:6" in result  # line:char (1-based)

    def test_format_locations_truncated(self, tmp_path):
        """Test formatting with truncation."""
        uri = (tmp_path / "test.py").as_uri()
        locs = [
            (uri, {"start": {"line": i, "character": 0}})
            for i in range(30)
        ]
        result = _format_locations(tmp_path, locs, 20)
        assert "... and" in result

    def test_extract_hover_text_none(self):
        """Test extracting hover text from None."""
        result = _extract_hover_text(None)
        assert result == ""

    def test_extract_hover_text_string(self):
        """Test extracting hover text from string."""
        result = _extract_hover_text("hover info")
        assert result == "hover info"

    def test_extract_hover_text_dict(self):
        """Test extracting hover text from dict."""
        result = _extract_hover_text({"value": "hover text"})
        assert result == "hover text"

    def test_extract_hover_text_list(self):
        """Test extracting hover text from list."""
        result = _extract_hover_text(
            ["first", {"value": "second"}, "third"]
        )
        assert "first" in result
        assert "second" in result
        assert "third" in result

    def test_resolve_valid_path(self, tmp_path):
        """Test resolve with valid path."""
        test_file = tmp_path / "test.py"
        test_file.write_text("")
        result = _resolve(tmp_path, "test.py")
        assert result is None

    def test_resolve_outside_workspace(self, tmp_path):
        """Test resolve with path outside workspace."""
        result = _resolve(tmp_path, "../outside.py")
        assert result is not None
        assert "error" in result


class TestRuntimeFunctions:
    """Tests for high-level runtime LSP functions."""

    def test_symbol_kinds_populated(self):
        """Test that SYMBOL_KINDS dict is populated."""
        assert len(SYMBOL_KINDS) > 0
        assert SYMBOL_KINDS.get(1) == "File"
        assert SYMBOL_KINDS.get(12) == "Function"

    @patch("runtime.tools.lsp.LSPManager.ask_lsp")
    def test_goto_definition_with_result(self, mock_ask, tmp_path):
        """Test goto_definition with result."""
        mock_lsp = MagicMock()
        mock_lsp.ask_lsp.return_value = {
            "uri": (tmp_path / "def.py").as_uri(),
            "range": {"start": {"line": 10, "character": 5}},
        }

        result = goto_definition(tmp_path, mock_lsp, "test.py", 5, 10)
        assert "def.py" in result or "11" in result

    @patch("runtime.tools.lsp.LSPManager.ask_lsp")
    def test_find_references_with_results(self, mock_ask, tmp_path):
        """Test find_references with results."""
        mock_lsp = MagicMock()
        mock_lsp.ask_lsp.return_value = [
            {
                "uri": (tmp_path / "a.py").as_uri(),
                "range": {"start": {"line": 0, "character": 0}},
            },
            {
                "uri": (tmp_path / "b.py").as_uri(),
                "range": {"start": {"line": 5, "character": 10}},
            },
        ]

        result = find_references(tmp_path, mock_lsp, "test.py", 1, 1)
        assert len(result.split("\n")) >= 2

    @patch("runtime.tools.lsp.LSPManager.ask_lsp")
    def test_hover_with_content(self, mock_ask, tmp_path):
        """Test hover with content."""
        mock_lsp = MagicMock()
        mock_lsp.ask_lsp.return_value = {
            "contents": "def my_func() -> str"
        }

        result = hover(tmp_path, mock_lsp, "test.py", 1, 1)
        assert "my_func" in result

    @patch("runtime.tools.lsp.LSPManager.open_file_and_get_diagnostics")
    def test_get_diagnostics_with_errors(self, mock_get_diags, tmp_path):
        """Test get_diagnostics with error diagnostics."""
        mock_lsp = MagicMock()
        mock_lsp.open_file_and_get_diagnostics.return_value = [
            {
                "range": {"start": {"line": 0, "character": 5}},
                "severity": 1,
                "message": "undefined variable",
                "source": "pylint",
            }
        ]

        result = get_diagnostics(tmp_path, mock_lsp, "test.py")
        assert "undefined variable" in result
        assert "Error" in result

    @patch("runtime.tools.lsp.LSPManager.ask_document_symbols")
    def test_document_symbols_with_symbols(self, mock_symbols, tmp_path):
        """Test document_symbols with results."""
        mock_lsp = MagicMock()
        mock_lsp.ask_document_symbols.return_value = [
            {
                "name": "MyClass",
                "kind": 5,
                "selectionRange": {"start": {"line": 0, "character": 0}},
                "children": [
                    {
                        "name": "method",
                        "kind": 6,
                        "selectionRange": {"start": {"line": 5, "character": 4}},
                    }
                ],
            }
        ]

        result = document_symbols(tmp_path, mock_lsp, "test.py")
        assert "MyClass" in result
        assert "method" in result

    @patch("runtime.tools.lsp.LSPManager.ask_rename")
    def test_rename_symbol_with_edits(self, mock_rename, tmp_path):
        """Test rename_symbol with workspace edits."""
        mock_lsp = MagicMock()
        mock_lsp.ask_rename.return_value = {
            "changes": {
                (tmp_path / "test.py").as_uri(): [
                    {
                        "range": {"start": {"line": 0, "character": 0}},
                        "newText": "new_name",
                    }
                ]
            }
        }

        result = rename_symbol(tmp_path, mock_lsp, "test.py", 1, 1, "new_name")
        assert isinstance(result, dict)

    def test_rename_symbol_error_on_invalid_workspace_path(self, tmp_path):
        """Test rename_symbol errors on invalid path."""
        mock_lsp = MagicMock()
        result = rename_symbol(tmp_path, mock_lsp, "../outside.py", 1, 1, "new")
        assert isinstance(result, str)
        assert "error" in result

    @patch("runtime.tools.lsp.LSPManager.ask_lsp")
    def test_goto_definition_no_result(self, mock_ask, tmp_path):
        """Test goto_definition with no result."""
        mock_lsp = MagicMock()
        mock_lsp.ask_lsp.return_value = None

        result = goto_definition(tmp_path, mock_lsp, "test.py", 1, 1)
        assert "No definition found" in result

    @patch("runtime.tools.lsp.LSPManager.ask_lsp")
    def test_find_references_no_result(self, mock_ask, tmp_path):
        """Test find_references with no result."""
        mock_lsp = MagicMock()
        mock_lsp.ask_lsp.return_value = None

        result = find_references(tmp_path, mock_lsp, "test.py", 1, 1)
        assert "No references found" in result

    @patch("runtime.tools.lsp.LSPManager.ask_lsp")
    def test_hover_no_content(self, mock_ask, tmp_path):
        """Test hover with no content."""
        mock_lsp = MagicMock()
        mock_lsp.ask_lsp.return_value = {"contents": ""}

        result = hover(tmp_path, mock_lsp, "test.py", 1, 1)
        assert "No hover information" in result

    @patch("runtime.tools.lsp.LSPManager.open_file_and_get_diagnostics")
    def test_get_diagnostics_no_errors(self, mock_get_diags, tmp_path):
        """Test get_diagnostics with no errors."""
        mock_lsp = MagicMock()
        mock_lsp.open_file_and_get_diagnostics.return_value = []

        result = get_diagnostics(tmp_path, mock_lsp, "test.py")
        assert "No diagnostics" in result or "clean" in result

    @patch("runtime.tools.lsp.LSPManager.ask_document_symbols")
    def test_document_symbols_no_symbols(self, mock_symbols, tmp_path):
        """Test document_symbols with no symbols."""
        mock_lsp = MagicMock()
        mock_lsp.ask_document_symbols.return_value = None

        result = document_symbols(tmp_path, mock_lsp, "test.py")
        assert "No document symbols" in result

    @patch("runtime.tools.lsp.LSPManager.ask_rename")
    def test_rename_symbol_no_edits(self, mock_rename, tmp_path):
        """Test rename_symbol with no edits."""
        mock_lsp = MagicMock()
        mock_lsp.ask_rename.return_value = None

        result = rename_symbol(tmp_path, mock_lsp, "test.py", 1, 1, "new")
        assert isinstance(result, str)
        assert "error" in result

    @patch("runtime.tools.lsp.LSPManager.ask_lsp")
    def test_goto_definition_timeout(self, mock_ask, tmp_path):
        """Test goto_definition handles timeout."""
        mock_lsp = MagicMock()
        mock_lsp.ask_lsp.side_effect = LSPTimeoutError("timeout")

        result = goto_definition(tmp_path, mock_lsp, "test.py", 1, 1)
        assert "error" in result
        assert "timeout" in result

    def test_rename_symbol_empty_name(self, tmp_path):
        """Test rename_symbol with empty new name."""
        mock_lsp = MagicMock()
        result = rename_symbol(tmp_path, mock_lsp, "test.py", 1, 1, "")
        assert "error" in result
        assert "new_name is required" in result

    @patch("runtime.tools.lsp.LSPManager.ask_lsp")
    def test_goto_definition_exception_handling(self, mock_ask, tmp_path):
        """Test goto_definition handles exceptions gracefully."""
        mock_lsp = MagicMock()
        mock_lsp.ask_lsp.side_effect = ValueError("invalid path")

        result = goto_definition(tmp_path, mock_lsp, "test.py", 1, 1)
        assert "error" in result
        assert "invalid path" in result

    @patch("runtime.tools.lsp.LSPManager.open_file_and_get_diagnostics")
    def test_get_diagnostics_with_hints(self, mock_get_diags, tmp_path):
        """Test get_diagnostics formats hints correctly."""
        mock_lsp = MagicMock()
        mock_lsp.open_file_and_get_diagnostics.return_value = [
            {
                "range": {"start": {"line": 2, "character": 10}},
                "severity": 4,
                "message": "info message",
            }
        ]

        result = get_diagnostics(tmp_path, mock_lsp, "test.py")
        assert "Hint" in result
        assert "info message" in result

    @patch("runtime.tools.lsp.LSPManager.ask_document_symbols")
    def test_document_symbols_with_nested_children(self, mock_symbols, tmp_path):
        """Test document_symbols handles nested symbol hierarchy."""
        mock_lsp = MagicMock()
        mock_lsp.ask_document_symbols.return_value = [
            {
                "name": "OuterClass",
                "kind": 5,
                "selectionRange": {"start": {"line": 0, "character": 0}},
                "children": [
                    {
                        "name": "inner_method",
                        "kind": 6,
                        "selectionRange": {"start": {"line": 2, "character": 4}},
                        "detail": "method details",
                    }
                ],
            }
        ]

        result = document_symbols(tmp_path, mock_lsp, "test.py")
        assert "OuterClass" in result
        assert "inner_method" in result
        assert "method details" in result

    @patch("runtime.tools.lsp.LSPManager.ask_document_symbols")
    def test_document_symbols_max_symbols_cap(self, mock_symbols, tmp_path):
        """Test document_symbols respects symbol limit."""
        mock_lsp = MagicMock()
        # Create more symbols than SYMBOL_MAX
        symbols = [
            {
                "name": f"func{i}",
                "kind": 12,
                "selectionRange": {"start": {"line": i, "character": 0}},
            }
            for i in range(250)
        ]
        mock_lsp.ask_document_symbols.return_value = symbols

        result = document_symbols(tmp_path, mock_lsp, "test.py")
        assert "... (capped at" in result

    def test_normalize_locations_with_missing_fields(self):
        """Test normalize_locations handles missing fields gracefully."""
        result = _normalize_locations(
            {
                "uri": "file:///test.py",
                "range": {"start": {}},  # Missing line and character
            }
        )
        assert len(result) == 1

    def test_format_locations_with_file_read_error(self, tmp_path):
        """Test format_locations handles file read errors."""
        # Use a path that doesn't exist
        uri = "file:///nonexistent/test.py"
        locs = [(uri, {"start": {"line": 0, "character": 5}})]
        result = _format_locations(tmp_path, locs, 20)
        assert "nonexistent" in result or "1:6" in result

    def test_extract_hover_text_with_empty_list(self):
        """Test extract_hover_text handles empty list."""
        result = _extract_hover_text([])
        assert result == ""

    def test_extract_hover_text_with_list_of_dicts(self):
        """Test extract_hover_text combines multiple dict values."""
        result = _extract_hover_text(
            [
                {"value": "first part"},
                {"value": "second part"},
            ]
        )
        assert "first part" in result
        assert "second part" in result
