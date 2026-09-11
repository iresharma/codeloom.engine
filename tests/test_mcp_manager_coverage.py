"""Coverage for runtime/mcp/manager.py"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from runtime.mcp.config import McpServerConfig
from runtime.mcp.manager import (
    McpManager,
    McpServerState,
    extract_auth_url,
    is_auth_error,
)


def test_extract_auth_url_found():
    url = extract_auth_url("error", "https://example.com/auth")
    assert url == "https://example.com/auth"


def test_extract_auth_url_multiple_chunks():
    url = extract_auth_url("error:", "login at https://example.com/login please", "more text")
    assert url == "https://example.com/login"


def test_extract_auth_url_with_punctuation():
    url = extract_auth_url("go to https://example.com/auth).")
    assert url == "https://example.com/auth"


def test_extract_auth_url_not_found():
    url = extract_auth_url("error", "no url here")
    assert url == ""


def test_is_auth_error_unauthorized():
    assert is_auth_error("error: unauthorized")


def test_is_auth_error_401():
    assert is_auth_error("HTTP 401 Unauthorized")


def test_is_auth_error_login_required():
    assert is_auth_error("login required")


def test_is_auth_error_invalid_token():
    assert is_auth_error("invalid_token")


def test_is_auth_error_not_auth():
    assert not is_auth_error("connection timeout")


def test_mcp_server_state_init():
    config = McpServerConfig(name="test", command="test-cmd")
    state = McpServerState(config=config)
    assert state.status == "starting"
    assert state.error == ""
    assert state.session is None
    assert state.tool_count == 0
    assert not state.ever_ready


async def test_mcp_manager_init(tmp_path):
    manager = McpManager(tmp_path)
    assert manager.workspace == tmp_path
    assert manager.backoff_s == (2.0, 4.0, 8.0)
    assert manager.cool_s == 30.0
    assert manager.servers == {}


async def test_mcp_manager_custom_backoff(tmp_path):
    manager = McpManager(tmp_path, backoff_s=(1.0, 2.0), cool_s=60.0)
    assert manager.backoff_s == (1.0, 2.0)
    assert manager.cool_s == 60.0


async def test_mcp_manager_rows(tmp_path):
    manager = McpManager(tmp_path)
    rows = manager.rows()
    assert isinstance(rows, list)


async def test_mcp_manager_approval_setting(tmp_path):
    manager = McpManager(tmp_path, approval="never")
    assert manager.approval == "never"


async def test_mcp_manager_callbacks(tmp_path):
    called = {"update": False, "auth": False, "warn": False}
    
    def on_update(*args):
        called["update"] = True
    
    def on_auth(*args):
        called["auth"] = True
    
    def on_warning(*args):
        called["warn"] = True
    
    manager = McpManager(
        tmp_path,
        on_update=on_update,
        on_auth=on_auth,
        on_warning=on_warning,
    )
    assert manager.on_update == on_update
    assert manager.on_auth == on_auth
    assert manager.on_warning == on_warning


async def test_mcp_manager_default_connect(tmp_path):
    with patch("runtime.mcp.manager.connect_stdio") as mock_connect:
        manager = McpManager(tmp_path)
        # _default_connect should be called if connect not provided
        assert manager._connect is not None


async def test_mcp_manager_custom_connect(tmp_path):
    async def custom_connect(cfg):
        return "custom"
    
    manager = McpManager(tmp_path, connect=custom_connect)
    assert manager._connect == custom_connect


def test_mcp_manager_ask_user(tmp_path):
    def ask_user(*args):
        return True
    
    manager = McpManager(tmp_path, ask_user=ask_user)
    assert manager.ask_user == ask_user


async def test_mcp_manager_custom_sleep(tmp_path):
    async def custom_sleep(delay):
        pass
    
    manager = McpManager(tmp_path, sleep=custom_sleep)
    assert manager._sleep == custom_sleep


def test_mcp_manager_custom_clock(tmp_path):
    import time
    
    def custom_clock():
        return time.time()
    
    manager = McpManager(tmp_path, clock=custom_clock)
    assert manager._clock == custom_clock


def test_mcp_server_state_wire_names():
    config = McpServerConfig(name="test", command="test-cmd")
    state = McpServerState(config=config)
    state.wire_names = ["tool1", "tool2"]
    assert len(state.wire_names) == 2


def test_mcp_server_state_advertised():
    config = McpServerConfig(name="test", command="test-cmd")
    state = McpServerState(config=config)
    state.advertised.add("tool1")
    assert "tool1" in state.advertised


def test_mcp_server_state_remote_tools():
    config = McpServerConfig(name="test", command="test-cmd")
    state = McpServerState(config=config)
    state.remote_tools = [{"name": "tool1"}]
    assert len(state.remote_tools) == 1
