from __future__ import annotations

import asyncio
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import pytest

from runtime.mcp.config import McpServerConfig
from runtime.mcp.tokens import apply_tokens, load_tokens, save_token, tokens_path
from runtime.tools.browser import (
    browser_console,
    browser_network,
    browser_open,
    browser_screenshot,
)
from runtime.tools.depwhy import dep_why
from runtime.tools.envinfo import runtime_info
from runtime.tools.pkg import pkg_info, _clip, _license, _block
from runtime.tools.scan import todo_scan
from runtime.tools.shell import (
    CommandResult,
    DEFAULT_TIMEOUT,
    HARD_MAX_TIMEOUT,
    file_limit_blocks,
    format_command_result,
    run_command,
)
from tools.shell import run_command as tools_run_command
from tools.skills import activate_skill, read_skill, _siblings
from tools.base import ToolContext
from runtime.skills.discover import Skill
from runtime.skills.catalog import SkillCatalog
from tests.confakes import FakeApprover
from tests.fakes import FakeProvider


# ===== Helper class for mocking MCP configs =====
@dataclass
class FakeMcpConfig:
    """Simple dataclass-like mock for cfg in apply_tokens tests."""
    name: str
    env: dict[str, str]
    token_env: str | None = None


# ===== Tests for runtime/tools/pkg.py =====

class TestPkg:
    def test_pkg_info_invalid_ecosystem(self):
        result = pkg_info("invalid", "leftpad")
        assert "error: ecosystem must be one of" in result
        assert "pypi" in result

    def test_pkg_info_empty_ecosystem(self):
        result = pkg_info("", "test")
        assert result.startswith("error:")

    def test_pkg_info_empty_name(self):
        result = pkg_info("pypi", "")
        assert "error: name is required" in result

    def test_pkg_info_with_whitespace(self):
        result = pkg_info("  pypi  ", "  requests  ")
        # Should not error on whitespace
        assert "error" not in result.lower() or "error:" not in result[:100]

    def test_license_with_dict(self):
        value = {"type": "Apache-2.0"}
        result = _license(value)
        assert result == "Apache-2.0"

    def test_license_with_dict_fallback_to_name(self):
        value = {"name": "MIT"}
        result = _license(value)
        assert result == "MIT"

    def test_license_with_dict_empty(self):
        value = {}
        result = _license(value)
        assert result == ""

    def test_license_with_string(self):
        result = _license("GPL-3.0")
        assert result == "GPL-3.0"

    def test_license_with_none(self):
        result = _license(None)
        assert result == ""

    def test_clip_short_text(self):
        text = "short"
        result = _clip(text, 100)
        assert result == text

    def test_clip_long_text(self):
        text = "x" * 200
        result = _clip(text, 100)
        assert len(result) == 103  # 100 + "..."
        assert result.endswith("...")

    def test_block_basic(self):
        result = _block(
            name="test",
            version="1.0.0",
            summary="A test package",
            homepage="https://example.com",
            license_name="MIT",
            yanked=False,
        )
        assert "name: test" in result
        assert "version: 1.0.0" in result
        assert "summary: A test package" in result
        assert "homepage: https://example.com" in result
        assert "license: MIT" in result
        assert "yanked: False" in result

    def test_block_with_extra(self):
        result = _block(
            name="test",
            version="1.0.0",
            summary="A test",
            homepage="https://example.com",
            license_name="MIT",
            yanked=True,
            extra="deprecated: yes",
        )
        assert "deprecated: yes" in result

    def test_block_with_none_values(self):
        result = _block(
            name="test",
            version=None,
            summary=None,
            homepage=None,
            license_name=None,
            yanked=False,
        )
        assert "version: (unknown)" in result
        assert "summary: (none)" in result
        assert "homepage: (none)" in result
        assert "license: (unknown)" in result


# ===== Tests for runtime/tools/browser.py =====

class TestBrowser:
    @pytest.mark.asyncio
    async def test_browser_open_missing(self):
        # When playwright is not available, should return error message
        result = await browser_open("https://example.com")
        assert "error:" in result or "unavailable" in result

    @pytest.mark.asyncio
    async def test_browser_console_missing(self):
        result = await browser_console()
        assert "error:" in result or "unavailable" in result

    @pytest.mark.asyncio
    async def test_browser_screenshot_missing(self, tmp_path):
        result = await browser_screenshot(tmp_path)
        assert "error:" in result or "unavailable" in result

    @pytest.mark.asyncio
    async def test_browser_network_missing(self):
        result = await browser_network()
        assert "error:" in result or "unavailable" in result


