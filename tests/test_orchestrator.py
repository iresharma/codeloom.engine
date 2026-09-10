from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

from agents.orchestrator import Orchestrator
from agents.subagent import Subagent
from llm.provider import LLMResult, ToolCall
from protocol.commands import AbortAgent, StartSession
from protocol.events import (
    AgentFinished,
    AgentStarted,
    AgentsUpdated,
    ChatMessageAdded,
    ChatMessageDelta,
    ChatMessageStarted,
    ErrorOccurred,
)
from runtime.config import EngineConfig
from runtime.session import EngineSession
from tests.fakes import FakeProvider
from tools.registry import discover_tools

_PERSONALITIES = {"ask", "coder", "tester", "reviewer", "researcher", "debugger"}


def _tool_names(tools) -> set[str]:
    names: set[str] = set()
    for item in tools or []:
        if not isinstance(item, dict):
            continue
        fn = item.get("function") if isinstance(item.get("function"), dict) else item
        name = fn.get("name") or ""
        if name:
            names.add(name)
    return names


def _is_orch(tools) -> bool:
    names = _tool_names(tools)
    return bool(names & _PERSONALITIES) or "write_context" in names or "settle_worktree" in names


def _bind(tmp_path, provider, **config_kw):
    session = EngineSession(tmp_path, tmp_path / "session.db")

    async def setup():
        await session.start()
        session._llm = provider
        session._config = EngineConfig(max_turns=8, **config_kw)
        await session.handle(StartSession(workspace=str(tmp_path)))
        session._loop._llm = provider
        return session

    return setup


async def _wait_idle(session, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        turn = session._turn_task is not None and not session._turn_task.done()
        orch = session._loop
        children = False
        if orch is not None:
            children = any(not task.done() for task in orch._child_tasks.values())
        if not turn and not children:
            await asyncio.sleep(0)
            turn = session._turn_task is not None and not session._turn_task.done()
            if not turn:
                return
        await asyncio.sleep(0.02)
    raise AssertionError("session did not go idle")


def _init_git(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.t"], cwd=path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=path, check=True, capture_output=True
    )
    (path / "README").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "i"], cwd=path, check=True, capture_output=True
    )


def test_ask_spawn_only_orch_chat(tmp_path):
    async def run():
        provider = _AskThenDone()
        session = await _bind(tmp_path, provider)()
        queue = session.subscribe()
        while not queue.empty():
            queue.get_nowait()
        session.start_turn("where is retry?")
        await _wait_idle(session)
        added = []
        started = []
        finished = []
        engine = []
        child_deltas = []
        child_added = []
        child_started = []
        while not queue.empty():
            item = queue.get_nowait()
            if isinstance(item, ChatMessageAdded) and item.role == "assistant":
                if item.agent_id:
                    child_added.append(item)
                else:
                    added.append(item)
            elif isinstance(item, ChatMessageAdded) and item.role == "engine":
                engine.append(item)
            elif isinstance(item, AgentStarted):
                started.append(item)
            elif isinstance(item, AgentFinished):
                finished.append(item)
            elif isinstance(item, ChatMessageDelta) and item.agent_id:
                child_deltas.append(item)
            elif isinstance(item, ChatMessageStarted) and item.agent_id:
                child_started.append(item)
        assert started and started[0].profile == "ask"
        assert not started[0].worktree
        assert finished
        assert added
        assert engine
        assert all("retry is in server.py" != item.text for item in added)
        assert child_started and child_deltas
        assert all(item.agent_id == started[0].agent_id for item in child_deltas)
        assert child_added
        assert child_added[-1].text == "retry is in server.py"
        assert child_added[-1].agent_id == started[0].agent_id
        assert all(
            "retry is in server.py" != m.text
            for m in session._state.messages
            if m.role == "assistant"
        )

    asyncio.run(run())


def test_orch_has_no_read_tools(tmp_path):
    async def run():
        session = await _bind(tmp_path, FakeProvider())()
        names = session._loop._tools.names()
        assert "ask" in names
        assert "write_context" in names
        assert "settle_worktree" in names
        assert "list_files" not in names
        assert "read_file" not in names
        assert "search" not in names
        assert "list_symbols" not in names

    asyncio.run(run())


