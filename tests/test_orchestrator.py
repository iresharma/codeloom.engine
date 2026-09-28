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
from agents.orchestrator import (
    ORCH_SYSTEM,
    _PendingSettle,
    _apply_run_status,
)
from agents.compactor import AgentResult
from agents.profile import ProfileRegistry
from tools.registry import ToolRegistry

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
    return bool(names & _PERSONALITIES) or "settle_worktree" in names


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


async def _abandon_settles(orch) -> None:
    for task in list(orch._settle_tasks.values()):
        if not task.done():
            task.cancel()
    orch._pending_settles.clear()
    orch._deferred_settles.clear()
    await orch.wait_settle()


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
        assert "remember" in names
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


def test_ingest_error_does_not_block_child_finish(tmp_path):
    async def run():
        import agents.orchestrator as orch_mod

        session = await _bind(tmp_path, FakeProvider())()
        queue = session.subscribe()
        while not queue.empty():
            queue.get_nowait()

        def boom(*args, **kwargs):
            raise TypeError("bad note")

        orig = orch_mod.ingest_result
        orch_mod.ingest_result = boom
        try:
            orch: Orchestrator = session._loop
            text = await orch.spawn("ask", "ping")
            await orch.wait_children()
        finally:
            orch_mod.ingest_result = orig
        assert text.startswith("started")
        await _wait_idle(session)
        finished = [item for item in _queued(queue) if isinstance(item, AgentFinished)]
        assert finished
        assert all(item.status != "failed" for item in finished)

    asyncio.run(run())


def test_spawn_budget(tmp_path):
    async def run():
        hang = asyncio.Event()
        session = await _bind(tmp_path, _HangChild(hang), max_spawns_per_turn=2)()
        orch: Orchestrator = session._loop
        first = await orch.spawn("debugger", "one")
        second = await orch.spawn("debugger", "two")
        third = await orch.spawn("debugger", "three")
        assert first.startswith("started")
        assert second.startswith("started")
        assert "spawn budget exhausted" in third
        hang.set()
        await orch.wait_children()
        fourth = await orch.spawn("debugger", "four")
        assert fourth.startswith("started")
        await orch.wait_children()
        await _wait_idle(session)

    asyncio.run(run())


def test_survey_spawn_once_per_user_message(tmp_path):
    async def run():
        session = await _bind(tmp_path, FakeProvider())()
        orch: Orchestrator = session._loop
        orch._inbox_turn = True
        first = await orch.spawn("ask", "one")
        second = await orch.spawn("ask", "two")
        assert first.startswith("started")
        assert "already spawned ask" in second
        r1 = await orch.spawn("researcher", "repo")
        r2 = await orch.spawn("researcher", "again")
        assert r1.startswith("started")
        assert "already spawned researcher" in r2
        orch.reset_user_message_spawns()
        third = await orch.spawn("ask", "three")
        assert third.startswith("started")
        await orch.wait_children()
        await _wait_idle(session)

    asyncio.run(run())


def test_survey_first_turn_may_fan_out(tmp_path):
    async def run():
        session = await _bind(tmp_path, FakeProvider())()
        orch: Orchestrator = session._loop
        started = [await orch.spawn("ask", f"area {i}") for i in range(3)]
        assert all(item.startswith("started") for item in started)
        fourth = await orch.spawn("ask", "too many")
        assert "already spawned ask" in fourth
        researcher = await orch.spawn("researcher", "docs")
        extra_researcher = await orch.spawn("researcher", "again")
        assert researcher.startswith("started")
        assert "already spawned researcher" in extra_researcher
        await orch.wait_children()
        await _wait_idle(session)

    asyncio.run(run())