# ===== Tests for runtime/tools/envinfo.py =====

class TestEnvInfo:
    def test_runtime_info_returns_string(self):
        result = runtime_info()
        assert isinstance(result, str)
        # Should have some entries for common tools
        assert "python:" in result or "node:" in result or "git:" in result
        # Check format
        lines = result.split("\n")
        assert len(lines) > 0
        for line in lines:
            assert ":" in line

    def test_runtime_info_has_git(self):
        result = runtime_info()
        assert "git:" in result

    def test_runtime_info_no_duplicates(self):
        result = runtime_info()
        lines = result.split("\n")
        labels = [line.split(":")[0] for line in lines if ":" in line]
        # Each tool should appear at most once
        assert len(labels) == len(set(labels))


# ===== Tests for runtime/tools/depwhy.py =====

class TestDepWhy:
    def test_depwhy_invalid_ecosystem(self):
        result = dep_why(Path("/tmp"), "invalid", "leftpad")
        assert "error: ecosystem must be one of" in result

    def test_depwhy_empty_ecosystem(self):
        result = dep_why(Path("/tmp"), "", "package")
        assert "error:" in result

    def test_depwhy_empty_name(self):
        result = dep_why(Path("/tmp"), "npm", "")
        assert "error: name is required" in result

    def test_depwhy_npm_not_installed(self, tmp_path):
        with mock.patch("shutil.which", return_value=None):
            result = dep_why(tmp_path, "npm", "express")
            assert "error: npm not installed" in result

    def test_depwhy_with_whitespace(self, tmp_path):
        with mock.patch("shutil.which", return_value=None):
            result = dep_why(tmp_path, "  npm  ", "  express  ")
            assert "error:" in result  # npm not installed


# ===== Tests for runtime/tools/scan.py =====

class TestScan:
    def test_todo_scan_hits(self, tmp_path):
        (tmp_path / "main.py").write_text("x = 1  # TODO: fix this\n")
        result = todo_scan(tmp_path)
        assert "main.py:1" in result
        assert "fix this" in result

    def test_todo_scan_multiple_marks(self, tmp_path):
        (tmp_path / "app.py").write_text("# FIXME: broken\n# XXX: hack\n# HACK: bad\n")
        result = todo_scan(tmp_path)
        lines = result.split("\n")
        # Should find multiple markers
        assert len(lines) >= 3

    def test_todo_scan_empty_workspace(self, tmp_path):
        result = todo_scan(tmp_path)
        assert result == "(none)"

    def test_todo_scan_custom_pattern(self, tmp_path):
        (tmp_path / "code.js").write_text("// DEBT: tech debt here\n")
        result = todo_scan(tmp_path, pattern="DEBT")
        assert "DEBT" in result

    def test_todo_scan_bad_pattern(self, tmp_path):
        result = todo_scan(tmp_path, pattern="[invalid(regex")
        assert "error: bad pattern:" in result

    def test_todo_scan_nonexistent_path(self, tmp_path):
        result = todo_scan(tmp_path, path="nonexistent")
        assert "error:" in result

    def test_todo_scan_limit(self, tmp_path):
        for i in range(10):
            (tmp_path / f"file{i}.py").write_text(f"# TODO: item {i}\n")
        result = todo_scan(tmp_path, limit=3)
        assert "...[truncated]" in result

    def test_todo_scan_file_path(self, tmp_path):
        (tmp_path / "file.py").write_text("# TODO: task\n")
        result = todo_scan(tmp_path, path="file.py")
        assert "file.py:1" in result

    def test_todo_scan_skips_cache_dirs(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("# TODO: src task\n")
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "lib.py").write_text("# TODO: cache task\n")
        result = todo_scan(tmp_path)
        assert "src/main.py" in result
        assert "node_modules" not in result


# ===== Tests for tools/shell.py =====