def test_parallel_spawns_start_before_finish(tmp_path):
    async def run():
        hang = asyncio.Event()
        provider = _TwoAsks(hang)
        session = await _bind(tmp_path, provider)()
        queue = session.subscribe()
        while not queue.empty():
            queue.get_nowait()
        seen = []
        session.start_turn("look around")
        started = []
        for _ in range(50):
            seen.extend(_queued(queue))
            started = [item for item in seen if isinstance(item, AgentStarted)]
            if len(started) >= 2:
                break
            await asyncio.sleep(0.02)
        finished_mid = [item for item in seen if isinstance(item, AgentFinished)]
        assert len(started) >= 2
        assert not finished_mid
        assert started[0].batch_id
        assert started[0].batch_id == started[1].batch_id
        assert started[0].batch_name == started[1].batch_name
        assert started[0].batch_name == "look around"
        assert started[0].task
        hang.set()
        await _wait_idle(session)
        seen.extend(_queued(queue))
        finished = [item for item in seen if isinstance(item, AgentFinished)]
        assert len(finished) >= 2

    asyncio.run(run())


def test_agents_updated_and_snapshot_include_task(tmp_path):
    async def run():
        hang = asyncio.Event()
        provider = _AskHangThenDone(hang)
        session = await _bind(tmp_path, provider)()
        queue = session.subscribe()
        while not queue.empty():
            queue.get_nowait()
        session.start_turn("look around")
        updated = []
        for _ in range(50):
            for item in _queued(queue):
                if isinstance(item, AgentsUpdated):
                    updated.append(item)
            if updated and updated[-1].agents:
                break
            await asyncio.sleep(0.02)
        assert updated
        live = updated[-1].agents
        assert len(live) >= 1
        assert live[0].task
        assert live[0].batch_id
        assert live[0].batch_name == "look around"
        snap_rows = session.snapshot().agents or []
        assert snap_rows
        assert snap_rows[0].task == live[0].task
        hang.set()
        await _wait_idle(session)

    asyncio.run(run())


def test_abort_unknown_agent(tmp_path):
    async def run():
        session = EngineSession(tmp_path, tmp_path / "session.db")
        await session.start()
        queue = session.subscribe()
        await session.handle(StartSession(workspace=str(tmp_path)))
        while not queue.empty():
            queue.get_nowait()
        await session.handle(AbortAgent(agent_id="x"))
        messages = [
            item.message
            for item in _queued(queue)
            if isinstance(item, ErrorOccurred)
        ]
        assert any("unknown agent" in message for message in messages)

    asyncio.run(run())


def test_abort_one_child_orch_continues(tmp_path):
    async def run():
        hang = asyncio.Event()
        provider = _AskHangThenDone(hang)
        session = await _bind(tmp_path, provider)()
        queue = session.subscribe()
        while not queue.empty():
            queue.get_nowait()
        session.start_turn("ask something")
        agent_id = ""
        for _ in range(50):
            for item in _queued(queue):
                if isinstance(item, AgentStarted):
                    agent_id = item.agent_id
            if agent_id:
                break
            await asyncio.sleep(0.02)
        assert agent_id
        assert session.abort_child(agent_id)
        hang.set()
        await _wait_idle(session)
        finished = [item for item in _queued(queue) if isinstance(item, AgentFinished)]
        assert any(item.status == "aborted" for item in finished)

    asyncio.run(run())


def test_child_crash_is_failed_not_orch_crash(tmp_path):
    async def run():
        session = await _bind(tmp_path, FakeProvider())()
        queue = session.subscribe()
        while not queue.empty():
            queue.get_nowait()

        async def boom(self, task):
            raise RuntimeError("boom")

        orig = Subagent.run
        Subagent.run = boom  # type: ignore[method-assign]
        try:
            orch: Orchestrator = session._loop
            text = await orch.spawn("ask", "ping")
            await orch.wait_children()
        finally:
            Subagent.run = orig  # type: ignore[method-assign]
        assert text.startswith("started")
        await _wait_idle(session)
        finished = [item for item in _queued(queue) if isinstance(item, AgentFinished)]
        assert any(item.status == "failed" for item in finished)

    asyncio.run(run())


