from __future__ import annotations

import asyncio
import json

from agents.agent_loop import CLOSER_MESSAGE, CONTINUE_GRANT, AgentLoop
from agents.compactor import AgentResult
from agents.hooks import AgentHooks
from agents.orchestrator import ORCH_SYSTEM, _apply_run_status
from llm.provider import LLMResult, ToolCall
from protocol.commands import AnswerPrompt
from protocol.events import AgentFinished, AgentStarted, UserPromptRequested
from runtime.config import EngineConfig
from runtime.prompts import PromptBroker
from tests.fakes import FakeProvider
from tests.test_orchestrator import (
    _bind,
    _init_git,
    _is_orch,
    _queued,
    _wait_idle,
)
from tools.base import Tool
from tools.registry import ToolRegistry


def _ping_tools() -> ToolRegistry:
    async def ping(ctx=None):
        return "pong"

    registry = ToolRegistry()
    registry.register(
        Tool(
            name="ping",
            description="ping",
            parameters={"type": "object", "properties": {}},
            fn=ping,
        )
    )
    return registry


def _ping_call(call_id: str) -> LLMResult:
    return LLMResult(
        text="",
        tool_calls=[ToolCall(id=call_id, name="ping", arguments_json="{}")],
    )


class _ScriptedAsk:
    def __init__(self, answers):
        self.answers = list(answers)
        self.asked = []

    async def __call__(self, question, kind="text", **kwargs):
        self.asked.append(
            {
                "question": question,
                "kind": kind,
                "choices": list(kwargs.get("choices") or ()),
                "default": kwargs.get("default"),
            }
        )
        if not self.answers:
            return "handoff"
        return self.answers.pop(0)


class _AlwaysPing(FakeProvider):
    def __init__(self, finish_after=None, text="done"):
        super().__init__()
        self.finish_after = finish_after
        self.text = text
        self.seen = []

    async def complete(self, messages, tools=None, *, on_delta=None, **kwargs):
        self.calls += 1
        self.seen.append(messages)
        if self.finish_after is not None and self.calls > self.finish_after:
            return LLMResult(text=self.text)
        return _ping_call(str(self.calls))


def _loop(provider, **config_kw) -> AgentLoop:
    ask = config_kw.pop("ask_user", None)
    hooks = config_kw.pop("hooks", None)
    return AgentLoop(
        provider,
        tools=_ping_tools(),
        config=EngineConfig(**config_kw),
        ask_user=ask,
        hooks=hooks,
    )


def test_closer_injected_at_n_minus_2_not_last_turn():
    async def run():
        provider = _AlwaysPing()
        loop = _loop(provider, max_turns=4, turn_continue="never")
        text = await loop.run("task")
        closers = [
            item
            for item in loop._history
            if item.get("role") == "user" and item.get("content") == CLOSER_MESSAGE
        ]
        assert len(closers) == 1
        closer_at = next(
            i
            for i, item in enumerate(loop._history)
            if item.get("content") == CLOSER_MESSAGE
        )
        tools_after = [
            item
            for item in loop._history[closer_at + 1 :]
            if item.get("tool_calls")
        ]
        assert tools_after
        assert loop._exit_status == "max_turns"
        assert "stopped after" in text

    asyncio.run(run())


def test_continue_bumps_ceiling_keeps_history_then_finishes():
    async def run():
        states = []
        provider = _AlwaysPing(finish_after=3, text="finished leftover")
        ask = _ScriptedAsk(["continue"])
        loop = _loop(
            provider,
            max_turns=3,
            turn_slice=2,
            max_continues=3,
            ask_user=ask,
            hooks=AgentHooks(
                on_state=lambda state, turn, max_turns: states.append(
                    (state, turn, max_turns)
                )
            ),
        )
        text = await loop.run("original task")
        assert text == "finished leftover"
        assert loop._exit_status == "ok"
        assert loop._config.max_turns == 3
        assert any(turn == 4 and max_turns == 5 for _, turn, max_turns in states)
        assert ask.asked and ask.asked[0]["kind"] == "choice"
        assert ask.asked[0]["choices"] == ["continue", "handoff", "stop"]
        assert ask.asked[0]["default"] == "handoff"
        assert any(
            item.get("role") == "user" and item.get("content") == "original task"
            for item in loop._history
        )
        assert any(
            CONTINUE_GRANT.format(slice=2) == item.get("content")
            for item in loop._history
        )

    asyncio.run(run())


def test_continue_does_not_leak_budget_to_next_run():
    async def run():
        ask = _ScriptedAsk(["continue"])
        loop = _loop(
            _AlwaysPing(finish_after=3, text="done"),
            max_turns=3,
            turn_slice=2,
            ask_user=ask,
        )
        await loop.run("first")
        assert loop._config.max_turns == 3
        loop._history.clear()
        provider = loop._llm
        provider.calls = 0
        provider.finish_after = None
        text = await loop.run("second")
        assert loop._exit_status == "max_turns"
        assert "stopped after 3 tool turns" in text
        assert loop._config.max_turns == 3

    asyncio.run(run())


