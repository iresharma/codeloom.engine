from __future__ import annotations

import asyncio

import pytest

from pathlib import Path

from protocol.commands import AnswerPrompt, StartSession, SubmitUserMessage
from protocol.events import UserPromptRequested
from runtime.prompts import PromptBroker, PromptTimeout
from runtime.session import EngineSession
from tests.fakes import FakeProvider


def test_ask_and_answer():
    async def run():
        events = []
        broker = PromptBroker(events.append)
        task = asyncio.create_task(broker.ask("ok?"))
        await asyncio.sleep(0)
        assert isinstance(events[0], UserPromptRequested)
        assert broker.answer(events[0].prompt_id, "yes")
        assert await task == "yes"
        assert broker.pending() is None

    asyncio.run(run())


def test_unknown_id():
    async def run():
        events = []
        broker = PromptBroker(events.append)
        task = asyncio.create_task(broker.ask("ok?"))
        await asyncio.sleep(0)
        assert broker.answer("nope", "x") is False
        assert not task.done()
        broker.answer(events[0].prompt_id, "later")
        await task

    asyncio.run(run())


def test_confirm_timeout_is_no():
    async def run():
        broker = PromptBroker(lambda e: None)
        result = await broker.ask("ok?", kind="confirm", timeout=0.01)
        assert result == "no"

    asyncio.run(run())


def test_cancel_all():
    async def run():
        broker = PromptBroker(lambda e: None)
        task = asyncio.create_task(broker.ask("ok?"))
        await asyncio.sleep(0)
        broker.cancel_all()
        with pytest.raises((asyncio.CancelledError, PromptTimeout)):
            await task

    asyncio.run(run())


def test_submit_answers_pending_prompt(tmp_path):
    async def run():
        session = EngineSession(tmp_path, tmp_path / "session.db")
        await session.start()
        session._llm = FakeProvider()
        await session.handle(StartSession(workspace=str(tmp_path)))
        task = asyncio.create_task(session._prompts.ask("Allow?", kind="confirm"))
        await asyncio.sleep(0)
        await session.handle(SubmitUserMessage(text="yes"))
        assert await task == "yes"
        assert session._prompts.pending() is None

    asyncio.run(run())


def test_plain_text_answers_outstanding_prompt():
    import dummy_client

    dummy_client._LAST_PROMPT_ID = "p1"
    command = dummy_client.command_from_line("yes", Path("."))
    assert isinstance(command, AnswerPrompt)
    assert command.prompt_id == "p1"
    assert command.text == "yes"
    assert dummy_client._LAST_PROMPT_ID == ""


def test_plain_text_is_message_without_prompt():
    import dummy_client

    dummy_client._LAST_PROMPT_ID = ""
    command = dummy_client.command_from_line("yes", Path("."))
    assert isinstance(command, SubmitUserMessage)
    assert command.text == "yes"