def test_survey_does_not_respawn_after_started(tmp_path):
    async def run():
        hang = asyncio.Event()
        provider = _RetryAskHang(hang)
        session = await _bind(tmp_path, provider)()
        queue = session.subscribe()
        while not queue.empty():
            queue.get_nowait()
        session.start_turn("update the landing page")
        started = []
        added = []
        for _ in range(80):
            for item in _queued(queue):
                if isinstance(item, AgentStarted):
                    started.append(item)
                elif isinstance(item, ChatMessageAdded) and item.role == "assistant":
                    if not item.agent_id:
                        added.append(item)
            turn = session._turn_task
            if (
                started
                and added
                and (turn is None or turn.done())
            ):
                break
            await asyncio.sleep(0.02)
        assert provider.orch_turns == 1
        assert len(started) == 1
        assert started[0].profile == "ask"
        assert added
        assert "I'll continue when that report arrives" in added[-1].text
        hang.set()
        await _wait_idle(session)

    asyncio.run(run())


def test_coder_does_not_respawn_after_started(tmp_path):
    async def run():
        _init_git(tmp_path)
        hang = asyncio.Event()
        provider = _RetryCoderHang(hang)
        session = await _bind(tmp_path, provider)()
        queue = session.subscribe()
        while not queue.empty():
            queue.get_nowait()
        session.start_turn("update the landing page")
        started = []
        added = []
        for _ in range(80):
            for item in _queued(queue):
                if isinstance(item, AgentStarted):
                    started.append(item)
                elif isinstance(item, ChatMessageAdded) and item.role == "assistant":
                    if not item.agent_id:
                        added.append(item)
            turn = session._turn_task
            if started and added and (turn is None or turn.done()):
                break
            await asyncio.sleep(0.02)
        assert provider.orch_turns == 1
        assert len(started) == 1
        assert started[0].profile == "coder"
        assert added
        assert "I'll continue when that report arrives" in added[-1].text
        hang.set()
        await _wait_idle(session)
        await _abandon_settles(session._loop)

    asyncio.run(run())


def test_incomplete_survey_may_respawn_once(tmp_path):
    from agents.compactor import OUTCOME_CLIP

    closer = "\n".join(
        [
            "what: coverage survey",
            "paths: runtime/server.py",
            "facts: " + ("runtime/server.py untested; " * 2000),
            "verdict: start with server.py",
        ]
    )
    assert len(closer) > OUTCOME_CLIP

    class _OrchQuiet(FakeProvider):
        def __init__(self):
            super().__init__()
            self.child_texts = [
                closer,
                "what: rest\nfacts: language.py\nverdict: ok",
            ]

        async def complete(self, messages, tools=None, *, on_delta=None, **kwargs):
            if _is_orch(tools):
                return LLMResult(text="noted")
            if self.child_texts:
                return LLMResult(text=self.child_texts.pop(0))
            return LLMResult(text="what: ok\nfacts: done\nverdict: done")

    async def run():
        session = await _bind(tmp_path, _OrchQuiet())()
        orch: Orchestrator = session._loop
        orch._inbox_turn = True
        first = await orch.spawn("ask", "one")
        assert first.startswith("started")
        await orch.wait_children()
        orch._inbox_turn = True
        second = await orch.spawn("ask", "two")
        assert second.startswith("started"), second
        await orch.wait_children()
        orch._inbox_turn = True
        third = await orch.spawn("ask", "three")
        assert "already spawned ask" in third
        await _wait_idle(session)

    asyncio.run(run())


def test_abort_turn_leaves_children(tmp_path):
    async def run():
        hang = asyncio.Event()
        provider = _AskHangThenDone(hang)
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
        if task is not None:
            await task
        orch: Orchestrator = session._loop
        assert orch._child_tasks
        # Ask already returned started, so the orch turn is done. Aborting
        # it is a no-op and must not cancel the reader.
        assert session.abort_turn() is False
        hang.set()
        await _wait_idle(session)
        finished = [item for item in _queued(queue) if isinstance(item, AgentFinished)]
        assert finished
        assert all(item.status != "aborted" for item in finished)

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


def test_second_live_coder_is_rejected(tmp_path):
    async def run():
        _init_git(tmp_path)
        hang = asyncio.Event()
        session = await _bind(tmp_path, _HangChild(hang))()
        orch: Orchestrator = session._loop
        first = await orch.spawn("coder", "add a flag")
        first_id = first.split("agent_id=")[1].split()[0]
        assert first.startswith("started")
        second = await orch.spawn("coder", "also add a flag")
        assert second.startswith("error:")
        assert first_id[:8] in second
        assert "already running" in second
        hang.set()
        await orch.wait_children()
        await _wait_idle(session)
        await orch.wait_settle()

    asyncio.run(run())


