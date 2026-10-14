"""Runtime-owned verification must survive writer budgets and optimistic replies."""

import asyncio
from dataclasses import replace

import pytest

from agents.agent_loop import CONTINUE_GRANT
from agents.compactor import AgentResult
from agents.profiles.coder import PROFILE as CODER
from agents.profiles.tester import PROFILE as TESTER
from agents.subagent import Subagent
from llm.provider import LLMResult, ToolCall
from protocol.events import AgentFinished, AgentStarted, ChatMessageAdded
from tests.fakes import FakeProvider
from tests.test_orchestrator import _bind, _queued, _wait_idle
from tools.base import Tool
from tools.registry import ToolRegistry


def _assistant_texts(items):
    return [
        event.text
        for event in items
        if isinstance(event, ChatMessageAdded)
        and event.role == "assistant"
        and not event.agent_id
    ]


def _stub_children(monkeypatch, *, status="ok", paths=("app.py",), finish_error=False):
    async def run(self, task):
        if self.profile == "coder":
            self._files_touched.extend(paths)
            self._exit_status = status if status != "incomplete" else "ok"
            for path in paths:
                (self._ctx.workspace / path).write_text("retained edit\n")
            if status == "aborted":
                raise asyncio.CancelledError
        return "done"

    async def finish(self, run_status):
        if self.profile == "coder" and finish_error:
            raise RuntimeError("finish failed")
        return AgentResult(
            status=status if self.profile == "coder" else "ok",
            summary="done",
            files_touched=list(self._files_touched),
        )

    monkeypatch.setattr(Subagent, "run", run)
    monkeypatch.setattr(Subagent, "finish", finish)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["max_turns", "incomplete", "failed", "aborted", "stopped"]
)
async def test_unfinished_edits_override_optimistic_final_reply(
    tmp_path, monkeypatch, status
):
    _stub_children(monkeypatch, status=status)
    provider = FakeProvider(
        results=[LLMResult(text="Everything is done and verified.")]
    )
    session = await _bind(tmp_path, provider)()
    queue = session.subscribe()
    await session._loop.spawn("coder", "edit app.py")
    await _wait_idle(session)
    items = _queued(queue)
    assert [event.profile for event in items if isinstance(event, AgentStarted)] == [
        "coder"
    ]
    final = _assistant_texts(items)[-1]
    assert "Incomplete; verification has not run" in final
    assert f"status={status}" in final
    assert (tmp_path / "app.py").read_text() == "retained edit\n"


@pytest.mark.asyncio
async def test_finish_failure_keeps_touched_paths_and_notice(tmp_path, monkeypatch):
    _stub_children(monkeypatch, finish_error=True)
    session = await _bind(tmp_path, FakeProvider())()
    queue = session.subscribe()
    await session._loop.spawn("coder", "edit app.py")
    await _wait_idle(session)
    assert "status=failed" in _assistant_texts(_queued(queue))[-1]
    change = next(iter(session._loop._unverified_changes.values()))
    assert change.files == ["app.py"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "paths,expected",
    [
        (("app.py",), {"coder", "reviewer", "tester"}),
        (("README.md",), {"coder", "reviewer"}),
        ((), {"coder"}),
    ],
)
async def test_successful_changes_use_required_verifiers(
    tmp_path, monkeypatch, paths, expected
):
    _stub_children(monkeypatch, paths=paths)
    session = await _bind(tmp_path, FakeProvider())()
    queue = session.subscribe()
    await session._loop.spawn("coder", "make the change")
    await _wait_idle(session)
    items = _queued(queue)
    assert {
        event.profile for event in items if isinstance(event, AgentStarted)
    } == expected
    assert not session._loop._unverified_changes
    assert all("Incomplete;" not in text for text in _assistant_texts(items))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["rejected", "missing", "exception"])
async def test_verifier_start_failure_is_reported(tmp_path, monkeypatch, failure):
    _stub_children(monkeypatch)
    session = await _bind(tmp_path, FakeProvider())()
    orch = session._loop
    original_spawn = orch.spawn
    original_get = orch._profiles.names

    async def spawn(profile, *args, **kwargs):
        if profile == "tester":
            if failure == "exception":
                raise RuntimeError("cannot create tester")
            return "error: spawn budget exhausted"
        return await original_spawn(profile, *args, **kwargs)

    if failure == "missing":
        monkeypatch.setattr(
            orch._profiles,
            "names",
            lambda: [n for n in original_get() if n != "tester"],
        )
    else:
        monkeypatch.setattr(orch, "spawn", spawn)
    queue = session.subscribe()
    await orch.spawn("coder", "edit app.py")
    await _wait_idle(session)
    items = _queued(queue)
    assert {event.profile for event in items if isinstance(event, AgentStarted)} == {
        "coder",
        "reviewer",
    }
    final = _assistant_texts(items)[-1]
    assert "Incomplete; verification has not run" in final
    assert "could not start tester" in final
    assert not orch._verifying


