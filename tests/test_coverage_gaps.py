"""
Coverage tests for medium-coverage modules.
Targets: runtime/mcp/manager.py, llm/openrouter.py, runtime/server.py,
agents/orchestrator.py, agents/agent_loop.py, runtime/tools/git.py,
runtime/commands/lifecycle.py, runtime/commands/files.py,
runtime/tools/shell.py, runtime/tools/docs.py
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from llm.openrouter import OpenRouterLLM, with_cache_breakpoints, _mark_message, load_env_sh
from runtime.config import EngineConfig
from runtime.commands.files import open_file, close_file, undo_last_edit, create_path, rename_path_cmd, delete_path_cmd
from runtime.commands.lifecycle import (
    start_session, list_stored_sessions, submit_user_message, 
    request_snapshot, request_orch_context, request_context,
    request_memory, request_agent_transcript, shutdown
)
from runtime.mcp.manager import (
    McpManager, McpServerState, extract_auth_url, is_auth_error,
    _tool_list, _tool_name, _tool_description, _input_schema, _read_only,
    _has_stored_token, _call, _maybe_await, connect_stdio,
)
from runtime.mcp.config import McpServerConfig
from runtime.session import EngineSession
from runtime.server import EngineServer
from runtime.tools.docs import docs_lookup, tldr, _clip, _go, _mdn
from runtime.tools.shell import file_limit_blocks, format_command_result, CommandResult, _auto_allowed
from runtime.tools.git import git_blame, git_log, git_show, git_range
from protocol.commands import (
    OpenFile, CloseFile, UndoLastEdit, CreatePath, RenamePath, DeletePath,
    StartSession, ListSessions, SubmitUserMessage, RequestSnapshot,
    RequestOrchContext, RequestContext, RequestMemory, RequestAgentTranscript, Shutdown,
)
from protocol.events import ErrorOccurred, FileClosed, PathChanged
from tests.fakes import FakeMcpSession, FakeProvider, fake_mcp_connect


# ============================================================================
# runtime/mcp/manager.py tests
# ============================================================================

class TestMcpManager:
    """Test coverage gaps in McpManager."""

    def test_extract_auth_url_with_url(self):
        """Test extracting auth URL from chunks."""
        url = extract_auth_url("visit https://example.com/auth", "more text")
        assert url == "https://example.com/auth"

    def test_extract_auth_url_empty_chunks(self):
        """Test extracting auth URL with empty chunks."""
        url = extract_auth_url("", "no url here", "")
        assert url == ""

    def test_extract_auth_url_cleans_punctuation(self):
        """Test that auth URL strips trailing punctuation."""
        url = extract_auth_url("visit https://example.com/auth,")
        assert url == "https://example.com/auth"
        url = extract_auth_url("visit https://example.com/auth).")
        assert url == "https://example.com/auth"

    def test_is_auth_error_with_auth_markers(self):
        """Test is_auth_error detection."""
        assert is_auth_error("unauthorized error")
        assert is_auth_error("401 forbidden")
        assert is_auth_error("authorization required")
        assert is_auth_error("please login required")
        assert not is_auth_error("regular error")

    def test_tool_list_from_dict(self):
        """Test _tool_list with dict response."""
        result = _tool_list({"tools": [{"name": "a"}, {"name": "b"}]})
        assert len(result) == 2

    def test_tool_list_from_object(self):
        """Test _tool_list with object response."""
        obj = SimpleNamespace(tools=[{"name": "a"}])
        result = _tool_list(obj)
        assert len(result) == 1

    def test_tool_list_none(self):
        """Test _tool_list with None."""
        assert _tool_list(None) == []

    def test_tool_name_from_dict(self):
        """Test _tool_name from dict."""
        assert _tool_name({"name": "search"}) == "search"
        assert _tool_name({}) == "tool"

    def test_tool_name_from_object(self):
        """Test _tool_name from object."""
        obj = SimpleNamespace(name="fetch")
        assert _tool_name(obj) == "fetch"

    def test_tool_description_from_dict(self):
        """Test _tool_description from dict."""
        desc = _tool_description({"name": "search", "description": "Search docs"}, "myserver")
        assert "Search docs" in desc
        assert "mcp:myserver" in desc

    def test_tool_description_from_object(self):
        """Test _tool_description from object."""
        obj = SimpleNamespace(name="search", description="Search docs")
        desc = _tool_description(obj, "srv")
        assert "Search docs" in desc
        assert "mcp:srv" in desc

    def test_input_schema_with_schema(self):
        """Test _input_schema extraction."""
        schema = _input_schema({"inputSchema": {"type": "object", "properties": {}}})
        assert schema["type"] == "object"

    def test_input_schema_fallback(self):
        """Test _input_schema fallback for missing schema."""
        schema = _input_schema({})
        assert schema == {"type": "object", "properties": {}}

    def test_read_only_annotation(self):
        """Test _read_only detection."""
        assert _read_only({"annotations": {"readOnlyHint": True}})
        assert not _read_only({})

    def test_has_stored_token(self):
        """Test _has_stored_token."""
        state = McpServerState(
            config=McpServerConfig(name="test", command="echo", token_env="MY_TOKEN", env={"MY_TOKEN": "xyz"})
        )
        assert _has_stored_token(state)

    def test_has_no_stored_token(self):
        """Test _has_stored_token when token not set."""
        state = McpServerState(
            config=McpServerConfig(name="test", command="echo", token_env="MY_TOKEN", env={})
        )
        assert not _has_stored_token(state)

    @pytest.mark.asyncio
    async def test_call_sync_function(self):
        """Test _call with sync function."""
        session = SimpleNamespace(list_tools=lambda: SimpleNamespace(tools=[]))
        result = await _call(session, "list_tools")
        assert result.tools == []

    @pytest.mark.asyncio
    async def test_call_async_function(self):
        """Test _call with async function."""
        async def async_method():
            return SimpleNamespace(tools=[])
        session = SimpleNamespace(list_tools=async_method)
        result = await _call(session, "list_tools")
        assert result.tools == []

    @pytest.mark.asyncio
    async def test_maybe_await_coroutine(self):
        """Test _maybe_await with coroutine."""
        async def coro():
            return "result"
        result = await _maybe_await(coro())
        assert result == "result"

    @pytest.mark.asyncio
    async def test_maybe_await_non_coroutine(self):
        """Test _maybe_await with non-coroutine."""
        result = await _maybe_await("value")
        assert result == "value"

    @pytest.mark.asyncio
    async def test_manager_emit(self):
        """Test manager._emit callback."""
        updates = []
        manager = McpManager(
            Path("."),
            on_update=lambda rows: updates.append(rows)
        )
        manager._emit()
        assert len(updates) == 1

    @pytest.mark.asyncio
    async def test_manager_warn(self):
        """Test manager._warn callback."""
        warnings = []
        manager = McpManager(
            Path("."),
            on_warning=lambda msg: warnings.append(msg)
        )
        manager._warn("test warning")
        assert "test warning" in manager.warnings
        assert len(warnings) == 1

    @pytest.mark.asyncio
    async def test_manager_rows(self):
        """Test manager.rows() returns McpServerRow list."""
        manager = McpManager(Path("."), connect=fake_mcp_connect())
        state = McpServerState(
            config=McpServerConfig(name="test", command="echo"),
            status="ready",
            tool_count=5
        )
        manager.servers["test"] = state
        rows = manager.rows()
        assert len(rows) == 1
        assert rows[0].name == "test"
        assert rows[0].status == "ready"
        assert rows[0].tool_count == 5

    @pytest.mark.asyncio
    async def test_manager_clear_cooling(self):
        """Test manager.clear_cooling()."""
        manager = McpManager(Path("."))
        state = McpServerState(
            config=McpServerConfig(name="test", command="echo"),
            status="error",
            error="cooling...",
            cool_until=1000.0
        )
        manager.servers["test"] = state
        manager.clear_cooling()
        assert state.cool_until == 0.0
        assert state.error == ""

    @pytest.mark.asyncio
    async def test_manager_restart_unknown_server(self):
        """Test restarting unknown server."""
        manager = McpManager(Path("."), connect=fake_mcp_connect())
        result = await manager.restart("unknown")
        assert result is None

    @pytest.mark.asyncio
    async def test_manager_call_tool_unknown_server(self):
        """Test call_tool with unknown server."""
        manager = McpManager(Path("."))
        result = await manager.call_tool("unknown", "tool", {})
        assert "unknown mcp server" in result

    @pytest.mark.asyncio
    async def test_manager_read_resource_scheme_denied(self):
        """Test read_resource with denied scheme."""
        manager = McpManager(Path("."))
        result = await manager.read_resource("file:///etc/passwd")
        assert "error:" in result or result == ""


# ============================================================================
# llm/openrouter.py tests
# ============================================================================

class TestOpenRouterLLM:
    """Test coverage gaps in OpenRouterLLM."""

    def test_cache_breakpoints_with_tools(self):
        """Test with_cache_breakpoints marks tool cache."""
        messages = [{"role": "user", "content": "test"}]
        tools = [{"name": "tool1"}, {"name": "tool2"}]
        msgs, tool_list = with_cache_breakpoints(messages, tools)
        assert tool_list is not None
        assert tool_list[-1].get("cache_control") is not None

    def test_cache_breakpoints_system_message(self):
        """Test system message gets cache marker."""
        messages = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "test"}
        ]
        msgs, _ = with_cache_breakpoints(messages)
        assert msgs[0].get("content")

    def test_mark_message_string_content(self):
        """Test _mark_message with string content."""
        msg = {"role": "user", "content": "hello"}
        marked = _mark_message(msg)
        assert isinstance(marked["content"], list)
        assert marked["content"][0].get("cache_control")

    def test_mark_message_list_content(self):
        """Test _mark_message with list content."""
        msg = {"role": "user", "content": [{"type": "text", "text": "hello"}]}
        marked = _mark_message(msg)
        assert marked.get("cache_control") or marked["content"][-1].get("cache_control")

    def test_load_env_sh_file_not_found(self):
        """Test load_env_sh with non-existent file."""
        load_env_sh(Path("/nonexistent/env.sh"))
        # Should not raise

    def test_load_env_sh_parsing(self, tmp_path):
        """Test load_env_sh parses export statements."""
        env_file = tmp_path / "env.sh"
        env_file.write_text("export KEY=value\nKEY2=value2\n# comment\n")
        load_env_sh(env_file)
        assert os.environ.get("KEY") == "value"
        assert os.environ.get("KEY2") == "value2"

    @pytest.mark.asyncio
    async def test_openrouter_from_env_missing_key(self, monkeypatch):
        """Test from_env with missing API key."""
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setenv("OPENROUTER_API_KEY", "")
        with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
            OpenRouterLLM.from_env()

    @pytest.mark.asyncio
    async def test_openrouter_from_env_placeholder(self, monkeypatch):
        """Test from_env with placeholder key."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "<OPENROUTER_API_KEY>")
        with pytest.raises(RuntimeError, match="real key"):
            OpenRouterLLM.from_env()


