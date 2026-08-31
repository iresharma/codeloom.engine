from __future__ import annotations

from protocol.commands import AbortAgent, AnswerPrompt
from protocol.events import ErrorOccurred
from runtime.commands.register import handles


@handles(AbortAgent)
def abort_agent(session, command: AbortAgent) -> None:
    if not session._require_session():
        return
    if command.agent_id:
        session._emit(ErrorOccurred(message="subagents are not implemented"))
        return
    if not session.abort_turn():
        session._emit(ErrorOccurred(message="no agent turn in flight"))


@handles(AnswerPrompt)
def answer_prompt(session, command: AnswerPrompt) -> None:
    if not session._require_session():
        return
    if not session._prompts.answer(command.prompt_id, command.text):
        session._emit(ErrorOccurred(message=f"unknown prompt: {command.prompt_id}"))
