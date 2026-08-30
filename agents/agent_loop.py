from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Literal

from agents.profile import AgentProfile
from llm.provider import LLMProvider, LLMResponse, Message, ToolCall
from protocol.events import AgentUpdated, ChatMessageAdded, ErrorOccurred
from protocol.snapshot import AgentRow, ChatMessage
from tools.base import ToolContext, ToolResult
from tools.registry import ToolRegistry


@dataclass
class AgentResult:
    status: str
    final_text: str
    files_touched: list[str] = field(default_factory=list)
    tool_trace: list[str] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)


@dataclass
class StepOutcome:
    done: bool
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


class AgentLoop:
    def __init__(
        self,
        profile: AgentProfile | None,
        tools: ToolRegistry | list,
        session: Any,
        llm: LLMProvider,
        *,
        agent_id: str,
        role: Literal["orchestrator", "subagent"] = "subagent",
        system_prompt: str | None = None,
        tool_names: list[str] | None = None,
        max_turns: int | None = None,
        model: str | None = None,
        temperature: float | None = None,
        parent_id: str | None = None,
    ) -> None:
        self.profile = profile
        self.session = session
        self.llm = llm
        self.agent_id = agent_id
        self.role = role
        self.parent_id = parent_id
        if isinstance(tools, ToolRegistry):
            self.registry = tools
        else:
            registry = ToolRegistry()
            for tool in tools:
                registry.register(tool)
            self.registry = registry
        self.system_prompt = system_prompt or (profile.system_prompt if profile else "")
        self.tool_names = tool_names or (profile.tool_names if profile else [])
        self.max_turns = max_turns if max_turns is not None else (profile.max_turns if profile else 40)
        self.model = model or (profile.model if profile else getattr(llm, "default_model", "gpt-4o"))
        self.temperature = (
            temperature if temperature is not None else (profile.temperature if profile else 0.2)
        )
        self.messages: list[Message] = []
        self.files_touched: list[str] = []
        self.tool_trace: list[str] = []
        self.turn = 0
        self.status = "idle"
        self.current_tool: str | None = None
        self._abort = asyncio.Event()

    @property
    def profile_name(self) -> str | None:
        return self.profile.name if self.profile else None

    def row(self) -> AgentRow:
        return AgentRow(
            id=self.agent_id,
            role=self.role,
            profile=self.profile_name,
            status=self.status,
            current_tool=self.current_tool,
            parent_id=self.parent_id,
        )

    def request_abort(self) -> None:
        self._abort.set()

    async def run(self, task: str) -> AgentResult:
        self.status = "running"
        self._emit_agent()
        if task:
            self.messages.append(Message(role="user", content=task))
            if self.role == "orchestrator":
                self._emit(
                    ChatMessageAdded(
                        message=ChatMessage(
                            role="user",
                            content=task,
                            agent_id=self.agent_id,
                        )
                    )
                )
        final_text = ""
        try:
            while not self._should_stop():
                outcome = await self.step()
                if outcome.text:
                    final_text = outcome.text
                if outcome.done:
                    break
        except asyncio.CancelledError:
            self.status = "aborted"
            self._emit_agent()
            return AgentResult(
                status="aborted",
                final_text=final_text,
                files_touched=list(dict.fromkeys(self.files_touched)),
                tool_trace=list(self.tool_trace),
                messages=list(self.messages),
            )
        except Exception as exc:
            self.status = "error"
            self._emit(ErrorOccurred(message=str(exc), agent_id=self.agent_id))
            self._emit_agent()
            return AgentResult(
                status="error",
                final_text=str(exc),
                files_touched=list(dict.fromkeys(self.files_touched)),
                tool_trace=list(self.tool_trace),
                messages=list(self.messages),
            )
        self.status = "finished"
        self.current_tool = None
        self._emit_agent()
        return AgentResult(
            status="finished",
            final_text=final_text,
            files_touched=list(dict.fromkeys(self.files_touched)),
            tool_trace=list(self.tool_trace),
            messages=list(self.messages),
        )

    async def step(self) -> StepOutcome:
        if self._abort.is_set():
            return StepOutcome(done=True, text="")
        self.turn += 1
        self.status = "running"
        self.current_tool = None
        self._emit_agent()
        response = await self._invoke_model()
        if response.usage and self.session is not None:
            self.session.add_usage(response.usage.input_tokens, response.usage.output_tokens)
        assistant = Message(
            role="assistant",
            content=response.text,
            tool_calls=response.tool_calls or None,
        )
        self.messages.append(assistant)
        if self.role == "orchestrator" and response.text and not response.tool_calls:
            self._emit(
                ChatMessageAdded(
                    message=ChatMessage(
                        role="assistant",
                        content=response.text,
                        agent_id=self.agent_id,
                        profile=self.profile_name,
                    )
                )
            )
        if response.tool_calls:
            await self._dispatch_tools(response.tool_calls)
            return StepOutcome(done=False, text=response.text, tool_calls=response.tool_calls)
        return StepOutcome(done=True, text=response.text)

    def _build_messages(self) -> list[Message]:
        built = [Message(role="system", content=self.system_prompt)]
        built.extend(self.messages)
        return built

    async def _invoke_model(self) -> LLMResponse:
        specs = self.registry.specs_for(self.role, self.tool_names)
        return await self.llm.complete(
            self._build_messages(),
            specs,
            model=self.model,
            temperature=self.temperature,
        )

    async def _dispatch_tools(self, calls: list[ToolCall]) -> list[ToolResult]:
        results: list[ToolResult] = []
        ctx = ToolContext(
            workspace=self.session.workspace,
            agent_id=self.agent_id,
            role=self.role,
            emit=self.session.emit,
            orchestrator=getattr(self.session, "orchestrator", None),
            session=self.session,
            refresh=self.session.refresh_workspace,
        )
        for call in calls:
            self.current_tool = call.name
            self.status = "running"
            self._emit_agent()
            if self.tool_names and call.name not in self.tool_names:
                result = ToolResult(
                    ok=False,
                    content=f"tool {call.name!r} is not on this agent's allowlist",
                )
            else:
                result = await self.registry.execute(call.name, ctx, call.arguments)
            results.append(result)
            self.tool_trace.append(call.name)
            self.files_touched.extend(result.files_touched)
            self.messages.append(
                Message(
                    role="tool",
                    name=call.name,
                    tool_call_id=call.id,
                    content=result.content,
                )
            )
        self.current_tool = None
        self._emit_agent()
        return results

    def _should_stop(self) -> bool:
        return self._abort.is_set() or self.turn >= self.max_turns

    def _emit(self, event: Any) -> None:
        if self.session is not None:
            self.session.emit(event)

    def _emit_agent(self) -> None:
        if self.session is not None:
            self.session.track_agent(self.row())
            self._emit(AgentUpdated(agent=self.row()))