def test_coder_continue_from_reuses_worktree(tmp_path):
    async def run():
        _init_git(tmp_path)
        hang = asyncio.Event()
        session = await _bind(tmp_path, _HangChild(hang))()
        orch: Orchestrator = session._loop
        first = await orch.spawn("coder", "add a flag")
        first_id = first.split("agent_id=")[1].split()[0]
        first_worktree = orch._worktrees[first_id]
        first_branch = orch._worktree_branches[first_id]
        (first_worktree / "flag.py").write_text("x = 1\n")
        hang.set()
        await orch.wait_children()

        second = await orch.spawn(
            "coder", "fix the review feedback", continue_from=first_id
        )
        assert second.startswith("started")
        assert f"worktree={first_worktree}" in second
        assert f"branch={first_branch}" in second
        assert f"continuing {first_id[:8]}" in second

        second_id = second.split("agent_id=")[1].split()[0]
        assert orch._worktrees[second_id] == first_worktree
        assert orch._worktree_branches[second_id] == first_branch
        # ownership transferred, not duplicated -- only one agent_id should
        # be responsible for settling this worktree
        assert first_id not in orch._worktrees

        await orch.wait_children()
        await _wait_idle(session)
        await _abandon_settles(orch)

    asyncio.run(run())


def test_second_coder_auto_joins_open_worktree(tmp_path):
    async def run():
        _init_git(tmp_path)
        hang = asyncio.Event()
        session = await _bind(tmp_path, _HangChild(hang))()
        orch: Orchestrator = session._loop
        first = await orch.spawn("coder", "add a flag")
        first_id = first.split("agent_id=")[1].split()[0]
        first_worktree = orch._worktrees[first_id]
        first_branch = orch._worktree_branches[first_id]
        (first_worktree / "flag.py").write_text("x = 1\n")
        hang.set()
        await orch.wait_children()

        second = await orch.spawn("coder", "now the home mock")
        assert second.startswith("started")
        assert f"worktree={first_worktree}" in second
        assert f"branch={first_branch}" in second
        assert f"continuing {first_id[:8]}" in second
        await orch.wait_children()
        await _wait_idle(session)
        await _abandon_settles(orch)

    asyncio.run(run())


def test_tester_without_verify_joins_coder_tree(tmp_path):
    async def run():
        _init_git(tmp_path)
        hang = asyncio.Event()
        session = await _bind(tmp_path, _HangChild(hang))()
        orch: Orchestrator = session._loop
        first = await orch.spawn("coder", "add a flag")
        first_id = first.split("agent_id=")[1].split()[0]
        first_worktree = orch._worktrees[first_id]
        (first_worktree / "flag.py").write_text("x = 1\n")
        hang.set()
        await orch.wait_children()

        tester = await orch.spawn("tester", "prove the flag")
        assert tester.startswith("started")
        assert f"worktree={first_worktree}" in tester
        assert "engine/tester/" not in tester
        assert f"verifying {first_id[:8]}" in tester
        await orch.wait_children()
        await _wait_idle(session)
        await _abandon_settles(orch)

    asyncio.run(run())


def test_coder_continue_from_unknown_agent_errors(tmp_path):
    async def run():
        _init_git(tmp_path)
        session = await _bind(tmp_path, FakeProvider())()
        orch: Orchestrator = session._loop
        text = await orch.spawn("coder", "fix it", continue_from="doesnotexist")
        assert text.startswith("error:")
        assert "no open worktree" in text

    asyncio.run(run())


def test_continue_from_rejects_profile_without_own_worktree(tmp_path):
    async def run():
        _init_git(tmp_path)
        session = await _bind(tmp_path, FakeProvider())()
        orch: Orchestrator = session._loop
        text = await orch.spawn("ask", "where?", continue_from="whatever")
        assert text.startswith("error:")
        assert "does not use its own worktree" in text

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

    async def complete(self, messages, tools=None, *, on_delta=None, **kwargs):
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