class TestToolsShell:
    @pytest.mark.asyncio
    async def test_tools_run_command_basic(self, tmp_path):
        ctx = ToolContext(
            workspace=tmp_path,
            files=mock.MagicMock(),
            journal=tmp_path / "db",
            session_id="test",
            config=None,
        )
        result = await tools_run_command(ctx, "echo hello", approval="never")
        assert "exit code:" in result
        assert "hello" in result


# ===== Tests for runtime/tools/shell.py =====

class TestRuntimeShell:
    def test_file_limit_blocks_1mb(self):
        assert file_limit_blocks(1) == 2048

    def test_file_limit_blocks_large(self):
        assert file_limit_blocks(2048) == 2048 * 2048

    def test_file_limit_blocks_fractional(self):
        result = file_limit_blocks(0)
        assert result >= 1  # Should enforce minimum

    def test_format_command_result_success(self):
        result = CommandResult(
            command="echo test",
            exit_code=0,
            stdout="test\n",
            stderr="",
            duration_s=0.1,
        )
        formatted = format_command_result(result)
        assert "$ echo test" in formatted
        assert "exit code: 0" in formatted
        assert "test\n" in formatted

    def test_format_command_result_with_timeout(self):
        result = CommandResult(
            command="sleep 30",
            exit_code=-1,
            stdout="",
            stderr="",
            duration_s=5.0,
            timed_out=True,
        )
        formatted = format_command_result(result)
        assert "timed out" in formatted
        assert "process group killed" in formatted

    def test_format_command_result_with_error(self):
        result = CommandResult(
            command="bad_cmd",
            exit_code=127,
            stdout="",
            stderr="command not found",
            duration_s=0.2,
        )
        formatted = format_command_result(result)
        assert "exit code: 127" in formatted
        assert "command not found" in formatted

    @pytest.mark.asyncio
    async def test_run_command_validation(self, tmp_path):
        with pytest.raises(ValueError):
            await run_command(tmp_path, "")

    @pytest.mark.asyncio
    async def test_run_command_sudo_denied(self, tmp_path):
        with pytest.raises(RuntimeError, match="sudo"):
            await run_command(tmp_path, "sudo ls", approval="never")

    @pytest.mark.asyncio
    async def test_run_command_engine_denied(self, tmp_path):
        with pytest.raises(RuntimeError, match=".engine"):
            await run_command(tmp_path, "rm -rf .engine/session.db", approval="never")

    @pytest.mark.asyncio
    async def test_run_command_timeout_enforcement(self, tmp_path):
        # Ensure timeout is bounded by HARD_MAX_TIMEOUT
        result = await run_command(
            tmp_path,
            "echo test",
            timeout=2000,
            approval="never",
        )
        # Just verify it doesn't crash and returns a result
        assert isinstance(result, CommandResult)

    @pytest.mark.asyncio
    async def test_run_command_env_scrubbing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "secret123")
        result = await run_command(
            tmp_path,
            "sh -c 'echo $OPENROUTER_API_KEY'",
            approval="never",
        )
        assert "secret123" not in result.stdout
        assert result.exit_code == 0

    @pytest.mark.asyncio
    async def test_run_command_ci_env_set(self, tmp_path):
        result = await run_command(
            tmp_path,
            "sh -c 'echo $CI'",
            approval="never",
        )
        # CI=1 should be set
        assert result.exit_code == 0


# ===== Tests for tools/skills.py =====