def test_spawn_budget(tmp_path):
    async def run():
        hang = asyncio.Event()
        session = await _bind(tmp_path, _HangChild(hang), max_spawns_per_turn=2)()
        orch: Orchestrator = session._loop
        first = await orch.spawn("ask", "one")
        second = await orch.spawn("ask", "two")
        third = await orch.spawn("ask", "three")
        assert first.startswith("started")
        assert second.startswith("started")
        assert "spawn budget exhausted" in third
        hang.set()
        await orch.wait_children()
        fourth = await orch.spawn("ask", "four")
        assert fourth.startswith("started")
        await orch.wait_children()
        await _wait_idle(session)

    asyncio.run(run())


def test_abort_turn_leaves_children(tmp_path):
    async def run():
        hang = asyncio.Event()
        provider = _AskHangThenDone(hang, hang_orch=True)
        session = await _bind(tmp_path, provider)()
        queue = session.subscribe()
        while not queue.empty():
            queue.get_nowait()
        session.start_turn("go")
        for _ in range(50):
            if any(isinstance(item, AgentStarted) for item in _queued(queue)):
                break
            await asyncio.sleep(0.02)
        task = session._turn_task
        assert session.abort_turn()
        if task is not None:
            await task
        orch: Orchestrator = session._loop
        assert orch._child_tasks
        hang.set()
        await _wait_idle(session)
        finished = [item for item in _queued(queue) if isinstance(item, AgentFinished)]
        assert finished
        assert all(item.status != "aborted" for item in finished)
        texts = [m.text for m in session._state.messages if m.role == "assistant"]
        assert any("aborted" in text for text in texts)

    asyncio.run(run())


def test_user_can_talk_while_child_runs(tmp_path):
    async def run():
        hang = asyncio.Event()
        provider = _AskHangThenDone(hang)
        session = await _bind(tmp_path, provider)()
        queue = session.subscribe()
        while not queue.empty():
            queue.get_nowait()
        session.start_turn("build a feature")
        for _ in range(50):
            if any(isinstance(item, AgentStarted) for item in _queued(queue)):
                break
            await asyncio.sleep(0.02)
        turn = session._turn_task
        if turn is not None:
            await turn
        errors = [
            item.message for item in _queued(queue) if isinstance(item, ErrorOccurred)
        ]
        assert not any("busy" in message for message in errors)
        session.start_turn("debug the escalation")
        errors = [
            item.message for item in _queued(queue) if isinstance(item, ErrorOccurred)
        ]
        assert not any("busy" in message for message in errors)
        second = session._turn_task
        if second is not None:
            await second
        hang.set()
        await _wait_idle(session)
        users = [m.text for m in session._state.messages if m.role == "user"]
        assert "build a feature" in users
        assert "debug the escalation" in users

    asyncio.run(run())


def test_coder_gets_worktree(tmp_path):
    async def run():
        _init_git(tmp_path)
        hang = asyncio.Event()
        session = await _bind(tmp_path, _HangChild(hang))()
        orch: Orchestrator = session._loop
        text = await orch.spawn("coder", "add a flag")
        assert "worktree=" in text
        assert "branch=engine/coder/" in text
        started = False
        for _ in range(50):
            if orch._child_tasks:
                started = True
                break
            await asyncio.sleep(0.02)
        assert started
        trees = list((tmp_path / ".engine" / "worktrees").iterdir())
        assert trees
        hang.set()
        await orch.wait_children()
        await _wait_idle(session)
        await orch.wait_settle()
        assert not trees[0].exists()

    asyncio.run(run())


def test_ask_skips_worktree(tmp_path):
    async def run():
        _init_git(tmp_path)
        session = await _bind(tmp_path, FakeProvider())()
        orch: Orchestrator = session._loop
        text = await orch.spawn("ask", "where?")
        assert "worktree=" not in text
        await orch.wait_children()
        await _wait_idle(session)
        root = tmp_path / ".engine" / "worktrees"
        assert not root.exists() or not any(root.iterdir())

    asyncio.run(run())


def _queued(queue):
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