@pytest.mark.asyncio
async def test_successful_continuation_verifies_retained_edits_without_new_edits(
    tmp_path, monkeypatch
):
    _stub_children(monkeypatch, status="max_turns")
    session = await _bind(tmp_path, FakeProvider())()
    orch = session._loop
    first = await orch.spawn("coder", "edit app.py")
    await _wait_idle(session)
    owner = first.split("agent_id=", 1)[1].split()[0]
    assert orch._unverified_changes

    _stub_children(monkeypatch, paths=())
    queue = session.subscribe()
    await orch.spawn("coder", "finish retained work", continue_from=owner)
    await _wait_idle(session)
    items = _queued(queue)
    starts = [event for event in items if isinstance(event, AgentStarted)]
    assert {event.profile for event in starts} == {"coder", "tester", "reviewer"}
    assert all("app.py" in event.task for event in starts if event.profile != "coder")
    assert not orch._unverified_changes
    assert all("Incomplete;" not in text for text in _assistant_texts(items))


@pytest.mark.asyncio
async def test_cancellation_does_not_start_verifiers(tmp_path, monkeypatch):
    started = asyncio.Event()

    async def run(self, task):
        self._files_touched.append("app.py")
        (self._ctx.workspace / "app.py").write_text("retained edit\n")
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(Subagent, "run", run)
    session = await _bind(tmp_path, FakeProvider())()
    queue = session.subscribe()
    text = await session._loop.spawn("coder", "edit app.py")
    owner = text.split("agent_id=", 1)[1].split()[0]
    await started.wait()
    session._loop.abort_child(owner)
    await _wait_idle(session)
    items = _queued(queue)
    assert [event.profile for event in items if isinstance(event, AgentStarted)] == [
        "coder"
    ]
    assert any(
        isinstance(event, AgentFinished) and event.status == "aborted"
        for event in items
    )
    assert "Incomplete; verification has not run" in _assistant_texts(items)[-1]


class _WriterBudgetProvider(FakeProvider):
    def __init__(self, finish_after):
        super().__init__()
        self.rounds = 0
        self.finish_after = finish_after

    async def complete(self, messages, tools=None, **kwargs):
        if tools and any(item["function"]["name"] == "budget_step" for item in tools):
            self.rounds += 1
            if self.finish_after and self.rounds > self.finish_after:
                return LLMResult(text="what: done\npaths: app.py\nverdict: done")
            return LLMResult(
                tool_calls=[
                    ToolCall(
                        id=str(self.rounds), name="budget_step", arguments_json="{}"
                    )
                ]
            )
        return LLMResult(text="what: done\nverdict: done")


@pytest.mark.asyncio
@pytest.mark.parametrize("writer_profile", [CODER, TESTER], ids=["coder", "tester"])
@pytest.mark.parametrize(
    "finish_after", [5, None], ids=["second-slice-success", "exhausted"]
)
async def test_writer_second_slice_preserves_history_and_is_bounded(
    tmp_path, monkeypatch, writer_profile, finish_after
):
    session = await _bind(tmp_path, FakeProvider(), turn_slice=2)()
    orch = session._loop
    provider = _WriterBudgetProvider(finish_after)
    registry = ToolRegistry()

    async def step(ctx):
        (ctx.workspace / "app.py").write_text("retained edit\n")
        return "step"

    registry.register(
        Tool(
            name="budget_step",
            description="step",
            parameters={"type": "object", "properties": {}},
            fn=step,
        )
    )
    profile = replace(
        writer_profile,
        max_turns=2,
        required_tools=[],
        requires_tests=False,
        tool_names=["budget_step"],
    )
    original_make = orch._make_subagent
    children = []

    def make(profile_spec, owner):
        if profile_spec.name == writer_profile.name and not children:
            child = Subagent(
                profile,
                llm=provider,
                tools=registry,
                workspace=tmp_path,
                config=replace(orch._config, turn_slice=2),
            )
            child._files_touched.append("app.py")
            children.append(child)
            return child
        return original_make(profile_spec, owner)

    monkeypatch.setattr(orch, "_make_subagent", make)
    queue = session.subscribe()
    await orch.spawn(writer_profile.name, "original change")
    await _wait_idle(session)
    child = children[0]
    items = _queued(queue)
    assert provider.rounds == 6
    assert child._config.max_turns == 2
    assert (
        sum(
            item.get("content") == CONTINUE_GRANT.format(slice=2)
            for item in child._history
        )
        == 2
    )
    assert any(item.get("content") == "original change" for item in child._history)
    assert child._ctx.workspace.resolve() == tmp_path.resolve()
    starts = {event.profile for event in items if isinstance(event, AgentStarted)}
    if finish_after:
        assert child._exit_status == "ok"
        if writer_profile.name == "coder":
            assert starts == {"coder", "tester", "reviewer"}
    else:
        assert child._exit_status == "max_turns"
        assert starts == {writer_profile.name}
        if writer_profile.name == "coder":
            assert "Incomplete; verification has not run" in _assistant_texts(items)[-1]