def test_handoff_without_ask_user():
    async def run():
        loop = _loop(_AlwaysPing(), max_turns=2)
        text = await loop.run("task")
        assert loop._exit_status == "max_turns"
        assert "stopped after" in text

    asyncio.run(run())


def test_never_skips_prompt():
    async def run():
        ask = _ScriptedAsk(["continue"])
        loop = _loop(
            _AlwaysPing(),
            max_turns=2,
            turn_continue="never",
            ask_user=ask,
        )
        await loop.run("task")
        assert loop._exit_status == "max_turns"
        assert ask.asked == []

    asyncio.run(run())


def test_stop_exit_status():
    async def run():
        ask = _ScriptedAsk(["stop"])
        loop = _loop(_AlwaysPing(), max_turns=2, ask_user=ask)
        text = await loop.run("task")
        assert loop._exit_status == "stopped"
        assert text == "stopped by user request"
        assert ask.asked

    asyncio.run(run())


def test_timeout_defaults_to_handoff():
    async def run():
        broker = PromptBroker(lambda e: None)

        async def ask(question, kind="text", **kwargs):
            return await broker.ask(
                question,
                kind=kind,
                choices=kwargs.get("choices") or (),
                default=kwargs.get("default"),
                timeout=0.01,
            )

        loop = _loop(_AlwaysPing(), max_turns=2, ask_user=ask)
        await loop.run("task")
        assert loop._exit_status == "max_turns"

    asyncio.run(run())


def test_unknown_answer_is_handoff():
    async def run():
        loop = _loop(
            _AlwaysPing(),
            max_turns=2,
            ask_user=_ScriptedAsk([""]),
        )
        await loop.run("task")
        assert loop._exit_status == "max_turns"

    asyncio.run(run())


def test_fourth_cap_after_three_continues_is_forced_handoff():
    async def run():
        ask = _ScriptedAsk(["continue", "continue", "continue", "continue"])
        states = []
        loop = _loop(
            _AlwaysPing(),
            max_turns=2,
            turn_slice=2,
            max_continues=3,
            ask_user=ask,
            hooks=AgentHooks(
                on_state=lambda state, turn, max_turns: states.append(
                    (state, turn, max_turns)
                )
            ),
        )
        text = await loop.run("task")
        assert loop._exit_status == "max_turns"
        assert "stopped after 8 tool turns" in text
        assert len(ask.asked) == 3
        assert loop._config.max_turns == 2
        assert any(max_turns == 8 for _, _, max_turns in states)

    asyncio.run(run())


def test_closer_reinjected_at_new_ceiling_after_continue():
    async def run():
        ask = _ScriptedAsk(["continue"])
        loop = _loop(
            _AlwaysPing(),
            max_turns=3,
            turn_slice=3,
            ask_user=ask,
        )
        await loop.run("task")
        closers = [
            item
            for item in loop._history
            if item.get("content") == CLOSER_MESSAGE
        ]
        assert len(closers) == 2

    asyncio.run(run())


def test_apply_run_status_stopped_vs_incomplete():
    incomplete = AgentResult(
        status="incomplete",
        summary="missed",
        outcome="closer",
        missing_checks=["get_diagnostics"],
    )
    _apply_run_status(incomplete, "stopped", "stopped after 4 turns")
    assert incomplete.status == "incomplete"

    ok = AgentResult(status="ok", summary="s", outcome="o")
    _apply_run_status(ok, "stopped", "stopped after 4 turns")
    assert ok.status == "stopped"

    maxed = AgentResult(status="ok", summary="s", outcome="o")
    _apply_run_status(maxed, "max_turns", "stopped after 4 turns")
    assert maxed.status == "max_turns"


def test_orch_prompt_handoff_and_stop():
    assert "status=max_turns" in ORCH_SYSTEM
    assert "status=stopped" in ORCH_SYSTEM
    assert "Do not respawn ask or researcher on max_turns" in ORCH_SYSTEM
    assert "tell the user; do not respawn" in ORCH_SYSTEM