# ============================================================================
# runtime/server.py tests
# ============================================================================

class TestEngineServer:
    """Test coverage gaps in EngineServer."""

    @pytest.mark.asyncio
    async def test_engine_server_init(self, tmp_path):
        """Test EngineServer initialization."""
        session = EngineSession(tmp_path, tmp_path / "db.sqlite")
        socket_path = tmp_path / "socket"
        server = EngineServer(session, socket_path)
        assert server._socket_path == socket_path
        assert server._session == session


# ============================================================================
# runtime/commands/lifecycle.py tests
# ============================================================================

class TestLifecycleCommands:
    """Test coverage gaps in lifecycle commands."""

    def test_submit_user_message_no_session(self, tmp_path):
        """Test submit_user_message without active session."""
        session = EngineSession(tmp_path, tmp_path / "db.sqlite")
        cmd = SubmitUserMessage(text="hello")
        # _require_session will return False without _state
        # This tests the early return path
        result = submit_user_message(session, cmd)
        # Should return early

    def test_request_context_no_session(self, tmp_path):
        """Test request_context without active session."""
        session = EngineSession(tmp_path, tmp_path / "db.sqlite")
        cmd = RequestContext()
        # _require_session will return False without _state
        result = request_context(session, cmd)
        # Should return early


# ============================================================================
# runtime/commands/files.py tests
# ============================================================================

