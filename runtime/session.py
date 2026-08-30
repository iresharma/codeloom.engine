from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from agents.orchestrator import Orchestrator
from llm.provider import LLMProvider
from protocol.commands import (
    AbortAgent,
    AnswerPrompt,
    CloseFile,
    Command,
    OpenFile,
    RequestSnapshot,
    Shutdown,
    StartSession,
    SubmitUserMessage,
)
from protocol.events import (
    ChatMessageAdded,
    ErrorOccurred,
    Event,
    FileChanged,
    FileContent,
    FileTreeUpdated,
    GitStateUpdated,
    SessionEnded,
    StatsUpdated,
)
from protocol.snapshot import AgentRow, EngineSnapshot, GitState, OpenFileState
from runtime.state import SessionState
from tools import build_registry
from tools.files.tree import build_file_tree
from tools.git.runner import collect_git_state
from tools.lsp.client import LspManager
from tools.paths import relative_to_workspace, resolve_workspace_path


class EngineSession:
    def __init__(self, workspace: Path, llm: LLMProvider) -> None:
        self.workspace = workspace.resolve()
        self.llm = llm
        self.registry = build_registry()
        self.lsp = LspManager()
        self.state = SessionState(workspace=str(self.workspace))
        self.orchestrator = Orchestrator(self.registry, self, llm)
        self._subscribers: list[asyncio.Queue[Event]] = []
        self._tasks: dict[str, asyncio.Task] = {}
        self._started = False

    async def start(self) -> EngineSnapshot:
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.state.workspace = str(self.workspace)
        self.track_agent(self.orchestrator.row())
        await self.refresh_workspace()
        self._started = True
        return self.snapshot()

    async def handle(self, command: Command) -> dict[str, Any]:
        if isinstance(command, StartSession):
            self.workspace = Path(command.workspace).resolve()
            self.state = SessionState(workspace=str(self.workspace))
            self.orchestrator = Orchestrator(self.registry, self, self.llm)
            snap = await self.start()
            return {"snapshot": snap.to_json()}
        if isinstance(command, SubmitUserMessage):
            await self.orchestrator.submit_user_message(command.text)
            return {}
        if isinstance(command, AnswerPrompt):
            self.orchestrator.answer_prompt(command.prompt_id, command.text)
            return {}
        if isinstance(command, OpenFile):
            await self._open_file(command.path)
            return {}
        if isinstance(command, CloseFile):
            self.state.open_files.pop(command.path, None)
            return {}
        if isinstance(command, RequestSnapshot):
            return {"snapshot": self.snapshot().to_json()}
        if isinstance(command, AbortAgent):
            await self.abort(command.agent_id)
            return {}
        if isinstance(command, Shutdown):
            await self.shutdown()
            return {}
        raise TypeError(f"unsupported command: {type(command)!r}")

    def subscribe(self) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue()
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    def snapshot(self) -> EngineSnapshot:
        self.state.workspace = str(self.workspace)
        return self.state.snapshot()

    def emit(self, event: Event) -> None:
        if isinstance(event, ChatMessageAdded):
            self.state.messages.append(event.message)
        if isinstance(event, FileChanged) and event.content is not None and event.path in self.state.open_files:
            self.state.open_files[event.path].content = event.content
        for queue in list(self._subscribers):
            queue.put_nowait(event)

    def track_agent(self, row: AgentRow) -> None:
        self.state.agents[row.id] = row

    def register_task(self, agent_id: str, task: asyncio.Task) -> None:
        self._tasks[agent_id] = task

        def _cleanup(done: asyncio.Task) -> None:
            self._tasks.pop(agent_id, None)
            if done.cancelled():
                return
            exc = done.exception()
            if exc is not None:
                self.emit(ErrorOccurred(message=str(exc), agent_id=agent_id))

        task.add_done_callback(_cleanup)

    def add_usage(self, tokens_in: int, tokens_out: int) -> None:
        self.state.tokens_in += tokens_in
        self.state.tokens_out += tokens_out
        self.emit(StatsUpdated(stats=self.state.stats()))

    async def refresh_workspace(self) -> None:
        self.state.file_tree = build_file_tree(self.workspace)
        raw = await collect_git_state(self.workspace)
        self.state.git = GitState(
            branch=str(raw["branch"]),
            dirty=bool(raw["dirty"]),
            staged_diff=str(raw["staged_diff"]),
            unstaged_diff=str(raw["unstaged_diff"]),
            status_lines=list(raw["status_lines"]),
        )
        self.emit(FileTreeUpdated(tree=self.state.file_tree))
        self.emit(GitStateUpdated(git=self.state.git))
        self.emit(StatsUpdated(stats=self.state.stats()))

    async def abort(self, agent_id: str | None = None) -> None:
        if agent_id:
            task = self._tasks.get(agent_id)
            if task and not task.done():
                task.cancel()
            if agent_id == self.orchestrator.agent_id:
                self.orchestrator.request_abort()
            return
        self.orchestrator.request_abort()
        for task in list(self._tasks.values()):
            if not task.done():
                task.cancel()

    async def shutdown(self) -> None:
        await self.abort(None)
        self.emit(SessionEnded(reason="shutdown"))
        self._started = False

    async def _open_file(self, raw: str) -> None:
        path = resolve_workspace_path(self.workspace, raw)
        if not path.is_file():
            self.emit(ErrorOccurred(message=f"file not found: {raw}"))
            return
        rel = relative_to_workspace(self.workspace, path)
        content = path.read_text(encoding="utf-8", errors="replace")
        self.state.open_files[rel] = OpenFileState(path=rel, content=content)
        self.emit(FileContent(path=rel, content=content))