class _CoderCapThenHandoff(FakeProvider):
    def __init__(self):
        super().__init__()
        self.orch_turns = 0
        self.spawned = 0
        self.second_tasks: list[str] = []
        self.child_calls = 0

    async def complete(self, messages, tools=None, *, on_delta=None, **kwargs):
        if _is_orch(tools):
            self.orch_turns += 1
            user = _last_user(messages)
            if self.orch_turns == 1:
                self.spawned += 1
                return LLMResult(
                    text="",
                    tool_calls=[
                        ToolCall(
                            id="1",
                            name="coder",
                            arguments_json='{"task":"edit helper"}',
                        )
                    ],
                )
            if "status: max_turns" in user:
                self.spawned += 1
                self.second_tasks.append(user)
                return LLMResult(
                    text="",
                    tool_calls=[
                        ToolCall(
                            id="2",
                            name="coder",
                            arguments_json=json.dumps(
                                {"task": "resume leftover: finish a.py helper"}
                            ),
                        )
                    ],
                )
            return LLMResult(text="waiting")
        system = ""
        if messages and messages[0].get("role") == "system":
            system = str(messages[0].get("content") or "")
        if "Report to the orchestrator" in system:
            return LLMResult(
                text=(
                    "what: partial helper\npaths: a.py\nfacts: mid-edit\n"
                    "verdict: budget\nleftover: finish a.py helper"
                )
            )
        if self.spawned >= 2:
            return LLMResult(text="what: done\npaths: a.py\nverdict: ok\n")
        self.child_calls += 1
        if self.child_calls == 1:
            return LLMResult(
                text="",
                tool_calls=[
                    ToolCall(
                        id="d",
                        name="get_diagnostics",
                        arguments_json='{"path":"README"}',
                    )
                ],
            )
        return LLMResult(
            text="",
            tool_calls=[
                ToolCall(id=str(self.child_calls), name="list_files", arguments_json="{}")
            ],
        )


class _AskStopNoRespawn(FakeProvider):
    def __init__(self):
        super().__init__()
        self.orch_turns = 0
        self.spawned = 0

    async def complete(self, messages, tools=None, *, on_delta=None, **kwargs):
        if _is_orch(tools):
            self.orch_turns += 1
            user = _last_user(messages)
            if self.orch_turns == 1:
                self.spawned += 1
                return LLMResult(
                    text="",
                    tool_calls=[
                        ToolCall(id="1", name="ask", arguments_json='{"task":"survey"}')
                    ],
                )
            if "status: stopped" in user:
                return LLMResult(text="stopped as requested")
            return LLMResult(text="waiting")
        system = ""
        if messages and messages[0].get("role") == "system":
            system = str(messages[0].get("content") or "")
        if "Report to the orchestrator" in system:
            return LLMResult(text="what: partial\nverdict: stopped\nleftover: none")
        return LLMResult(
            text="",
            tool_calls=[ToolCall(id="1", name="list_files", arguments_json="{}")],
        )


def _last_user(messages) -> str:
    for message in reversed(messages or []):
        if message.get("role") == "user":
            return str(message.get("content") or "")
    return ""


def test_orch_respawns_coder_on_max_turns_with_leftover(tmp_path):
    async def run():
        _init_git(tmp_path)
        provider = _CoderCapThenHandoff()
        session = await _bind(tmp_path, provider, turn_continue="never")()
        session._loop._profiles.get("coder").max_turns = 4
        queue = session.subscribe()
        while not queue.empty():
            queue.get_nowait()
        session.start_turn("please implement the helper")
        await _wait_idle(session, timeout=8.0)
        items = _queued(queue)
        started = [item for item in items if isinstance(item, AgentStarted)]
        finished = [item for item in items if isinstance(item, AgentFinished)]
        assert len(started) >= 2
        assert started[0].profile == "coder"
        assert started[1].profile == "coder"
        assert any(item.status == "max_turns" for item in finished)
        assert provider.second_tasks
        assert "leftover" in provider.second_tasks[0].lower() or "max_turns" in provider.second_tasks[0]

    asyncio.run(run())


def test_stopped_child_does_not_respawn(tmp_path):
    async def run():
        provider = _AskStopNoRespawn()
        session = await _bind(tmp_path, provider)()
        session._loop._profiles.get("ask").max_turns = 3
        session._loop._child_ask_user = _ScriptedAsk(["stop"])
        queue = session.subscribe()
        while not queue.empty():
            queue.get_nowait()
        session.start_turn("look around")
        await _wait_idle(session, timeout=8.0)
        items = _queued(queue)
        started = [item for item in items if isinstance(item, AgentStarted)]
        finished = [item for item in items if isinstance(item, AgentFinished)]
        assert len(started) == 1
        assert any(item.status == "stopped" for item in finished)
        assert provider.spawned == 1

    asyncio.run(run())


def test_session_continue_prompt_uses_existing_broker(tmp_path):
    async def run():
        session = await _bind(
            tmp_path,
            _AskStopNoRespawn(),
            turn_slice=2,
            max_continues=3,
        )()
        session._loop._profiles.get("ask").max_turns = 3
        queue = session.subscribe()
        while not queue.empty():
            queue.get_nowait()
        session.start_turn("look around")
        prompt = None
        for _ in range(200):
            for item in _queued(queue):
                if isinstance(item, UserPromptRequested) and item.kind == "choice":
                    prompt = item
            if prompt is not None:
                break
            await asyncio.sleep(0.02)
        assert prompt is not None
        assert "continue" in prompt.choices
        await session.handle(AnswerPrompt(prompt_id=prompt.prompt_id, text="stop"))
        await _wait_idle(session, timeout=8.0)
        finished = [item for item in _queued(queue) if isinstance(item, AgentFinished)]
        assert any(item.status == "stopped" for item in finished)

    asyncio.run(run())
