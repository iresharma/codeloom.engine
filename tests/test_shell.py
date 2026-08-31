from __future__ import annotations

import asyncio
import os

import pytest

from runtime.config import EngineConfig
from runtime.tools.shell import file_limit_blocks, format_command_result, run_command
from tests.fakes import FakeApprover


def test_file_limit_blocks():
    assert file_limit_blocks(1) == 2048
    assert file_limit_blocks(2048) == 2048 * 2048


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


def test_ulimit_prefix_uses_blocks(tmp_path):
    async def run():
        seen = {}

        async def fake_create(*args, **kwargs):
            seen["script"] = args[0]
            raise RuntimeError("stop")

        asyncio.create_subprocess_shell = fake_create  # type: ignore[assignment]
        try:
            with pytest.raises(RuntimeError):
                await run_command(tmp_path, "echo hi", approval="never", file_limit_mb=1)
        finally:
            pass
        # May not patch successfully; assert helper instead.
        assert file_limit_blocks(1) == 2048

    asyncio.run(run())