class _RetryCoderHang(FakeProvider):
    def __init__(self, hang):
        super().__init__()
        self.hang = hang
        self.orch_turns = 0

    async def complete(self, messages, tools=None, *, on_delta=None, **kwargs):
        if _is_orch(tools):
            self.orch_turns += 1
            last = ""
            for message in reversed(messages or []):
                if message.get("role") == "user":
                    last = str(message.get("content") or "")
                    break
            if last.lstrip().startswith("[agent "):
                return LLMResult(text="waiting for the first coder")
            return LLMResult(
                text="",
                tool_calls=[
                    ToolCall(
                        id=str(self.orch_turns),
                        name="coder",
                        arguments_json=f'{{"task":"update landing {self.orch_turns}"}}',
                    )
                ],
            )
        await self.hang.wait()
        return _diagnostics_first(messages, tools) or LLMResult(text="child")


class _RetryAskHang(FakeProvider):
    def __init__(self, hang):
        super().__init__()
        self.hang = hang
        self.orch_turns = 0

    async def complete(self, messages, tools=None, *, on_delta=None, **kwargs):
        if _is_orch(tools):
            self.orch_turns += 1
            last = ""
            for message in reversed(messages or []):
                if message.get("role") == "user":
                    last = str(message.get("content") or "")
                    break
            if last.lstrip().startswith("[agent "):
                return LLMResult(text="waiting for the first ask")
            return LLMResult(
                text="",
                tool_calls=[
                    ToolCall(
                        id=str(self.orch_turns),
                        name="ask",
                        arguments_json=f'{{"task":"find landing {self.orch_turns}"}}',
                    )
                ],
            )
        await self.hang.wait()
        return LLMResult(text="done")


class _TwoAsks(FakeProvider):
    def __init__(self, hang):
        super().__init__()
        self.hang = hang
        self.orch_spawned = False

    async def complete(self, messages, tools=None, *, on_delta=None, **kwargs):
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

    async def complete(self, messages, tools=None, *, on_delta=None, **kwargs):
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


def _diagnostics_first(messages, tools):
    """A compliant writer calls get_diagnostics once before its closing report."""
    if "get_diagnostics" not in _tool_names(tools):
        return None
    if any(message.get("role") == "tool" for message in messages):
        return None
    return LLMResult(
        text="",
        tool_calls=[
            ToolCall(
                id="diag", name="get_diagnostics", arguments_json='{"path": "flag.py"}'
            )
        ],
    )


class _HangChild(FakeProvider):
    def __init__(self, hang):
        super().__init__()
        self.hang = hang

    async def complete(self, messages, tools=None, *, on_delta=None, **kwargs):
        if _is_orch(tools):
            return LLMResult(text="ok")
        await self.hang.wait()
        return _diagnostics_first(messages, tools) or LLMResult(text="child")


