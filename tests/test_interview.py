"""Pre-turn prompter gate: ENGINE_INTERVIEW, classify_brief, cheap-model fallback."""

from __future__ import annotations

import asyncio

from llm.provider import LLMResult, ToolCall, Usage
from tests.conftest import FakeJudge, FakeVerdict
from tests.fakes import FakeProvider
from tests.test_orchestrator import _tool_names
from tests.test_session_routing import _bound, _wait_idle

GAP_VERDICT = FakeVerdict(
    nouls={
        "has_done_statement": 0.9,
        "has_material_gaps": 0.8,
        "is_already_actionable": 0.2,
        "is_conversational": 0.1,
    }
)
ACTIONABLE_VERDICT = FakeVerdict(
    nouls={
        "has_done_statement": 0.9,
        "has_material_gaps": 0.1,
        "is_already_actionable": 0.85,
        "is_conversational": 0.1,
    }
)
BRIEF = (
    "BRIEF\n"
    "Goal: add a dark-mode toggle\n"
    "Assumptions: CSS variables already exist\n"
    "Done: the header toggle switches the page between light and dark"
)
_USAGE = Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2, requests=1)


class _InterviewProvider(FakeProvider):
    """Routes classifier / prompter / orchestrator completes without a fixed queue."""

    def __init__(self, *, fallback_json: str | None = None):
        super().__init__()
        self._fallback_json = fallback_json
        self.orch_tasks: list[str] = []
        self.classify_calls: list[dict] = []
        self.prompter_turns = 0

    async def complete(self, messages, tools=None, *, on_delta=None, **kwargs):
        self.calls += 1
        names = _tool_names(tools)
        if "ask_user" in names:
            self.prompter_turns += 1
            if self.prompter_turns == 1:
                return LLMResult(
                    text="",
                    tool_calls=[
                        ToolCall(
                            id="1",
                            name="ask_user",
                            arguments_json='{"question":"What should Done look like?"}',
                        )
                    ],
                    usage=_USAGE,
                )
            return LLMResult(text=BRIEF, usage=_USAGE)
        if not names and self._fallback_json is not None:
            self.classify_calls.append(
                {
                    "model": kwargs.get("model"),
                    "temperature": kwargs.get("temperature"),
                    "messages": messages,
                }
            )
            text = self._fallback_json
            self._fallback_json = None
            return LLMResult(text=text, usage=_USAGE)
        last = ""
        for message in reversed(messages or []):
            if message.get("role") == "user":
                last = str(message.get("content") or "")
                break
        self.orch_tasks.append(last)
        return LLMResult(text="done", usage=_USAGE)


def _spy_run(session) -> list[str]:
    seen: list[str] = []
    orig = session._loop.run

    async def spy(task):
        seen.append(task)
        return await orig(task)

    session._loop.run = spy
    return seen


def _spy_ask(session, answer: str = "header toggle, CSS variables"):
    asked: list[dict] = []

    async def fake_ask(question, kind="text", **kwargs):
        asked.append({"question": question, "kind": kind, **kwargs})
        return answer

    session._prompts.ask = fake_ask
    return asked


def _brief_tags(judge: FakeJudge) -> list[dict]:
    return [call for call in judge.calls if call.get("tag") == "brief"]


def test_interview_off_sends_raw_text_and_skips_brief_judge(tmp_path):
    async def run():
        provider = _InterviewProvider()
        session = await _bound(tmp_path, provider)
        judge = FakeJudge()
        judge.responses["brief"] = GAP_VERDICT
        session._judge = judge
        tasks = _spy_run(session)
        asked = _spy_ask(session)

        session.start_turn("add a settings page")
        await _wait_idle(session)

        assert tasks == ["add a settings page"]
        assert asked == []
        assert _brief_tags(judge) == []
        assert provider.prompter_turns == 0

    asyncio.run(run())


def test_interview_on_gap_asks_and_orch_gets_done_brief(tmp_path):
    async def run():
        provider = _InterviewProvider()
        session = await _bound(tmp_path, provider, interview=True)
        judge = FakeJudge()
        judge.responses["brief"] = GAP_VERDICT
        session._judge = judge
        tasks = _spy_run(session)
        asked = _spy_ask(session)

        session.start_turn("add dark mode")
        await _wait_idle(session)

        assert _brief_tags(judge)
        assert asked
        assert asked[0]["kind"] == "interview"
        assert asked[0].get("profile") == "prompter"
        assert len(tasks) == 1
        assert "Done:" in tasks[0]
        assert tasks[0].startswith("BRIEF")
        assert provider.classify_calls == []

    asyncio.run(run())


def test_interview_on_actionable_does_not_ask(tmp_path):
    async def run():
        provider = _InterviewProvider()
        session = await _bound(tmp_path, provider, interview=True)
        judge = FakeJudge()
        judge.responses["brief"] = ACTIONABLE_VERDICT
        session._judge = judge
        tasks = _spy_run(session)
        asked = _spy_ask(session)

        session.start_turn("add a dark-mode toggle; Done: the header switch works")
        await _wait_idle(session)

        assert _brief_tags(judge)
        assert asked == []
        assert tasks == ["add a dark-mode toggle; Done: the header switch works"]
        assert provider.prompter_turns == 0

    asyncio.run(run())


def test_judge_none_fallback_json_true_interviews(tmp_path):
    async def run():
        provider = _InterviewProvider(fallback_json='{"interview": true}')
        session = await _bound(
            tmp_path, provider, interview=True, model_cheap="test/cheap"
        )
        session._judge = FakeJudge()
        tasks = _spy_run(session)
        asked = _spy_ask(session)

        session.start_turn("make it nicer")
        await _wait_idle(session)

        assert provider.classify_calls
        assert provider.classify_calls[0]["temperature"] == 0
        assert provider.classify_calls[0]["model"] == "test/cheap"
        assert asked and asked[0]["kind"] == "interview"
        assert "Done:" in tasks[0]

    asyncio.run(run())


def test_judge_none_fallback_json_false_passes_through(tmp_path):
    async def run():
        provider = _InterviewProvider(fallback_json='{"interview": false}')
        session = await _bound(
            tmp_path, provider, interview=True, model_cheap="test/cheap"
        )
        session._judge = FakeJudge()
        tasks = _spy_run(session)
        asked = _spy_ask(session)

        session.start_turn("add a settings page")
        await _wait_idle(session)

        assert provider.classify_calls
        assert provider.classify_calls[0]["temperature"] == 0
        assert asked == []
        assert tasks == ["add a settings page"]
        assert provider.prompter_turns == 0

    asyncio.run(run())


def test_inbox_text_is_never_gated(tmp_path):
    async def run():
        provider = _InterviewProvider()
        session = await _bound(tmp_path, provider, interview=True)
        judge = FakeJudge()
        judge.responses["brief"] = GAP_VERDICT
        session._judge = judge
        tasks = _spy_run(session)
        asked = _spy_ask(session)
        report = "[agent ask ed8e4551 finished]\nfound retry in server.py"

        session.start_turn(report)
        await _wait_idle(session)

        assert _brief_tags(judge) == []
        assert asked == []
        assert tasks == [report]
        assert provider.prompter_turns == 0

        asked.clear()
        tasks.clear()
        session._inbox.append("[worktree coder b03318d3 pr]\nopened pull request")
        session._maybe_pump()
        await _wait_idle(session)

        assert _brief_tags(judge) == []
        assert asked == []
        assert tasks == ["[worktree coder b03318d3 pr]\nopened pull request"]

    asyncio.run(run())