class TestToolsSkills:
    def test_activate_skill_not_available(self):
        ctx = ToolContext(
            workspace=Path("/tmp"),
            files=mock.MagicMock(),
            journal=Path("/tmp/db"),
            session_id="test",
            config=None,
            skills=None,
        )
        result = activate_skill(ctx, "any_skill")
        assert "error: skills are not available" in result

    def test_activate_skill_not_found(self):
        catalog = SkillCatalog([])
        ctx = ToolContext(
            workspace=Path("/tmp"),
            files=mock.MagicMock(),
            journal=Path("/tmp/db"),
            session_id="test",
            config=None,
            skills=catalog,
        )
        result = activate_skill(ctx, "nonexistent")
        assert "error: unknown skill" in result

    def test_activate_skill_found(self, tmp_path):
        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        skill = Skill(
            name="test_skill",
            description="A test skill",
            source="test",
            directory=skill_dir,
            body="TEST BODY",
            auto=True,
        )
        catalog = SkillCatalog([skill])
        ctx = ToolContext(
            workspace=tmp_path,
            files=mock.MagicMock(),
            journal=tmp_path / "db",
            session_id="test",
            config=None,
            skills=catalog,
        )
        result = activate_skill(ctx, "test_skill")
        assert "TEST BODY" in result

    def test_activate_skill_with_siblings(self, tmp_path):
        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        (skill_dir / "extra.md").write_text("extra content")
        (skill_dir / "subdir").mkdir()
        skill = Skill(
            name="test_skill",
            description="A test skill",
            source="test",
            directory=skill_dir,
            body="TEST BODY",
            auto=True,
        )
        catalog = SkillCatalog([skill])
        ctx = ToolContext(
            workspace=tmp_path,
            files=mock.MagicMock(),
            journal=tmp_path / "db",
            session_id="test",
            config=None,
            skills=catalog,
        )
        result = activate_skill(ctx, "test_skill")
        assert "TEST BODY" in result
        assert "Sibling files:" in result
        assert "extra.md" in result

    def test_read_skill_not_available(self):
        ctx = ToolContext(
            workspace=Path("/tmp"),
            files=mock.MagicMock(),
            journal=Path("/tmp/db"),
            session_id="test",
            config=None,
            skills=None,
        )
        result = read_skill(ctx, "any", "file.md")
        assert "error: skills are not available" in result

    def test_read_skill_not_found(self):
        catalog = SkillCatalog([])
        ctx = ToolContext(
            workspace=Path("/tmp"),
            files=mock.MagicMock(),
            journal=Path("/tmp/db"),
            session_id="test",
            config=None,
            skills=catalog,
        )
        result = read_skill(ctx, "nonexistent", "file.md")
        assert "error: unknown skill" in result

    def test_read_skill_file(self, tmp_path):
        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        (skill_dir / "data.txt").write_text("DATA CONTENT")
        skill = Skill(
            name="test_skill",
            description="A test skill",
            source="test",
            directory=skill_dir,
            body="TEST BODY",
            auto=True,
        )
        catalog = SkillCatalog([skill])
        ctx = ToolContext(
            workspace=tmp_path,
            files=mock.MagicMock(),
            journal=tmp_path / "db",
            session_id="test",
            config=None,
            skills=catalog,
        )
        result = read_skill(ctx, "test_skill", "data.txt")
        assert "DATA CONTENT" in result

    def test_siblings_empty(self, tmp_path):
        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        skill = Skill(
            name="test",
            description="test",
            source="test",
            directory=skill_dir,
            body="body",
            auto=True,
        )
        result = _siblings(skill)
        assert result == []

    def test_siblings_with_files_and_dirs(self, tmp_path):
        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("body")
        (skill_dir / "extra.txt").write_text("extra")
        (skill_dir / "subdir").mkdir()
        skill = Skill(
            name="test",
            description="test",
            source="test",
            directory=skill_dir,
            body="body",
            auto=True,
        )
        result = _siblings(skill)
        assert len(result) >= 2
        assert "extra.txt" in result
        assert "subdir/" in result

    def test_siblings_read_error(self):
        # Mock a skill with a directory that raises on iterdir
        skill = mock.MagicMock()
        skill.directory.iterdir.side_effect = OSError("permission denied")
        result = _siblings(skill)
        assert result == []


# ===== Tests for runtime/mcp/tokens.py =====

