from __future__ import annotations

import asyncio
import time

import pytest

from headless_client import HeadlessError, auto_answer, drive_session, wait_until_idle
from llm.provider import LLMResult, Usage
from protocol.commands import AnswerPrompt
from protocol.events import (
    AgentFinished,
    AgentStateChanged,
    UserPromptRequested,
)
from tests.fakes import FakeProvider
from tests.test_orchestrator import _bind


def _prompt(**kwargs) -> UserPromptRequested:
    data = {
        "prompt_id": "p1",
        "question": "ok?",
        "kind": "confirm",
        "choices": ["yes", "no"],
    }
    data.update(kwargs)
    return UserPromptRequested(**data)


def test_auto_answer_settle_keep():
    event = _prompt(
        kind="choice",
        choices=["merge", "pr", "keep", "discard"],
        question="settle worktree?",
    )
    assert auto_answer(event) == "keep"
    assert auto_answer(event, settle="pr") == "pr"


def test_auto_answer_mcp_auth_no():
    event = _prompt(kind="mcp_auth", choices=[], question="paste token")
    assert auto_answer(event) == "no"


def test_auto_answer_turn_cap_continue():
    event = _prompt(
        kind="choice",
        choices=["continue", "handoff", "stop"],
        question="Turn budget exhausted (32/32).",
    )
    assert auto_answer(event) == "continue"


def test_auto_answer_confirm_yes():
    assert auto_answer(_prompt()) == "yes"


def test_wait_until_idle_answers_prompt_and_returns():
    events = [
        _prompt(),
        AgentStateChanged(state="thinking", turn=1, max_turns=16),
        AgentStateChanged(state="idle", turn=1, max_turns=16),
    ]
    sent = []

    async def get_event():
        if not events:
            await asyncio.sleep(30)
            raise AssertionError("get_event called after events exhausted")
        return events.pop(0)

    async def send(command):
        sent.append(command)

    async def run():
        await wait_until_idle(get_event, send, timeout=2.0, quiet_s=0.05)

    asyncio.run(run())
    assert len(sent) == 1
    assert isinstance(sent[0], AnswerPrompt)
    assert sent[0].text == "yes"
    assert sent[0].prompt_id == "p1"


def test_wait_until_idle_zero_timeout_is_unlimited():
    events = [
        AgentStateChanged(state="thinking", turn=1, max_turns=16),
        AgentStateChanged(state="idle", turn=1, max_turns=16),
    ]

    async def get_event():
        await asyncio.sleep(0.25)
        if not events:
            await asyncio.sleep(30)
            raise AssertionError("get_event called after events exhausted")
        return events.pop(0)

    async def send(command):
        raise AssertionError(f"unexpected send {command}")

    async def run():
        await wait_until_idle(get_event, send, timeout=0, quiet_s=0.05)

    asyncio.run(run())


def test_wait_until_idle_positive_timeout_still_fires():
    async def get_event():
        await asyncio.sleep(30)
        raise AssertionError("should have timed out")

    async def send(command):
        raise AssertionError(f"unexpected send {command}")

    async def run():
        with pytest.raises(HeadlessError, match="timed out"):
            await wait_until_idle(get_event, send, timeout=0.05, quiet_s=0.01)

    asyncio.run(run())


def test_wait_until_idle_waits_for_orch_after_child_finishes():
    events = [
        AgentStateChanged(state="thinking", turn=1, max_turns=16),
        AgentFinished(
            agent_id="abc",
            profile="reviewer",
            status="ok",
            summary="looks good",
        ),
        AgentStateChanged(state="idle", turn=1, max_turns=16),
    ]

    async def get_event():
        await asyncio.sleep(0.05)
        if not events:
            await asyncio.sleep(30)
            raise AssertionError("get_event called after events exhausted")
        return events.pop(0)

    async def send(command):
        raise AssertionError(f"unexpected send {command}")

    started = time.monotonic()

    async def run():
        await wait_until_idle(
            get_event,
            send,
            timeout=0,
            quiet_s=0.05,
            post_child_quiet_s=0.25,
        )

    asyncio.run(run())
    # child finish resets quiet to 0.25s; a 0.05s quiet would have returned earlier
    assert time.monotonic() - started >= 0.35


def test_drive_session_exits_when_orch_idle(tmp_path):
    async def run():
        provider = FakeProvider(
            results=[
                LLMResult(
                    text="done",
                    usage=Usage(
                        prompt_tokens=3,
                        completion_tokens=1,
                        total_tokens=4,
                        requests=1,
                    ),
                )
            ]
        )
        session = await _bind(tmp_path, provider)()
        await drive_session(session, "hello", timeout=5.0, quiet_s=0.05)
        await session.aclose()

    asyncio.run(run())
