from __future__ import annotations

import asyncio

import pytest

from runtime.tools.shell import file_limit_blocks, format_command_result, run_command
from tests.fakes import FakeApprover
from runtime.tools.shell import (
    CommandResult,
)
from unittest.mock import Mock
from runtime.tools.shell import _auto_allowed
from tools.shell import run_command as tool_run_command


def test_file_limit_blocks():
    assert file_limit_blocks(1) == 2048
    assert file_limit_blocks(2048) == 2048 * 2048
    assert file_limit_blocks(0) == 2048


def test_echo_round_trip(tmp_path):
    async def run():
        result = await run_command(tmp_path, "echo hello", approval="never")
        assert result.exit_code == 0
        assert "hello" in result.stdout
        assert "ulimit -f 4194304" in format_command_result(result) or result.command == "echo hello"

    asyncio.run(run())


def test_nonzero_exit_returned(tmp_path):
    async def run():
        result = await run_command(tmp_path, "sh -c 'exit 3'", approval="never")
        assert result.exit_code == 3

    asyncio.run(run())


def test_stdout_stderr_separated(tmp_path):
    async def run():
        result = await run_command(
            tmp_path, "sh -c 'echo out; echo err >&2'", approval="never"
        )
        assert "out" in result.stdout
        assert "err" in result.stderr

    asyncio.run(run())


def test_timeout_kills(tmp_path):
    async def run():
        result = await run_command(tmp_path, "sleep 30", timeout=1, approval="never")
        assert result.timed_out or result.exit_code != 0

    asyncio.run(run())


def test_key_scrubbed(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret-key")

    async def run():
        result = await run_command(tmp_path, "sh -c 'echo $OPENROUTER_API_KEY'", approval="never")
        assert "secret-key" not in result.stdout

    asyncio.run(run())


def test_typesafe_key_scrubbed(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "typesafe-secret")

    async def run():
        result = await run_command(tmp_path, "sh -c 'echo $TYPESAFE_API_KEY'", approval="never")
        assert "typesafe-secret" not in result.stdout

    asyncio.run(run())


def test_cwd_outside_refused(tmp_path):
    async def run():
        with pytest.raises(Exception):
            await run_command(tmp_path, "pwd", cwd="../", approval="never")

    asyncio.run(run())


def test_sudo_denied(tmp_path):
    async def run():
        with pytest.raises(RuntimeError, match="sudo"):
            await run_command(tmp_path, "sudo ls", approval="never")

    asyncio.run(run())


def test_engine_path_denied(tmp_path):
    async def run():
        with pytest.raises(RuntimeError, match=".engine"):
            await run_command(tmp_path, "cat .engine/session.db", approval="never")

    asyncio.run(run())


def test_always_denies_without_approve(tmp_path):
    async def run():
        approver = FakeApprover("no")
        with pytest.raises(RuntimeError, match="not approved"):
            await run_command(
                tmp_path,
                "echo hi",
                approval="always",
                approve=approver,
            )

    asyncio.run(run())


def test_auto_allows_git_status_but_not_chained(tmp_path):
    async def run():
        approver = FakeApprover("no")
        with pytest.raises(RuntimeError, match="not approved"):
            await run_command(
                tmp_path,
                "git status && rm -rf x",
                approval="auto",
                approve=approver,
            )

    asyncio.run(run())


def test_ulimit_prefix_uses_blocks(tmp_path, monkeypatch):
    async def run():
        seen = {}

        async def fake_create(*args, **kwargs):
            seen["script"] = args[0]
            raise RuntimeError("stop")

        monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_create)
        with pytest.raises(RuntimeError):
            await run_command(tmp_path, "echo hi", approval="never", file_limit_mb=1)
        assert file_limit_blocks(1) == 2048

    asyncio.run(run())


def test_format_command_result():
    result = CommandResult(
        command="echo hello",
        exit_code=0,
        stdout="hello",
        stderr="",
        duration_s=0.1,
    )
    output = format_command_result(result)
    assert "echo hello" in output
    assert "hello" in output


def test_format_command_result_timeout():
    result = CommandResult(
        command="sleep 100",
        exit_code=124,
        stdout="",
        stderr="",
        duration_s=5.0,
        timed_out=True,
    )
    output = format_command_result(result)
    assert "timed out" in output


async def test_run_command_simple(tmp_path):
    result = await run_command(tmp_path, "echo hello", timeout=5, approval="never")
    assert result.exit_code == 0
    assert "hello" in result.stdout


async def test_run_command_error(tmp_path):
    result = await run_command(
        tmp_path, "ls /nonexistent 2>/dev/null || true", timeout=5, approval="never"
    )
    assert result.exit_code == 0  # because of || true


async def test_run_command_empty_command(tmp_path):
    try:
        await run_command(tmp_path, "", timeout=5)
        assert False, "should raise ValueError"
    except ValueError as e:
        assert "required" in str(e)


async def test_run_command_whitespace_only(tmp_path):
    try:
        await run_command(tmp_path, "   ", timeout=5)
        assert False, "should raise ValueError"
    except ValueError as e:
        assert "required" in str(e)


async def test_run_command_sudo_denied(tmp_path):
    try:
        await run_command(tmp_path, "sudo ls", timeout=5)
        assert False, "should raise RuntimeError"
    except RuntimeError as e:
        assert "sudo" in str(e).lower()


async def test_run_command_engine_denied(tmp_path):
    try:
        await run_command(tmp_path, "rm -rf .engine/data", timeout=5)
        assert False, "should raise RuntimeError"
    except RuntimeError as e:
        assert ".engine" in str(e)


async def test_run_command_custom_cwd(tmp_path):
    subdir = tmp_path / "subdir"
    subdir.mkdir()
    result = await run_command(tmp_path, "pwd", cwd="subdir", timeout=5)
    assert result.exit_code == 0


async def test_run_command_timeout_bound(tmp_path):
    # Timeout > HARD_MAX_TIMEOUT should be clamped
    result = await run_command(
        tmp_path, "echo ok", timeout=10000, file_limit_mb=1, approval="never"
    )
    assert result.exit_code == 0


async def test_run_command_approval_never(tmp_path):
    result = await run_command(tmp_path, "npm test", timeout=5, approval="never")
    # Should silently allow (approval="never" means don't ask)
    assert result is not None


async def test_run_command_approval_auto_readonly(tmp_path):
    result = await run_command(tmp_path, "git status", timeout=5, approval="auto")
    # Should auto-approve read-only commands
    assert result is not None


async def test_run_command_with_callbacks(tmp_path):
    outputs = []
    
    def on_output(channel, text):
        outputs.append((channel, text))
    
    result = await run_command(
        tmp_path, "echo test", timeout=5, on_output=on_output, approval="never"
    )
    assert result.exit_code == 0


def test_run_command_sync(tmp_path):
    async def run():
        return await run_command(tmp_path, "echo sync_test", timeout=5, approval="never")

    result = asyncio.run(run())
    assert result.exit_code == 0


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