def test_discover_tools_includes_new_families():
    registry = discover_tools()
    names = registry.names()
    assert {"git_status", "git_diff", "web_fetch", "web_search"} <= names
    assert {"browser_open", "browser_console", "browser_screenshot", "browser_network"} <= names
    assert {"start_server", "stop_server", "server_logs"} <= names
    assert {
        "git_log",
        "gh_pr_view",
        "gh_pr_create",
        "github_search_code",
        "github_repo",
        "github_tree",
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


def test_apply_run_status_precedence():
    from agents.compactor import AgentResult
    from agents.orchestrator import _apply_run_status

    incomplete = AgentResult(
        status="incomplete",
        summary="missed diagnostics",
        outcome="closer",
        missing_checks=["get_diagnostics"],
    )
    _apply_run_status(incomplete, "max_turns", "stopped after 16 turns")
    assert incomplete.status == "incomplete"

    ok = AgentResult(status="ok", summary="done", outcome="done")
    _apply_run_status(ok, "max_turns", "stopped after 16 turns")
    assert ok.status == "max_turns"

    stopped = AgentResult(status="ok", summary="done", outcome="done")
    _apply_run_status(stopped, "stopped", "stopped after 16 turns")
    assert stopped.status == "stopped"

    incomplete_stop = AgentResult(
        status="incomplete",
        summary="missed diagnostics",
        outcome="closer",
        missing_checks=["get_diagnostics"],
    )
    _apply_run_status(incomplete_stop, "stopped", "stopped after 16 turns")
    assert incomplete_stop.status == "incomplete"

    aborted = AgentResult(status="incomplete", summary="s", outcome="closer")
    _apply_run_status(aborted, "aborted", "(aborted)")
    assert aborted.status == "aborted"
    assert aborted.outcome == "closer"

    failed = AgentResult(status="ok", summary="report", outcome="closer")
    _apply_run_status(failed, "failed", "error: boom")
    assert failed.status == "failed"
    assert failed.summary == "report"
    assert failed.outcome == "closer"

    empty = AgentResult(status="ok", summary="", outcome="")
    _apply_run_status(empty, "failed", "error: boom")
    assert empty.status == "failed"
    assert empty.outcome == "error: boom"


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


def _make_orchestrator(**kwargs):
    return Orchestrator(
        all_tools=ToolRegistry(),
        profiles=ProfileRegistry(),
        **kwargs,
    )


def test_orch_system_prompt():
    assert "orchestrator" in ORCH_SYSTEM.lower()
    assert "spawn" in ORCH_SYSTEM.lower()


def test_pending_settle_init():
    settle = _PendingSettle(
        agent_id="agent123",
        profile="coder",
        dest=Path("/tmp/work"),
        branch="engine/coder/agent123",
        summary="changes made",
    )
    assert settle.agent_id == "agent123"
    assert settle.profile == "coder"


def test_apply_run_status_ok_to_ok():
    result = AgentResult(status="ok", summary="done", outcome="")
    updated = _apply_run_status(result, "ok", "")
    assert updated.status == "ok"


def test_apply_run_status_ok_does_not_override_incomplete():
    # "incomplete" is set upstream by the compactor, never by run_status
    # itself; _apply_run_status must not clobber it with "ok".
    result = AgentResult(status="incomplete", summary="done", outcome="")
    updated = _apply_run_status(result, "ok", "")
    assert updated.status == "incomplete"


def test_apply_run_status_ok_to_max_turns():
    result = AgentResult(status="ok", summary="done", outcome="")
    updated = _apply_run_status(result, "max_turns", "")
    assert updated.status == "max_turns"


def test_apply_run_status_incomplete_beats_max_turns():
    result = AgentResult(status="incomplete", summary="done", outcome="")
    updated = _apply_run_status(result, "max_turns", "")
    assert updated.status == "incomplete"


def test_apply_run_status_aborted_wins():
    result = AgentResult(status="ok", summary="done", outcome="")
    updated = _apply_run_status(result, "aborted", "")
    assert updated.status == "aborted"


def test_apply_run_status_failed_wins():
    result = AgentResult(status="ok", summary="done", outcome="")
    updated = _apply_run_status(result, "failed", "error: test error")
    assert updated.status == "failed"


def test_apply_run_status_failed_preserves_summary():
    result = AgentResult(status="ok", summary="original summary", outcome="")
    updated = _apply_run_status(result, "failed", "error: failed")
    assert updated.summary == "original summary"


def test_apply_run_status_failed_sets_outcome():
    result = AgentResult(status="ok", summary="done", outcome="")
    updated = _apply_run_status(result, "failed", "error: test error")
    assert updated.outcome == "error: test error"


def test_apply_run_status_stopped():
    result = AgentResult(status="ok", summary="done", outcome="")
    updated = _apply_run_status(result, "stopped", "")
    assert updated.status == "stopped"


def test_apply_run_status_outcome_already_set():
    result = AgentResult(status="ok", summary="done", outcome="existing outcome")
    updated = _apply_run_status(result, "failed", "error: new error")
    assert updated.outcome == "existing outcome"


def test_orchestrator_init(tmp_path):
    provider = FakeProvider()
    orch = _make_orchestrator(llm=provider, workspace=tmp_path)
    assert orch._ctx.workspace == tmp_path


def test_orchestrator_system_prompt(tmp_path):
    provider = FakeProvider()
    orch = _make_orchestrator(llm=provider, workspace=tmp_path)
    assert "orchestrator" in orch._system_prompt.lower() or "spawn" in orch._system_prompt.lower()


def test_orchestrator_max_turns(tmp_path):
    from runtime.config import EngineConfig

    provider = FakeProvider()
    orch = _make_orchestrator(llm=provider, workspace=tmp_path, config=EngineConfig(max_turns=5))
    assert orch._config.max_turns == 5


def test_orchestrator_context_dump(tmp_path):
    provider = FakeProvider()
    orch = _make_orchestrator(llm=provider, workspace=tmp_path)
    dump = orch.context_dump()
    assert isinstance(dump, str)


def test_orchestrator_breakdown_for_unknown(tmp_path):
    provider = FakeProvider()
    orch = _make_orchestrator(llm=provider, workspace=tmp_path)
    breakdown = orch.breakdown_for("unknown_agent")
    assert breakdown is None


def test_orchestrator_transcript_for_unknown(tmp_path):
    provider = FakeProvider()
    orch = _make_orchestrator(llm=provider, workspace=tmp_path)
    transcript = orch.transcript_for("unknown_agent")
    assert transcript is None


def test_transcript_for_matches_report_prefix(tmp_path):
    provider = FakeProvider()
    orch = _make_orchestrator(llm=provider, workspace=tmp_path)
    full = "abcdef1234567890abcd"
    orch._finished_transcripts[full] = {
        "lines": [{"role": "assistant", "text": "saw the page"}]
    }
    found = orch.transcript_for(full[:8])
    assert found is not None
    assert found[0]["text"] == "saw the page"


def test_orchestrator_with_config(tmp_path):
    from runtime.config import EngineConfig

    provider = FakeProvider()
    config = EngineConfig(max_turns=10)
    orch = _make_orchestrator(llm=provider, workspace=tmp_path, config=config)
    assert orch._config.max_turns == 10


def test_orchestrator_child_tasks(tmp_path):
    provider = FakeProvider()
    orch = _make_orchestrator(llm=provider, workspace=tmp_path)
    assert hasattr(orch, "_child_tasks")
    assert isinstance(orch._child_tasks, dict)


def test_orchestrator_on_agent_result_callback(tmp_path):
    provider = FakeProvider()
    results = []

    def on_agent_result(agent_id, result):
        results.append((agent_id, result))

    orch = _make_orchestrator(llm=provider, workspace=tmp_path, on_agent_result=on_agent_result)
    assert orch._on_agent_result == on_agent_result


def test_orchestrator_on_tool_callback(tmp_path):
    provider = FakeProvider()
    tools_called = []

    def on_tool(name, tool_input, result, status):
        tools_called.append(name)

    orch = _make_orchestrator(llm=provider, workspace=tmp_path, on_tool=on_tool)
    assert orch._on_tool == on_tool


def test_orchestrator_hooks(tmp_path):
    from agents.hooks import AgentHooks

    provider = FakeProvider()
    hooks = AgentHooks()
    orch = _make_orchestrator(llm=provider, workspace=tmp_path, hooks=hooks)
    assert orch._hooks == hooks


def test_submit_user_message_model_round_trip():
    from protocol.codec import decode_command, encode
    from protocol.commands import SubmitUserMessage

    encoded = encode(SubmitUserMessage(text="hi", model="openai/gpt-5.6-luna"))
    decoded = decode_command(encoded)
    assert decoded.model == "openai/gpt-5.6-luna"
    omitted = decode_command(encode(SubmitUserMessage(text="hi")))
    assert omitted.model is None
    assert b'"model"' not in encode(SubmitUserMessage(text="hi"))


def test_make_subagent_uses_turn_model(tmp_path):
    from agents.profile import discover_profiles
    from agents.profiles.coder import PROFILE as CODER

    orch = Orchestrator(
        FakeProvider(),
        all_tools=discover_tools(),
        profiles=discover_profiles(),
        workspace=tmp_path,
    )
    orch.use_model("openai/gpt-test")
    child = orch._make_subagent(CODER, "abc123", tmp_path, isolated=False)
    assert child._model == "openai/gpt-test"
    orch.use_model(None)
    child = orch._make_subagent(CODER, "def456", tmp_path, isolated=False)
    assert child._model == CODER.model