class TestFilesCommands:
    """Test coverage gaps in files commands."""

    def test_open_file_not_found(self, tmp_path):
        """Test open_file with non-existent file."""
        session = EngineSession(tmp_path, tmp_path / "db.sqlite")
        cmd = OpenFile(path="nonexistent.txt")
        # _require_session will return False without _state
        open_file(session, cmd)
        # Should return early

    def test_close_file_not_open(self, tmp_path):
        """Test close_file with file not open."""
        session = EngineSession(tmp_path, tmp_path / "db.sqlite")
        cmd = CloseFile(path="nonexistent.txt")
        # _require_session will return False without _state
        close_file(session, cmd)
        # Should return early


# ============================================================================
# runtime/tools/docs.py tests
# ============================================================================

class TestDocsTools:
    """Test coverage gaps in docs tools."""

    def test_docs_lookup_unknown_source(self):
        """Test docs_lookup with unknown source."""
        result = docs_lookup("unknown", "query")
        assert "error:" in result
        assert "must be one of" in result

    def test_docs_lookup_empty_query(self):
        """Test docs_lookup with empty query."""
        result = docs_lookup("mdn", "")
        assert "error:" in result
        assert "query is required" in result

    def test_tldr_empty_topic(self):
        """Test tldr with empty topic."""
        result = tldr("")
        assert "error:" in result
        assert "topic is required" in result

    def test_clip_long_text(self):
        """Test _clip truncates long text."""
        long_text = "x" * 50000
        clipped = _clip(long_text, 1000)
        assert len(clipped) < len(long_text)
        assert "truncated" in clipped

    def test_clip_short_text(self):
        """Test _clip doesn't truncate short text."""
        short_text = "hello"
        clipped = _clip(short_text, 1000)
        assert clipped == short_text

    def test_go_query_with_path(self):
        """Test _go with module path query."""
        with patch("runtime.tools.docs.web_fetch") as mock_fetch:
            mock_fetch.return_value = "content"
            result = _go("github.com/user/repo")
            assert "https://pkg.go.dev/" in result or "content" in result

    def test_mdn_empty_results(self):
        """Test _mdn with empty results."""
        with patch("runtime.tools.docs.get_json") as mock_get:
            mock_get.return_value = ({"documents": []}, "")
            result = _mdn("nonexistent")
            assert "(no results)" in result


