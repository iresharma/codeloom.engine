"""Coverage tests for runtime/tools/shell.py"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from runtime.tools.shell import (
    CommandResult,
    file_limit_blocks,
    format_command_result,
    run_command,
    _hard_deny,
    _auto_allowed,
    _join_capped,
)


def test_file_limit_blocks_zero():
    """Test file_limit_blocks with zero."""
    result = file_limit_blocks(0)
    assert result == 2048


def test_file_limit_blocks_one():
    """Test file_limit_blocks with one."""
    result = file_limit_blocks(1)
    assert result == 2048


def test_file_limit_blocks_large():
    """Test file_limit_blocks with large value."""
    result = file_limit_blocks(2048)
    assert result == 2048 * 2048


def test_file_limit_blocks_negative():
    """Test file_limit_blocks with negative value."""
    result = file_limit_blocks(-1)
    assert result == 2048


def test_format_command_result_success():
    """Test format_command_result with successful command."""
    result = CommandResult(
        command="echo hello",
        exit_code=0,
        stdout="hello\n",
        stderr="",
        duration_s=0.1,
    )
    formatted = format_command_result(result)
    assert "echo hello" in formatted
    assert "exit code: 0" in formatted
    assert "hello" in formatted
    assert "(empty)" in formatted


def test_format_command_result_error():
    """Test format_command_result with error."""
    result = CommandResult(
        command="false",
        exit_code=1,
        stdout="",
        stderr="error message",
        duration_s=0.05,
    )
    formatted = format_command_result(result)
    assert "false" in formatted
    assert "exit code: 1" in formatted
    assert "error message" in formatted


def test_format_command_result_timeout():
    """Test format_command_result with timeout."""
    result = CommandResult(
        command="sleep 10",
        exit_code=-1,
        stdout="",
        stderr="",
        duration_s=5.0,
        timed_out=True,
    )
    formatted = format_command_result(result)
    assert "sleep 10" in formatted
    assert "timed out" in formatted
    assert "(5" in formatted or "5.0" in formatted


def test_hard_deny_sudo():
    """Test _hard_deny with sudo command."""
    with pytest.raises(RuntimeError, match="sudo"):
        _hard_deny("sudo ls")


def test_hard_deny_sudo_with_path():
    """Test _hard_deny with sudo in path."""
    with pytest.raises(RuntimeError, match="sudo"):
        _hard_deny("sudo /bin/bash")


def test_hard_deny_engine_path():
    """Test _hard_deny with .engine path."""
    with pytest.raises(RuntimeError, match=".engine"):
        _hard_deny("cat .engine/secret")


def test_hard_deny_engine_path_with_spaces():
    """Test _hard_deny with .engine in command."""
    with pytest.raises(RuntimeError, match=".engine"):
        _hard_deny("rm -rf .engine")


def test_hard_deny_safe_command():
    """Test _hard_deny with safe command."""
    # Should not raise
    _hard_deny("echo hello")


def test_auto_allowed_never():
    """Test _auto_allowed with approval='never'."""
    assert _auto_allowed("anything", "never") is True


def test_auto_allowed_always():
    """Test _auto_allowed with approval='always'."""
    assert _auto_allowed("anything", "always") is False


def test_auto_allowed_auto_read_only():
    """Test _auto_allowed with read-only command."""
    assert _auto_allowed("git log", "auto") is True
    assert _auto_allowed("ls", "auto") is True
    assert _auto_allowed("cat file.txt", "auto") is True
    assert _auto_allowed("pwd", "auto") is True


def test_auto_allowed_auto_with_metachar():
    """Test _auto_allowed with shell metacharacters."""
    assert _auto_allowed("echo hello; ls", "auto") is False
    assert _auto_allowed("ls | grep txt", "auto") is False
    assert _auto_allowed("cat a && cat b", "auto") is False
    assert _auto_allowed("echo test > file", "auto") is False
    assert _auto_allowed("echo `cmd`", "auto") is False
    assert _auto_allowed("echo $(cmd)", "auto") is False


def test_auto_allowed_auto_write_command():
    """Test _auto_allowed with write command."""
    assert _auto_allowed("rm file.txt", "auto") is False
    assert _auto_allowed("cp a b", "auto") is False


def test_auto_allowed_read_prefix_exact():
    """Test _auto_allowed with exact read-only prefix."""
    assert _auto_allowed("git status", "auto") is True
    assert _auto_allowed("git diff", "auto") is True
    assert _auto_allowed("git show HEAD", "auto") is True


def test_auto_allowed_read_prefix_with_args():
    """Test _auto_allowed with read-only prefix and arguments."""
    assert _auto_allowed("git log --oneline", "auto") is True
    assert _auto_allowed("ls -la", "auto") is True
    assert _auto_allowed("cat file.py", "auto") is True


def test_join_capped_under_limit():
    """Test _join_capped when text is under limit."""
    parts = ["hello", " ", "world"]
    result = _join_capped(parts, 20)
    assert result == "hello world"


def test_join_capped_over_limit():
    """Test _join_capped when text exceeds limit."""
    text = "x" * 100
    parts = [text[i:i+10] for i in range(0, len(text), 10)]
    result = _join_capped(parts, 30)
    assert "... (" in result
    assert ") ..." in result
    assert "omitted" in result or "elided" in result


def test_join_capped_exact_limit():
    """Test _join_capped when text exactly matches limit."""
    parts = ["hello"]
    result = _join_capped(parts, 5)
    assert result == "hello"


def test_join_capped_large_text():
    """Test _join_capped with large text."""
    parts = ["a" * 1000]
    result = _join_capped(parts, 100)
    assert len(result) < len("a" * 1000)
    assert "elided" in result


def test_command_result_dataclass():
    """Test CommandResult dataclass creation."""
    result = CommandResult(
        command="test",
        exit_code=0,
        stdout="out",
        stderr="err",
        duration_s=1.5,
    )
    assert result.command == "test"
    assert result.exit_code == 0
    assert result.timed_out is False


def test_command_result_with_timeout():
    """Test CommandResult with timed_out flag."""
    result = CommandResult(
        command="test",
        exit_code=-1,
        stdout="",
        stderr="",
        duration_s=30.0,
        timed_out=True,
    )
    assert result.timed_out is True


async def test_run_command_empty(tmp_path):
    """Test run_command with empty command."""
    with pytest.raises(ValueError, match="command is required"):
        await run_command(tmp_path, "")


async def test_run_command_whitespace_only(tmp_path):
    """Test run_command with whitespace-only command."""
    with pytest.raises(ValueError, match="command is required"):
        await run_command(tmp_path, "   ")


async def test_run_command_hard_denied(tmp_path):
    """Test run_command with denied command."""
    with pytest.raises(RuntimeError, match="sudo"):
        await run_command(tmp_path, "sudo ls", approval="never")


async def test_run_command_always_requires_approval(tmp_path):
    """Test run_command with approval='always' requires user."""
    with pytest.raises(RuntimeError, match="not approved"):
        await run_command(tmp_path, "echo hello", approval="always")


async def test_run_command_approval_denied(tmp_path):
    """Test run_command when user denies approval."""
    async def deny(question: str, kind: str) -> bool:
        return False
    
    with pytest.raises(RuntimeError, match="not approved"):
        await run_command(
            tmp_path,
            "rm file",
            approval="auto",
            approve=deny,
        )


async def test_run_command_approval_approved(tmp_path):
    """Test run_command when user approves."""
    async def approve(question: str, kind: str) -> bool:
        return True
    
    result = await run_command(
        tmp_path,
        "echo test",
        approval="auto",
        approve=approve,
    )
    assert result.exit_code == 0
    assert "test" in result.stdout


async def test_run_command_approval_false_string(tmp_path):
    """Test run_command when approver returns non-yes value."""
    async def deny_via_string(question: str, kind: str) -> bool:
        return False
    
    with pytest.raises(RuntimeError, match="not approved"):
        await run_command(
            tmp_path,
            "rm file",
            approval="auto",
            approve=deny_via_string,
        )


async def test_run_command_timeout(tmp_path):
    """Test run_command with timeout."""
    result = await run_command(
        tmp_path,
        "sleep 30",
        timeout=1,
        approval="never",
    )
    assert result.timed_out is True


async def test_run_command_nonzero_exit(tmp_path):
    """Test run_command with non-zero exit code."""
    result = await run_command(
        tmp_path,
        "sh -c 'exit 5'",
        approval="never",
    )
    assert result.exit_code == 5


async def test_run_command_cwd_invalid(tmp_path):
    """Test run_command with invalid cwd."""
    with pytest.raises(Exception):  # WorkspacePathError
        await run_command(
            tmp_path,
            "pwd",
            cwd="../outside",
            approval="never",
        )


async def test_run_command_cwd_valid(tmp_path):
    """Test run_command with valid cwd."""
    subdir = tmp_path / "subdir"
    subdir.mkdir()
    result = await run_command(
        tmp_path,
        "pwd",
        cwd="subdir",
        approval="never",
    )
    assert result.exit_code == 0
    assert "subdir" in result.stdout


async def test_run_command_timeout_value_clamped(tmp_path):
    """Test run_command clamps timeout to limits."""
    # Very large timeout should be clamped
    result = await run_command(
        tmp_path,
        "echo test",
        timeout=9999,
        approval="never",
    )
    assert result.exit_code == 0


async def test_run_command_timeout_too_small(tmp_path):
    """Test run_command with very small timeout."""
    result = await run_command(
        tmp_path,
        "echo test",
        timeout=0,
        approval="never",
    )
    # Should be clamped to minimum of 1
    assert result.exit_code == 0


async def test_run_command_on_output_callback(tmp_path):
    """Test run_command with on_output callback."""
    outputs = []
    
    def capture(channel, text):
        outputs.append((channel, text))
    
    result = await run_command(
        tmp_path,
        "echo hello",
        on_output=capture,
        approval="never",
    )
    assert result.exit_code == 0
    assert any("hello" in text for channel, text in outputs if channel == "stdout")


async def test_run_command_on_proc_callback(tmp_path):
    """Test run_command with on_proc callback."""
    proc_events = []
    
    def track_proc(proc, started):
        proc_events.append(started)
    
    result = await run_command(
        tmp_path,
        "echo test",
        on_proc=track_proc,
        approval="never",
    )
    assert result.exit_code == 0
    assert len(proc_events) == 2  # Started and finished
    assert proc_events[0] is True  # Started
    assert proc_events[1] is False  # Finished


async def test_run_command_env_excludes_openrouter(tmp_path):
    """Test run_command excludes OPENROUTER_* env vars."""
    os.environ["OPENROUTER_API_KEY"] = "secret"
    try:
        result = await run_command(
            tmp_path,
            "sh -c 'echo $OPENROUTER_API_KEY'",
            approval="never",
        )
        assert "secret" not in result.stdout
    finally:
        del os.environ["OPENROUTER_API_KEY"]


async def test_run_command_env_has_defaults(tmp_path):
    """Test run_command sets default env vars."""
    result = await run_command(
        tmp_path,
        "sh -c 'echo $CI'",
        approval="never",
    )
    assert result.exit_code == 0
    assert "1" in result.stdout


async def test_run_command_stderr_captured(tmp_path):
    """Test run_command captures stderr separately."""
    result = await run_command(
        tmp_path,
        "sh -c 'echo stdout; echo stderr >&2'",
        approval="never",
    )
    assert "stdout" in result.stdout
    assert "stderr" in result.stderr


async def test_run_command_large_output_capped(tmp_path):
    """Test run_command caps large output."""
    result = await run_command(
        tmp_path,
        "python -c 'print(\"x\" * 100000)'",
        approval="never",
    )
    # Output should be capped
    assert result.exit_code == 0


async def test_run_command_file_limit_applied(tmp_path):
    """Test run_command applies file limit."""
    result = await run_command(
        tmp_path,
        "sh -c 'echo ulimit'",
        file_limit_mb=1024,
        approval="never",
    )
    assert result.exit_code == 0
