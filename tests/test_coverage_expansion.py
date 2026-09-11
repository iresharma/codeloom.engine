"""Expansion tests for coverage targets: pkg, browser, envinfo, depwhy, scan, shell, skills, tokens."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from tests.fakes import FakeApprover

from runtime.mcp.tokens import apply_tokens, load_tokens, save_token, tokens_path
from runtime.tools.browser import browser_console, browser_network, browser_open, browser_screenshot
from runtime.tools.depwhy import dep_why
from runtime.tools.envinfo import runtime_info
from runtime.tools.pkg import pkg_info
from runtime.tools.scan import todo_scan
from runtime.tools.shell import run_command
from tools.shell import run_command as tool_run_command
from tools.skills import activate_skill, read_skill, _siblings


# ============================================================================
# Tests for runtime/tools/pkg.py
# ============================================================================

def test_pkg_info_pypi(monkeypatch):
    """Test PyPI package lookup."""
    monkeypatch.setattr(
        "runtime.tools.pkg.get_json",
        lambda url, **k: (
            {
                "info": {
                    "name": "requests",
                    "version": "2.32.0",
                    "summary": "HTTP for Humans",
                    "home_page": "https://requests.readthedocs.io",
                    "license": "Apache-2.0",
                    "yanked": False,
                }
            },
            "",
        ),
    )
    result = pkg_info("pypi", "requests")
    assert "name: requests" in result
    assert "2.32.0" in result
    assert "HTTP for Humans" in result


def test_pkg_info_npm(monkeypatch):
    """Test NPM package lookup."""
    monkeypatch.setattr(
        "runtime.tools.pkg.get_json",
        lambda url, **k: (
            {
                "dist-tags": {"latest": "1.0.0"},
                "versions": {
                    "1.0.0": {
                        "name": "lodash",
                        "description": "Utility library",
                    }
                },
            },
            "",
        ),
    )
    result = pkg_info("npm", "lodash")
    assert "name: lodash" in result or "Utility library" in result


def test_pkg_info_invalid_ecosystem():
    """Test invalid ecosystem error."""
    result = pkg_info("bogus", "package")
    assert "error:" in result
    assert "pypi" in result


def test_pkg_info_no_name():
    """Test missing package name error."""
    result = pkg_info("pypi", "")
    assert "error:" in result
    assert "required" in result


# ============================================================================
# Tests for runtime/tools/browser.py
# ============================================================================

@pytest.mark.asyncio
async def test_browser_open_unavailable():
    """Test browser open when playwright unavailable."""
    with patch("runtime.tools.browser._ensure", return_value=None):
        result = await browser_open("https://example.com")
        assert "unavailable" in result


@pytest.mark.asyncio
async def test_browser_console_unavailable():
    """Test console when playwright unavailable."""
    with patch("runtime.tools.browser._ensure", return_value=None):
        result = await browser_console()
        assert "unavailable" in result


@pytest.mark.asyncio
async def test_browser_network_unavailable():
    """Test network when playwright unavailable."""
    with patch("runtime.tools.browser._ensure", return_value=None):
        result = await browser_network()
        assert "unavailable" in result


@pytest.mark.asyncio
async def test_browser_screenshot_unavailable(tmp_path):
    """Test screenshot when playwright unavailable."""
    with patch("runtime.tools.browser._ensure", return_value=None):
        result = await browser_screenshot(tmp_path)
        assert "unavailable" in result


# ============================================================================
# Tests for runtime/tools/envinfo.py
# ============================================================================

def test_runtime_info_found_tools(monkeypatch):
    """Test runtime_info with found tools."""
    def mock_which(tool):
        if tool == "python3":
            return "/usr/bin/python3"
        if tool == "git":
            return "/usr/bin/git"
        return None

    def mock_run(*args, **kwargs):
        result = Mock()
        if "python" in str(args):
            result.stdout = "Python 3.11.0"
            result.stderr = ""
        elif "git" in str(args):
            result.stdout = "git version 2.40.0"
            result.stderr = ""
        return result

    monkeypatch.setattr("runtime.tools.envinfo.shutil.which", mock_which)
    monkeypatch.setattr("runtime.tools.envinfo.subprocess.run", mock_run)
    
    result = runtime_info()
    assert "python:" in result.lower() or "git:" in result.lower()


def test_runtime_info_no_tools(monkeypatch):
    """Test runtime_info when no tools found."""
    monkeypatch.setattr("runtime.tools.envinfo.shutil.which", lambda x: None)
    result = runtime_info()
    assert "(not found)" in result


# ============================================================================
# Tests for runtime/tools/depwhy.py
# ============================================================================

def test_dep_why_invalid_ecosystem():
    """Test dep_why with invalid ecosystem."""
    result = dep_why(Path.cwd(), "bogus", "package")
    assert "error:" in result
    assert "ecosystem" in result


def test_dep_why_no_name():
    """Test dep_why with no package name."""
    result = dep_why(Path.cwd(), "npm", "")
    assert "error:" in result
    assert "required" in result


def test_dep_why_binary_not_found(monkeypatch):
    """Test dep_why when binary not found."""
    monkeypatch.setattr("runtime.tools.depwhy.shutil.which", lambda x: None)
    result = dep_why(Path.cwd(), "npm", "lodash")
    assert "error:" in result
    assert "not installed" in result


# ============================================================================
# Tests for runtime/tools/scan.py
# ============================================================================

def test_todo_scan_default(tmp_path):
    """Test todo_scan with default pattern."""
    test_file = tmp_path / "test.py"
    test_file.write_text("# TODO: fix this\ndef foo():\n    pass")
    
    result = todo_scan(tmp_path)
    assert "TODO" in result
    assert "fix this" in result


def test_todo_scan_invalid_pattern(tmp_path):
    """Test todo_scan with invalid regex pattern."""
    result = todo_scan(tmp_path, pattern="[invalid")
    assert "error:" in result


def test_todo_scan_custom_pattern(tmp_path):
    """Test todo_scan with custom pattern."""
    test_file = tmp_path / "test.py"
    test_file.write_text("# CUSTOM: something\n# TODO: other")
    
    result = todo_scan(tmp_path, pattern="CUSTOM")
    assert "CUSTOM" in result


def test_todo_scan_limit(tmp_path):
    """Test todo_scan respects limit."""
    for i in range(100):
        f = tmp_path / f"test{i}.py"
        f.write_text(f"# TODO item {i}")
    
    result = todo_scan(tmp_path, limit=5)
    lines = result.strip().split("\n")
    assert len(lines) <= 6  # 5 items + truncation marker


def test_todo_scan_nonexistent_path(tmp_path):
    """Test todo_scan with nonexistent path."""
    result = todo_scan(tmp_path, path="nonexistent")
    assert "error:" in result


# ============================================================================
# Tests for runtime/tools/shell.py (via tools/shell.py)
# ============================================================================

@pytest.mark.asyncio
async def test_shell_run_command_basic(tmp_path):
    """Test basic shell command execution."""
    result = await run_command(tmp_path, "echo hello", approval="never")
    assert result.exit_code == 0
    assert "hello" in result.stdout


@pytest.mark.asyncio
async def test_shell_run_command_empty_command(tmp_path):
    """Test that empty command raises error."""
    with pytest.raises(ValueError):
        await run_command(tmp_path, "", approval="never")


@pytest.mark.asyncio
async def test_shell_run_command_sudo_denied(tmp_path):
    """Test that sudo commands are denied."""
    with pytest.raises(RuntimeError, match="sudo"):
        await run_command(tmp_path, "sudo ls", approval="always")


@pytest.mark.asyncio
async def test_shell_run_command_approval_prompt(tmp_path):
    """Test approval callback on dangerous command."""
    approver = FakeApprover("yes")
    result = await run_command(
        tmp_path,
        "echo test | cat",
        approval="auto",
        approve=approver,
    )
    assert result.exit_code == 0


@pytest.mark.asyncio
async def test_shell_run_command_denied_via_callback(tmp_path):
    """Test approval denied via callback."""
    async def deny_approval(question: str, kind: str) -> bool:
        return False
    
    with pytest.raises(RuntimeError, match="not approved"):
        await run_command(
            tmp_path,
            "echo test | cat",
            approval="auto",
            approve=deny_approval,
        )


# ============================================================================
# Tests for tools/shell.py wrapper
# ============================================================================

@pytest.mark.asyncio
async def test_tool_run_command_with_config(tmp_path):
    """Test tool wrapper for run_command."""
    ctx = Mock()
    ctx.workspace = tmp_path
    ctx.config = Mock(exec_approval="never", exec_file_limit_mb=2048, exec_timeout_s=120)
    ctx.ask_user = None
    ctx.on_output = None
    ctx.on_proc = None
    
    result = await tool_run_command(ctx, "echo test")
    assert "exit code: 0" in result


# ============================================================================
# Tests for tools/skills.py
# ============================================================================

def test_activate_skill_unavailable():
    """Test activate_skill when skills unavailable."""
    ctx = Mock()
    ctx.activate_skill = None
    ctx.skills = None
    
    result = activate_skill(ctx, "anyskill")
    assert "not available" in result


def test_activate_skill_unknown():
    """Test activate_skill with unknown skill."""
    ctx = Mock()
    ctx.activate_skill = None
    ctx.skills = {}
    
    result = activate_skill(ctx, "unknown")
    assert "unknown skill" in result


def test_read_skill_unavailable():
    """Test read_skill when skills unavailable."""
    ctx = Mock()
    ctx.skills = None
    
    result = read_skill(ctx, "anyskill", "file.txt")
    assert "not available" in result


def test_read_skill_unknown():
    """Test read_skill with unknown skill."""
    ctx = Mock()
    ctx.skills = {}
    
    result = read_skill(ctx, "unknown", "file.txt")
    assert "unknown skill" in result


def test_siblings_error():
    """Test _siblings with error."""
    skill = Mock()
    skill.directory = Mock()
    skill.directory.iterdir = Mock(side_effect=OSError("permission denied"))
    
    result = _siblings(skill)
    assert result == []


# ============================================================================
# Tests for runtime/mcp/tokens.py
# ============================================================================

def test_tokens_path(tmp_path):
    """Test tokens_path returns correct path."""
    path = tokens_path(tmp_path)
    assert str(path).endswith(".engine/mcp-tokens.json")


def test_load_tokens_nonexistent(tmp_path):
    """Test load_tokens with nonexistent file."""
    result = load_tokens(tmp_path)
    assert result == {}


def test_load_tokens_invalid_json(tmp_path):
    """Test load_tokens with invalid JSON."""
    tokens_file = tmp_path / ".engine" / "mcp-tokens.json"
    tokens_file.parent.mkdir(parents=True, exist_ok=True)
    tokens_file.write_text("invalid json {")
    
    result = load_tokens(tmp_path)
    assert result == {}


def test_load_tokens_not_dict(tmp_path):
    """Test load_tokens when JSON is not a dict."""
    tokens_file = tmp_path / ".engine" / "mcp-tokens.json"
    tokens_file.parent.mkdir(parents=True, exist_ok=True)
    tokens_file.write_text("[]")
    
    result = load_tokens(tmp_path)
    assert result == {}


def test_load_tokens_valid(tmp_path):
    """Test load_tokens with valid data."""
    tokens_file = tmp_path / ".engine" / "mcp-tokens.json"
    tokens_file.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "server1": {
            "token": "abc123",
            "token_env": "ENV_VAR"
        }
    }
    tokens_file.write_text(json.dumps(data))
    
    result = load_tokens(tmp_path)
    assert result["server1"]["token"] == "abc123"
    assert result["server1"]["tokenEnv"] == "ENV_VAR"


def test_save_token(tmp_path):
    """Test save_token creates and saves token."""
    result = save_token(tmp_path, "myserver", "mytoken", "MY_ENV")
    assert result.exists()
    
    data = json.loads(result.read_text())
    assert data["myserver"]["token"] == "mytoken"
    assert data["myserver"]["tokenEnv"] == "MY_ENV"


def test_save_token_permissions(tmp_path):
    """Test save_token sets correct permissions."""
    result = save_token(tmp_path, "server", "token", "ENV")
    mode = oct(result.stat().st_mode)[-3:]
    assert mode == "600"


def test_apply_tokens():
    """Test apply_tokens applies environment variables."""
    cfg1 = Mock()
    cfg1.name = "server1"
    cfg1.token_env = "TOKEN_ENV"
    cfg1.env = {}
    
    tokens = {
        "server1": {
            "token": "secret123",
            "tokenEnv": "TOKEN_ENV"
        }
    }
    
    apply_tokens([cfg1], tokens)
    assert cfg1.env["TOKEN_ENV"] == "secret123"


def test_apply_tokens_no_match():
    """Test apply_tokens skips non-matching servers."""
    cfg1 = Mock()
    cfg1.name = "server1"
    cfg1.token_env = "TOKEN_ENV"
    cfg1.env = {}
    
    tokens = {
        "other": {
            "token": "secret",
            "tokenEnv": "TOKEN_ENV"
        }
    }
    
    apply_tokens([cfg1], tokens)
    assert cfg1.env == {}


def test_apply_tokens_no_env_name():
    """Test apply_tokens skips when no env name."""
    cfg1 = Mock()
    cfg1.name = "server1"
    cfg1.token_env = ""
    cfg1.env = {}
    
    tokens = {
        "server1": {
            "token": "secret",
            "tokenEnv": ""
        }
    }
    
    apply_tokens([cfg1], tokens)
    assert cfg1.env == {}