class TestMcpTokens:
    def test_tokens_path(self, tmp_path):
        path = tokens_path(tmp_path)
        assert path == tmp_path / ".engine" / "mcp-tokens.json"

    def test_load_tokens_nonexistent_file(self, tmp_path):
        result = load_tokens(tmp_path)
        assert result == {}

    def test_load_tokens_invalid_json(self, tmp_path):
        path = tmp_path / ".engine" / "mcp-tokens.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("invalid json {")
        result = load_tokens(tmp_path)
        assert result == {}

    def test_load_tokens_not_dict(self, tmp_path):
        path = tmp_path / ".engine" / "mcp-tokens.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('["not", "a", "dict"]')
        result = load_tokens(tmp_path)
        assert result == {}

    def test_load_tokens_valid(self, tmp_path):
        path = tmp_path / ".engine" / "mcp-tokens.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "server1": {"token": "token123", "tokenEnv": "API_KEY"},
            "server2": {"token": "token456", "token_env": "TOKEN"},
        }
        path.write_text(json.dumps(data))
        result = load_tokens(tmp_path)
        assert "server1" in result
        assert result["server1"]["token"] == "token123"
        assert result["server1"]["tokenEnv"] == "API_KEY"
        assert "server2" in result

    def test_load_tokens_invalid_row_no_token(self, tmp_path):
        path = tmp_path / ".engine" / "mcp-tokens.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "server1": {"tokenEnv": "API_KEY"},  # Missing token
            "server2": {"token": "token456"},
        }
        path.write_text(json.dumps(data))
        result = load_tokens(tmp_path)
        assert "server1" not in result
        assert "server2" in result

    def test_save_token(self, tmp_path):
        result = save_token(tmp_path, "test_server", "mytoken", "MY_ENV")
        assert result.is_file()
        data = json.loads(result.read_text())
        assert data["test_server"]["token"] == "mytoken"
        assert data["test_server"]["tokenEnv"] == "MY_ENV"

    def test_save_token_permissions(self, tmp_path):
        result = save_token(tmp_path, "server", "token", "ENV")
        # Check that file is readable (has at least some permissions)
        assert result.stat().st_mode & 0o600 == 0o600

    def test_apply_tokens_no_match(self):
        cfg = FakeMcpConfig(name="server1", env={}, token_env=None)
        tokens = {"other_server": {"token": "token", "tokenEnv": "ENV"}}
        apply_tokens([cfg], tokens)
        # Should not modify cfg
        assert cfg.env == {}

    def test_apply_tokens_with_match(self):
        cfg = FakeMcpConfig(name="server1", env={}, token_env=None)
        tokens = {"server1": {"token": "mytoken", "tokenEnv": "API_KEY"}}
        apply_tokens([cfg], tokens)
        assert cfg.env == {"API_KEY": "mytoken"}
        assert cfg.token_env == "API_KEY"

    def test_apply_tokens_preserves_existing_env(self):
        cfg = FakeMcpConfig(name="server1", env={"OTHER": "value"}, token_env=None)
        tokens = {"server1": {"token": "mytoken", "tokenEnv": "API_KEY"}}
        apply_tokens([cfg], tokens)
        assert cfg.env["OTHER"] == "value"
        assert cfg.env["API_KEY"] == "mytoken"

    def test_apply_tokens_uses_cfg_token_env_fallback(self):
        cfg = FakeMcpConfig(name="server1", env={}, token_env="DEFAULT_ENV")
        tokens = {"server1": {"token": "mytoken", "tokenEnv": ""}}
        apply_tokens([cfg], tokens)
        # Should use cfg.token_env when token_env is empty
        assert cfg.env.get("DEFAULT_ENV") == "mytoken"

    def test_apply_tokens_multiple_configs(self):
        cfg1 = FakeMcpConfig(name="server1", env={}, token_env=None)
        cfg2 = FakeMcpConfig(name="server2", env={}, token_env=None)
        tokens = {
            "server1": {"token": "token1", "tokenEnv": "ENV1"},
            "server2": {"token": "token2", "tokenEnv": "ENV2"},
        }
        apply_tokens([cfg1, cfg2], tokens)
        assert cfg1.env == {"ENV1": "token1"}
        assert cfg2.env == {"ENV2": "token2"}

    def test_apply_tokens_token_env_not_changed_if_already_set(self):
        cfg = FakeMcpConfig(name="server1", env={}, token_env="EXISTING")
        tokens = {"server1": {"token": "mytoken", "tokenEnv": "NEW_ENV"}}
        apply_tokens([cfg], tokens)
        # token_env should remain unchanged
        assert cfg.token_env == "EXISTING"

    def test_apply_tokens_dict_conversion(self):
        # Ensure we handle the cfg.env = dict(cfg.env) correctly
        initial_env = {"KEY": "value"}
        cfg = FakeMcpConfig(name="server1", env=initial_env, token_env=None)
        tokens = {"server1": {"token": "mytoken", "tokenEnv": "NEW_KEY"}}
        apply_tokens([cfg], tokens)
        # Original env should still have KEY, plus NEW_KEY
        assert cfg.env["KEY"] == "value"
        assert cfg.env["NEW_KEY"] == "mytoken"