class _AskThenDone(FakeProvider):
    def __init__(self):
        super().__init__()
        self.orch_turns = 0

    async def complete(self, messages, tools=None, *, on_delta=None):
        if _is_orch(tools):
            self.orch_turns += 1
            if self.orch_turns == 1:
                return LLMResult(
                    text="",
                    tool_calls=[
                        ToolCall(
                            id="1", name="ask", arguments_json='{"task":"find retry"}'
                        )
                    ],
                )
            if self.orch_turns == 2:
                return LLMResult(text="spawned, waiting")
            return LLMResult(text="ask found retry")
        if on_delta:
            on_delta("text", "retry is in server.py")
        return LLMResult(text="retry is in server.py")


class _TwoAsks(FakeProvider):
    def __init__(self, hang):
        super().__init__()
        self.hang = hang
        self.orch_spawned = False

    async def complete(self, messages, tools=None, *, on_delta=None):
        if _is_orch(tools):
            if not self.orch_spawned:
                self.orch_spawned = True
                return LLMResult(
                    text="",
                    tool_calls=[
                        ToolCall(id="1", name="ask", arguments_json='{"task":"a"}'),
                        ToolCall(id="2", name="ask", arguments_json='{"task":"b"}'),
                    ],
                )
            return LLMResult(text="spawned both")
        await self.hang.wait()
        return LLMResult(text="done")


class _AskHangThenDone(FakeProvider):
    def __init__(self, hang, *, hang_orch=False):
        super().__init__()
        self.hang = hang
        self.hang_orch = hang_orch
        self.orch_spawned = False

    async def complete(self, messages, tools=None, *, on_delta=None):
        if _is_orch(tools):
            if not self.orch_spawned:
                self.orch_spawned = True
                return LLMResult(
                    text="",
                    tool_calls=[
                        ToolCall(id="1", name="ask", arguments_json='{"task":"x"}')
                    ],
                )
            if self.hang_orch:
                await self.hang.wait()
            return LLMResult(text="orch done")
        await self.hang.wait()
        return LLMResult(text="child")


class _HangChild(FakeProvider):
    def __init__(self, hang):
        super().__init__()
        self.hang = hang

    async def complete(self, messages, tools=None, *, on_delta=None):
        if _is_orch(tools):
            return LLMResult(text="ok")
        await self.hang.wait()
        return LLMResult(text="child")


def test_discover_tools_includes_new_families():
    registry = discover_tools()
    names = registry.names()
    assert {"git_status", "git_diff", "web_fetch", "web_search"} <= names
    assert {"browser_open", "browser_console", "browser_screenshot", "browser_network"} <= names
    assert {
        "git_log",
        "gh_pr_view",
        "gh_pr_create",
        "github_search_code",
        "pkg_info",
        "docs_lookup",
        "tldr",
        "osv_query",
        "http_request",
        "todo_scan",
        "runtime_info",
        "dep_why",
    } <= names
    assert not registry.errors


def test_batch_nickname():
    from agents.orchestrator import batch_nickname

    assert batch_nickname("look around") == "look around"
    assert batch_nickname("  ship the flag\nplease  ") == "ship the flag please"
    assert batch_nickname("") == "untitled"
    long = "x" * 80
    assert batch_nickname(long).endswith("...")
    assert len(batch_nickname(long)) == 48
    assert (
        batch_nickname("[agent ask abcdef12 finished]\nstatus: ok") == "after ask"
    )


def test_request_orch_context(tmp_path):
    from protocol.commands import RequestOrchContext
    from protocol.events import OrchContext

    async def run():
        session = await _bind(tmp_path, FakeProvider())()
        session._loop.hydrate(
            [
                type("M", (), {"role": "user", "text": "hello"})(),
                type("M", (), {"role": "assistant", "text": "hi"})(),
            ]
        )
        queue = session.subscribe()
        while not queue.empty():
            queue.get_nowait()
        await session.handle(RequestOrchContext())
        events = [item for item in _queued(queue) if isinstance(item, OrchContext)]
        assert events
        assert "--- system ---" in events[0].text
        assert "hello" in events[0].text
        assert "hi" in events[0].text

    asyncio.run(run())
