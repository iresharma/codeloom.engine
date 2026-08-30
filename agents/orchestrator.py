from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents.agent_loop import AgentLoop, AgentResult
from agents.compressor import ConversationCompressor
from agents.profile import ProfileRegistry, built_in_registry
from agents.subagent import Subagent
from llm.provider import LLMProvider, Message
from protocol.events import (
    AgentFinished,
    AgentStarted,
    ChatMessageAdded,
    ContextFileUpdated,
    UserPromptRequested,
)
from protocol.snapshot import ChatMessage, PendingPrompt
from tools import ORCHESTRATOR_TOOL_NAMES
from tools.registry import ToolRegistry

ORCHESTRATOR_SYSTEM = """You are the orchestrator for an agentic coding engine.
You may talk to the user, inspect the workspace, and spawn named subagents.

Available subagent profiles:
- ask: read-only questions about the repo
- linter: find issues, no edits
- editor: make code changes
- reviewer: review diffs, no edits

Use spawn_subagent with a profile_name and a focused task. Subagents cannot ask the user anything.
When a subagent returns, you receive a compressed summary. Write lasting decisions with write_context.
If you need a human decision, stop and wait — the engine will surface a user prompt when you call for it.
Prefer spawning specialists over doing large edits yourself.
"""


class Orchestrator(AgentLoop):
    def __init__(
        self,
        tools: ToolRegistry,
        session: Any,
        llm: LLMProvider,
        profiles: ProfileRegistry | None = None,
        *,
        agent_id: str = "orchestrator",
    ) -> None:
        super().__init__(
            profile=None,
            tools=tools,
            session=session,
            llm=llm,
            agent_id=agent_id,
            role="orchestrator",
            system_prompt=ORCHESTRATOR_SYSTEM,
            tool_names=ORCHESTRATOR_TOOL_NAMES,
            max_turns=80,
            model=getattr(llm, "default_model", "gpt-4o"),
        )
        self.profiles = profiles or built_in_registry()
        self.compressor = ConversationCompressor(llm)
        self._run_task: asyncio.Task | None = None
        self._pending: dict[str, asyncio.Future[str]] = {}
        self._sub_tasks: dict[str, asyncio.Task] = {}

    @property
    def workspace(self) -> Path:
        return self.session.workspace

    def context_path(self) -> Path:
        return self.workspace / ".engine" / "context.md"

    def _build_messages(self) -> list[Message]:
        context = ""
        path = self.context_path()
        if path.is_file():
            context = path.read_text(encoding="utf-8", errors="replace")
        system = self.system_prompt
        if context.strip():
            system = f"{system}\n\n# Long-term context\n\n{context}"
        built = [Message(role="system", content=system)]
        built.extend(self.messages)
        return built

    async def submit_user_message(self, text: str) -> None:
        if self._run_task and not self._run_task.done():
            self.messages.append(Message(role="user", content=text))
            self._emit(
                ChatMessageAdded(
                    message=ChatMessage(role="user", content=text, agent_id=self.agent_id)
                )
            )
            return
        self._run_task = asyncio.create_task(self.run(text), name="orchestrator-run")
        self.session.register_task(self.agent_id, self._run_task)

    async def request_user_input(self, question: str) -> str:
        prompt_id = uuid.uuid4().hex[:12]
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._pending[prompt_id] = future
        prompt = PendingPrompt(id=prompt_id, question=question)
        self.session.state.pending_prompt = prompt
        self.status = "waiting_user"
        self._emit_agent()
        self._emit(UserPromptRequested(prompt=prompt))
        try:
            return await future
        finally:
            self._pending.pop(prompt_id, None)
            if self.session.state.pending_prompt and self.session.state.pending_prompt.id == prompt_id:
                self.session.state.pending_prompt = None
            self.status = "running"
            self._emit_agent()

    def answer_prompt(self, prompt_id: str, text: str) -> None:
        future = self._pending.get(prompt_id)
        if future is None or future.done():
            raise KeyError(f"unknown or completed prompt: {prompt_id}")
        future.set_result(text)
        self.messages.append(Message(role="user", content=text))
        self._emit(
            ChatMessageAdded(
                message=ChatMessage(role="user", content=text, agent_id=self.agent_id)
            )
        )

    async def spawn_subagent(self, profile_name: str, prompt: str) -> str:
        profile = self.profiles.get(profile_name)
        agent_id = f"sub-{uuid.uuid4().hex[:8]}"
        sub = Subagent(
            profile,
            self.registry,
            self.session,
            self.llm,
            agent_id=agent_id,
            parent_id=self.agent_id,
        )
        self.session.track_agent(sub.row())
        self._emit(AgentStarted(agent=sub.row()))
        task = asyncio.create_task(self._run_subagent(sub, prompt), name=agent_id)
        self._sub_tasks[agent_id] = task
        self.session.register_task(agent_id, task)
        return agent_id

    async def _run_subagent(self, sub: Subagent, prompt: str) -> None:
        try:
            result = await sub.run(prompt)
        except asyncio.CancelledError:
            result = AgentResult(status="aborted", final_text="", messages=list(sub.messages))
        sub.finish(result)
        await self.on_subagent_done(sub.agent_id, result)

    async def on_subagent_done(self, agent_id: str, result: AgentResult) -> None:
        self._sub_tasks.pop(agent_id, None)
        compressed = await self.compressor.compress(result.messages)
        files = compressed.files_touched or result.files_touched
        note = (
            f"subagent {agent_id} ({compressed.outcome})\n"
            f"{compressed.summary}\n"
            f"files: {', '.join(files) or 'none'}"
        )
        self.write_context(note)
        summary = (
            f"[subagent {agent_id} {result.status}]\n"
            f"{compressed.summary}\n"
            f"outcome: {compressed.outcome}\n"
            f"files: {', '.join(files) or 'none'}"
        )
        if compressed.leftover_questions:
            summary += "\nquestions: " + "; ".join(compressed.leftover_questions)
        self.messages.append(Message(role="user", content=summary))
        agent = self.session.state.agents.get(agent_id)
        if agent is not None:
            agent.status = result.status
            agent.current_tool = None
            self._emit(AgentFinished(agent=agent, final_text=result.final_text))

    def write_context(self, note: str) -> None:
        path = self.context_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n## {stamp}\n\n{note.strip()}\n")
        content = path.read_text(encoding="utf-8", errors="replace")
        self._emit(ContextFileUpdated(path=str(path), content=content))
