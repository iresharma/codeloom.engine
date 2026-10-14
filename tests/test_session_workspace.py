from __future__ import annotations

import asyncio
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest

from agents.subagent import Subagent
from runtime.language import LanguageInfo
from runtime.tools.lsp import LSPManager
from tests.fakes import FakeProvider
from tests.test_orchestrator import _bind, _init_git


def test_agents_share_session_resources_until_shutdown(tmp_path, monkeypatch):
    manager = MagicMock()
    factory = MagicMock(return_value=manager)
    monkeypatch.setattr("runtime.session.LSPManager", factory)
    monkeypatch.setattr(
        "runtime.session.detect_language",
        lambda _: LanguageInfo("python", True, {}, None),
    )

    async def run():
        _init_git(tmp_path)
        # Existing, uncommitted user changes must be visible to every agent.
        (tmp_path / "README").write_text("user edit\n")
        release = asyncio.Event()

        async def child_run(self, task):
            await release.wait()
            self._exit_status = "stopped"
            return "stopped"

        monkeypatch.setattr(Subagent, "run", child_run)
        session = await _bind(tmp_path, FakeProvider())()
        orch = session._loop
        ids = {}
        for name in ("ask", "researcher", "debugger", "coder", "reviewer", "tester"):
            reply = await orch.spawn(
                name, "check the current files",
                verify_owner=ids["coder"] if name in {"reviewer", "tester"} else "",
            )
            assert reply.startswith("started"), reply
            ids[name] = reply.split("agent_id=")[1].split()[0]
        children = list(orch._children.values())
        assert all(child._ctx.workspace == tmp_path for child in children)
        assert all(child._ctx.lsp is manager for child in children)
        assert all(child._ctx.write_lock is session._write_lock for child in children)
        assert orch._ctx.write_lock is session._write_lock
        assert len({id(child._ctx.files) for child in children}) == len(children)
        assert all((child._ctx.workspace / "README").read_text() == "user edit\n"
                   for child in children)
        assert not (tmp_path / ".engine" / "worktrees").exists()
        factory.assert_called_once_with(tmp_path)

        # Ending one child must leave the shared LSP available to siblings.
        orch.abort_child(ids["ask"])
        await asyncio.sleep(0)
        manager.shutdown_all.assert_not_called()
        # Omission of continue_from must not bypass the verification gate.
        assert (await orch.spawn("coder", "edit during verification")).startswith("error:")
        release.set()
        await orch.wait_children()
        manager.shutdown_all.assert_not_called()

        reply = await orch.spawn("coder", "continue", continue_from=ids["coder"])
        next_id = reply.split("agent_id=")[1].split()[0]
        assert orch._children[next_id]._ctx.lsp is manager
        await orch.wait_children()
        session._start_lsp()
        factory.assert_called_once_with(tmp_path)
        assert session._prompts.pending() is None
        await session.aclose()
        manager.shutdown_all.assert_called_once()
        assert (tmp_path / "README").read_text() == "user edit\n"
        assert subprocess.check_output(
            ["git", "show", "HEAD:README"], cwd=tmp_path, text=True
        ) == "x\n"  # No automatic commit.

    asyncio.run(run())


def test_shutdown_preserves_aborted_coder_edits(tmp_path, monkeypatch):
    async def run():
        _init_git(tmp_path)
        edited = asyncio.Event()

        async def child_run(self, task):
            (self._ctx.workspace / "partial.txt").write_text("partial change\n")
            edited.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(Subagent, "run", child_run)
        session = await _bind(tmp_path, FakeProvider())()
        await session._loop.spawn("coder", "make a change")
        await edited.wait()
        await session.aclose()
        assert (tmp_path / "partial.txt").read_text() == "partial change\n"
        assert not (tmp_path / ".engine" / "worktrees").exists()

    asyncio.run(run())


@pytest.mark.parametrize("language", ["python", "javascript"])
def test_concurrent_lsp_start_creates_one_server(tmp_path, monkeypatch, language):
    manager = LSPManager(tmp_path)
    initializing = threading.Event()
    duplicate = threading.Event()
    release = threading.Event()
    clients = []

    def make_client(*args, **kwargs):
        client = MagicMock()
        client._alive = False
        clients.append(client)
        if len(clients) > 1:
            duplicate.set()

        def initialize(*args, **kwargs):
            initializing.set()
            assert release.wait(2)
            return {}

        client.request.side_effect = initialize
        return client

    monkeypatch.setattr("runtime.tools.lsp.LSPClient", make_client)
    cfg = manager.SERVER_CONFIGS[language]
    # JS and TS use the same server command, so also test cross-language reuse.
    other = manager.SERVER_CONFIGS["typescript"] if language == "javascript" else cfg
    second_entered = threading.Event()

    def start_second():
        second_entered.set()
        return manager._client_for(other)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(manager._client_for, cfg)
        assert initializing.wait(2)
        second = pool.submit(start_second)
        try:
            assert second_entered.wait(2)
            assert not duplicate.wait(0.1)
        finally:
            release.set()
        assert first.result(timeout=2) is second.result(timeout=2)
    assert len(clients) == 1
    manager.shutdown_all()
    clients[0].shutdown.assert_called_once()


def test_failed_lsp_initialization_closes_process_before_retry(tmp_path, monkeypatch):
    manager = LSPManager(tmp_path)
    failed = MagicMock()
    failed.request.side_effect = RuntimeError("initialize failed")
    healthy = MagicMock()
    healthy._alive = False
    factory = MagicMock(side_effect=[failed, healthy])
    monkeypatch.setattr("runtime.tools.lsp.LSPClient", factory)
    cfg = manager.SERVER_CONFIGS["python"]
    with pytest.raises(RuntimeError, match="initialize failed"):
        manager._client_for(cfg)
    failed.shutdown.assert_called_once()
    assert manager._client_for(cfg) is healthy
    manager.shutdown_all()
    healthy.shutdown.assert_called_once()


@pytest.mark.parametrize("second_action", ["open", "change"])
def test_shared_lsp_opens_document_once(tmp_path, monkeypatch, second_action):
    (tmp_path / "app.py").write_text("VALUE = 1\n")
    manager = LSPManager(tmp_path)
    client = MagicMock()
    monkeypatch.setattr(manager, "_client_for", lambda _: client)
    opening = threading.Event()
    second_entered = threading.Event()
    release = threading.Event()
    duplicate = threading.Event()
    messages = []

    def notify(method, params):
        if method == "textDocument/didOpen":
            if opening.is_set():
                duplicate.set()
            opening.set()
            assert release.wait(2)
        messages.append((method, params))

    client.notify.side_effect = notify

    def start_second():
        second_entered.set()
        if second_action == "open":
            return manager._did_open("app.py")
        return manager.did_change("app.py", "VALUE = 2\n")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(manager._did_open, "app.py")
        assert opening.wait(2)
        second = pool.submit(start_second)
        try:
            assert second_entered.wait(2)
            assert not duplicate.wait(0.1)
        finally:
            release.set()
        assert first.result(timeout=2) == second.result(timeout=2)
    assert [method for method, _ in messages].count("textDocument/didOpen") == 1
    if second_action == "change":
        assert messages[-1][0] == "textDocument/didChange"
        assert messages[-1][1]["textDocument"]["version"] == 2
        assert manager._sent_sha[str(tmp_path / "app.py")] == manager._text_sha("VALUE = 2\n")