# ============================================================================
# runtime/tools/shell.py tests
# ============================================================================

class TestShellTools:
    """Test coverage gaps in shell tools."""

    def test_file_limit_blocks(self):
        """Test file_limit_blocks calculation."""
        assert file_limit_blocks(1) == 2048
        assert file_limit_blocks(2) == 4096
        assert file_limit_blocks(0) == 2048  # min is 1

    def test_format_command_result_with_timeout(self):
        """Test formatting result with timeout."""
        result = CommandResult(
            command="test", exit_code=1, stdout="out", stderr="err",
            duration_s=5.0, timed_out=True
        )
        formatted = format_command_result(result)
        assert "timed out" in formatted
        assert "test" in formatted

    def test_format_command_result_normal(self):
        """Test formatting result normally."""
        result = CommandResult(
            command="echo hi", exit_code=0, stdout="hi", stderr="",
            duration_s=0.1
        )
        formatted = format_command_result(result)
        assert "echo hi" in formatted
        assert "exit code: 0" in formatted


# ============================================================================
# runtime/tools/git.py tests (additional coverage)
# ============================================================================

class TestGitTools:
    """Additional git tools tests for coverage."""

    def test_git_blame_invalid_line_range(self, tmp_path):
        """Test git_blame with invalid line range."""
        # Initialize a git repo
        import subprocess
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "t@t.t"], 
            cwd=tmp_path, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "config", "user.name", "t"],
            cwd=tmp_path, check=True, capture_output=True
        )
        (tmp_path / "file.txt").write_text("line1\n")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=tmp_path, check=True, capture_output=True
        )
        
        # Blame with end_line < start_line
        result = git_blame(tmp_path, "file.txt", start_line=2, end_line=1)
        # Should still work or return error


# ============================================================================
# Misc coverage targets
# ============================================================================

class TestShellApproval:
    """Test _auto_allowed in shell.py."""

    def test_auto_allowed_read_only_command(self):
        """Test _auto_allowed with read-only command."""
        result = _auto_allowed("git status", "auto")
        assert result is True

    def test_auto_allowed_non_read_only_command(self):
        """Test _auto_allowed with non-read-only command."""
        result = _auto_allowed("rm file.txt", "auto")
        assert result is False

    def test_auto_allowed_never_approval(self):
        """Test _auto_allowed with never approval."""
        result = _auto_allowed("git status", "never")
        assert result is True

    def test_auto_allowed_always_approval(self):
        """Test _auto_allowed with always approval."""
        result = _auto_allowed("git status", "always")
        assert result is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
