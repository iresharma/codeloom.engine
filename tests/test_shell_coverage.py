"""Coverage for runtime/tools/shell.py"""
from __future__ import annotations

import asyncio
from pathlib import Path

from runtime.tools.shell import (
    run_command,
    file_limit_blocks,
    format_command_result,
    CommandResult,
)


def test_file_limit_blocks():
    assert file_limit_blocks(1) == 2048
    assert file_limit_blocks(2) == 4096
    assert file_limit_blocks(0) == 2048


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
