from __future__ import annotations

import asyncio
import inspect
import threading
import time
from contextlib import suppress
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from agents.hooks import AgentHooks
from agents.orchestrator import Orchestrator
from agents.profile import discover_profiles
from llm.openrouter import OpenRouterLLM
from llm.provider import Usage
from protocol.commands import Command
from protocol.events import (
    AgentFinished,
    AgentStarted,
    AgentStateChanged,
    AgentsUpdated,
    ChatHistoryAdded,
    ChatHistoryComplete,
    ChatMessageAdded,
    ChatMessageDelta,
    ChatMessageStarted,
    CommandOutputChunk,
    ContextCompacted,
    ErrorOccurred,
    Event,
    FileContent,
    FileEdited,
    FileTreeUpdated,
    McpAuthRequired,
    McpServersUpdated,
    SessionEnded,
    SkillActivated,
    SkillCatalogUpdated,
    SnapshotReady,
    StatsUpdated,
    ToolCallFinished,
    ToolCallStarted,
    WarningOccurred,
    WorktreeSettled,
)
from protocol.snapshot import AgentRow, ChatMessage, EngineSnapshot, GitState, Stats
from runtime.commands import HANDLERS
from runtime.config import EngineConfig
from runtime.language import LanguageInfo
from runtime.language import detect as detect_language
from runtime.prompts import PromptBroker
from runtime.store import SessionState
from runtime.store.sqlite import init as init_store
from runtime.store.sqlite import save as save_snapshot
from runtime.subscriber import EVENT_SOFT_LIMIT, Subscriber, clip_text
from runtime.tools.fs import WorkspacePathError, list_tree, read_text
from runtime.tools.git import read_state as read_git
from runtime.tools.lsp import LSPManager, LSPTimeoutError
from runtime.mcp.config import load_mcp_config, load_trust, save_trust
from runtime.mcp.manager import McpManager
from runtime.mcp.tokens import apply_tokens, load_tokens, save_token
from runtime.skills.catalog import SkillCatalog
from runtime.skills.discover import discover_skills
from runtime.tools.tracker import FileTracker
from tools.registry import discover_tools


class EngineSession:
    def __init__(self, workspace: Path, db_path: Path):
        self._workspace = workspace.resolve()
        self._db_path = db_path
        self._state = SessionState()
        self._subscribers: list[Subscriber] = []
        self._config = EngineConfig.from_env(self._workspace)
        self._llm: OpenRouterLLM | None = None
        self._loop: Orchestrator | None = None
        self._lsp: LSPManager | None = None
        self._files = FileTracker()
        self._history_task: asyncio.Task | None = None
        self._history_generation = 0
        self._turn_task: asyncio.Task | None = None
        self._pending_user: list[str] = []
        self._inbox: list[str] = []
        self._aborting = False
        self._turn_started = 0.0
        self._live_procs: set = set()
        self._write_lock: asyncio.Lock | None = None
        self._prompts = PromptBroker(
            self._emit, self._on_prompt_state, self._set_pending_prompt
        )
        self._last_call_id = ""
        self._tool_started: dict[str, float] = {}
        self._stream_id = ""
        self._streamed_ids: set[str] = set()
        self.language: LanguageInfo = detect_language(self._workspace)
        self._mcp: McpManager | None = None
        self._skills: SkillCatalog | None = None
        self._mcp_connect = None
        self._mcp_backoff = (2.0, 4.0, 8.0)
        self._mcp_cool_s = 30.0
        self._auth_tasks: list[asyncio.Task] = []
        self._registry = None
        try:
            self._llm = OpenRouterLLM.from_env(self._workspace, config=self._config)
        except RuntimeError:
            self._llm = None

    async def start(self) -> None:
        init_store(self._db_path)

    async def handle(self, command: Command) -> None:
        fn = HANDLERS.get(type(command))
        if fn is None:
            self._emit(
                ErrorOccurred(message=f"unknown command: {type(command).__name__}")
            )
            return
        result = fn(self, command)
        if inspect.isawaitable(result):
            await result

    def subscribe(self) -> Subscriber:
        queue = Subscriber(
            capacity=self._config.subscriber_capacity,
            max_bytes=self._config.subscriber_bytes,
        )
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: Subscriber) -> None:
        try:
            self._subscribers.remove(queue)
        except ValueError:
            pass

    def snapshot(self) -> EngineSnapshot:
        tree = list_tree(self._workspace)
        snap = self._state.snapshot(
            str(self._workspace),
            tree,
            read_git(self._workspace, diffs=False),
            language=self.language.name,
            language_supported=self.language.supported,
        )
        if self._mcp is not None:
            snap.mcp_servers = self._mcp.rows()
        if self._skills is not None:
            snap.skills = self._skills.rows()
        return snap

    def shutdown(self) -> None:
        if not self.close_session():
            self._emit(ErrorOccurred(message="no active session; start one first"))

    async def aclose(self) -> None:
        self.abort_turn()
        task = self._turn_task
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        if isinstance(self._loop, Orchestrator):
            self._loop.abort_all_children()
            self._kill_live_procs()
            await self._loop.wait_children()
            await self._loop.wait_settle()
            self._loop.cleanup_worktrees()
        self._prompts.cancel_all()
        for task in list(self._auth_tasks):
            task.cancel()
        if self._auth_tasks:
            await asyncio.gather(*self._auth_tasks, return_exceptions=True)
        self._auth_tasks.clear()
        if self._mcp is not None:
            await self._mcp.aclose()
            self._mcp = None
        self.close_session()

    def close_session(self) -> bool:
        if self._state.session_id is None:
            return False
        self.abort_turn()
        if isinstance(self._loop, Orchestrator):
            self._loop.abort_all_children()
            self._prompts.cancel_all()
            self._kill_live_procs()
        self._cancel_history_replay()
        self._state.ended = True
        self._persist()
        self._emit(SessionEnded(reason="shutdown"))
        self._state = SessionState()
        self._loop = None
        self._pending_user = []
        self._inbox = []
        self._stop_lsp()
        mcp = self._mcp
        self._mcp = None
        if mcp is not None:
            with suppress(RuntimeError):
                loop = asyncio.get_running_loop()
                loop.create_task(mcp.aclose())
        return True

    def emit_error(self, message: str) -> None:
        self._emit(ErrorOccurred(message=message))

    def start_turn(self, text: str) -> None:
        self._add_message(role="user", text=text)
        self._persist()
        if self._loop is not None:
            self._loop.set_catalog_query(text)
        if self._turn_task is not None and not self._turn_task.done():
            self._pending_user.append(text)
            return
        self._begin_turn(text)

    def _begin_turn(self, text: str) -> None:
        self._state.stats.last_turn_tokens = 0
        self._state.stats.last_turn_cost = 0.0
        self._aborting = False
        self._turn_started = time.monotonic()
        self._turn_task = asyncio.get_running_loop().create_task(self._run_turn(text))

    def _maybe_pump(self) -> None:
        if self._turn_task is not None and not self._turn_task.done():
            return
        if self._state.ended or self._state.session_id is None:
            return
        if self._pending_user:
            self._begin_turn(self._pending_user.pop(0))
            return
        if self._inbox:
            report = "\n\n".join(self._inbox)
            self._inbox.clear()
            self._begin_turn(report)

    async def _run_turn(self, text: str) -> None:
        # CancelledError is swallowed: this task is the cancellation
        # boundary. After an abort, task.cancelled() is False and
        # exception() is None. Use self._aborting, not the task flags.
        try:
            reply = await self._loop.run(text)
            if not self._aborting:
                # Same id as ChatMessageStarted/Delta so clients that
                # already rendered the stream do not reprint the text.
                self._add_message(
                    role="assistant",
                    text=reply,
                    message_id=self._stream_id or None,
                )
        except asyncio.CancelledError:
            self._add_message(role="assistant", text="(aborted by the user)")
        except Exception as exc:  # noqa: BLE001
            self._emit(ErrorOccurred(message=f"llm error: {exc}"))
        finally:
            self._state.stats.elapsed_s += max(0.0, time.monotonic() - self._turn_started)
            self._persist()
            self._turn_task = None
            self._aborting = False
            self._emit_stats()
            self._on_state("idle", 0)
            self._maybe_pump()
            self._flush_settles_if_idle()

    def _flush_settles_if_idle(self) -> None:
        if self._turn_task is not None and not self._turn_task.done():
            return
        orch = self._loop
        if not isinstance(orch, Orchestrator):
            return
        if orch.has_live_children():
            return
        orch.schedule_flush_settles()

    def abort_turn(self) -> bool:
        task = self._turn_task
        if task is None or task.done():
            return False
        self._aborting = True
        self._on_state("aborting", 0)
        # abort_turn returning True means cancellation was requested, not
        # that the turn has already stopped. Children keep running.
        task.cancel()
        return True

    def abort_child(self, agent_id: str) -> bool:
        if not isinstance(self._loop, Orchestrator):
            return False
        self._prompts.cancel_agent(agent_id)
        return self._loop.abort_child(agent_id)

    def _kill_live_procs(self) -> None:
        for proc in list(self._live_procs):
            with suppress(ProcessLookupError, OSError):
                import os
                import signal

                pid = getattr(proc, "pid", None)
                if pid is not None:
                    os.killpg(os.getpgid(pid), signal.SIGTERM)

    def _ensure_write_lock(self) -> asyncio.Lock:
        if self._write_lock is None:
            self._write_lock = asyncio.Lock()
        return self._write_lock

    async def _bind_loop(self) -> None:
        registry = discover_tools()
        for message in registry.errors:
            self._emit(ErrorOccurred(message=message))
        profiles = discover_profiles()
        for message in profiles.errors:
            self._emit(ErrorOccurred(message=message))
        for warning in self._config.warnings:
            self._emit(WarningOccurred(message=warning))
        self._skills = SkillCatalog(discover_skills(self._workspace))
        self._emit(SkillCatalogUpdated(skills=self._skills.rows()))
        await self._start_mcp(registry)
        self._registry = registry
        if self._llm is None:
            self._loop = None
            return
        self._start_lsp()
        self._files = FileTracker()
        self._state.agents = []
        orch_hooks = self._hooks_for("", stream_chat=True)

        def child_hooks(agent_id: str, profile: str) -> AgentHooks:
            return self._hooks_for(agent_id, stream_chat=False)

        self._loop = Orchestrator(
            self._llm,
            all_tools=registry,
            profiles=profiles,
            spawn_budget=self._config.max_spawns_per_turn,
            make_child_hooks=child_hooks,
            make_child_lsp=self._make_child_lsp,
            on_agent_started=self._on_agent_started,
            on_agent_finished=self._on_agent_finished,
            on_agent_result=self._on_agent_result,
            on_worktree_settled=self._on_worktree_settled,
            child_ask_user=self._prompts.ask,
            child_on_output=self._on_command_output,
            child_on_edit=self._on_edit,
            child_on_proc=self._on_proc,
            write_lock=self._ensure_write_lock(),
            workspace=self._workspace,
            hooks=orch_hooks,
            language=self.language,
            lsp=self._lsp,
            files=self._files,
            journal=self._db_path,
            session_id=self._state.session_id,
            on_edit=self._on_edit,
            config=self._config,
            ask_user=self._prompts.ask,
            on_output=self._on_command_output,
            on_proc=self._on_proc,
            skills=self._skills,
            on_skill_activated=self._on_skill_activated,
        )
        self._loop.hydrate(self._state.messages)

    async def _start_mcp(self, registry) -> None:
        if self._mcp is not None:
            await self._mcp.aclose()
        trust = load_trust(self._workspace)
        configs, warnings, untrusted = load_mcp_config(self._workspace)
        for message in warnings:
            self._emit(WarningOccurred(message=message))
        if untrusted:
            names = ", ".join(untrusted)
            self._emit(
                WarningOccurred(
                    message=f"imported MCP servers from .cursor/mcp.json: {names}"
                )
            )
            answer = await self._prompts.ask(
                f"Start imported Cursor MCP servers ({names})?",
                kind="confirm",
                choices=["yes", "no"],
                default="no",
            )
            if str(answer).strip().lower() in {"yes", "y"}:
                trusted = list(dict.fromkeys(list(trust.get("cursor") or []) + untrusted))
                save_trust(self._workspace, trusted, trust.get("refused") or [])
                configs, _, _ = load_mcp_config(
                    self._workspace, trust_cursor=trusted
                )
            else:
                refused = list(dict.fromkeys(list(trust.get("refused") or []) + untrusted))
                save_trust(self._workspace, list(trust.get("cursor") or []), refused)
        apply_tokens(configs, load_tokens(self._workspace))
        manager = McpManager(
            self._workspace,
            connect=self._mcp_connect,
            backoff_s=self._mcp_backoff,
            cool_s=self._mcp_cool_s,
            ask_user=self._prompts.ask,
            on_update=lambda rows: self._emit(McpServersUpdated(servers=rows)),
            on_auth=self._on_mcp_auth,
            on_warning=lambda message: self._emit(WarningOccurred(message=message)),
            approval=self._config.exec_approval,
        )
        self._mcp = manager
        await manager.start(configs)
        self._install_mcp_tools(registry)

    def _install_mcp_tools(self, registry) -> None:
        if registry is None or self._mcp is None:
            return
        registry.drop_family("mcp")
        for spec in self._mcp.tools():
            registry.register(spec)
        for message in self._mcp.warnings:
            if "collision" in message:
                self._emit(WarningOccurred(message=message))

    async def _on_mcp_auth(self, server: str, url: str, first: bool, token_env: str | None):
        cfg = None
        if self._mcp is not None and server in self._mcp.servers:
            cfg = self._mcp.servers[server].config
            token_env = token_env or cfg.token_env
        if not token_env:
            self._emit(McpAuthRequired(server=server, url=url or "", prompt_id=""))
            self._emit(
                WarningOccurred(
                    message=(
                        f"add tokenEnv to mcp.json for {server} or set the secret "
                        "in env.sh and ReloadIntegrations"
                    )
                )
            )
            return
        question = (
            f"Open {url} and paste the token for MCP server {server}"
            if url
            else (
                f"Paste a token for MCP server {server} (CompleteMcpAuth). "
                + ("never authed" if first else "token rejected; CompleteMcpAuth")
            )
        )

        def _created(prompt_id: str) -> None:
            self._emit(
                McpAuthRequired(server=server, url=url or "", prompt_id=prompt_id)
            )

        async def _ask() -> None:
            token = await self._prompts.ask(
                question,
                kind="mcp_auth",
                on_created=_created,
            )
            await self.complete_mcp_auth(server, token)

        try:
            loop = asyncio.get_running_loop()
            self._auth_tasks.append(loop.create_task(_ask()))
        except RuntimeError:
            self._emit(McpAuthRequired(server=server, url=url or "", prompt_id=""))

    def _on_skill_activated(self, name: str, agent_id: str) -> None:
        self._emit(SkillActivated(name=name, agent_id=agent_id or ""))

    async def reload_integrations(self) -> None:
        if self._mcp is not None:
            self._mcp.clear_cooling()
            await self._mcp.stop()
        registry = self._registry
        if registry is None:
            registry = discover_tools()
            self._registry = registry
        self._skills = SkillCatalog(discover_skills(self._workspace))
        self._emit(SkillCatalogUpdated(skills=self._skills.rows()))
        if self._loop is not None:
            self._loop._skills = self._skills
            self._loop._ctx.skills = self._skills
        await self._start_mcp(registry)
        if isinstance(self._loop, Orchestrator):
            self._loop._all_tools = registry

    async def complete_mcp_auth(self, server: str, token: str) -> None:
        token_env = ""
        if self._mcp is not None and server in self._mcp.servers:
            token_env = self._mcp.servers[server].config.token_env or ""
        if not token_env:
            self._emit(
                WarningOccurred(
                    message=f"add tokenEnv to mcp.json for {server} before pasting a token"
                )
            )
            return
        save_token(self._workspace, server, token, token_env)
        if self._mcp is not None and server in self._mcp.servers:
            self._mcp.servers[server].config.env[token_env] = token
            self._mcp.servers[server].config.token_env = token_env
            await self._mcp.restart(server)
            self._install_mcp_tools(self._registry)
            self._emit(McpServersUpdated(servers=self._mcp.rows()))

    def set_mcp_enabled(self, name: str, enabled: bool) -> None:
        import json

        path = self._workspace / ".engine" / "mcp.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {}
        servers = data.setdefault("mcpServers", {})
        entry = servers.get(name) or {}
        if not isinstance(entry, dict):
            entry = {}
        entry["enabled"] = bool(enabled)
        servers[name] = entry
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        if self._mcp is not None and name in self._mcp.servers:
            self._mcp.servers[name].config.enabled = bool(enabled)

    def unlock_skill(self, name: str) -> bool:
        if self._loop is None or self._skills is None:
            return False
        if self._skills.get(name) is None:
            return False
        return self._loop.unlock_skill(name)

    def _hooks_for(self, agent_id: str, *, stream_chat: bool) -> AgentHooks:
        return AgentHooks(
            on_tool=lambda call_id, name, arguments, result: self._on_tool(
                call_id, name, arguments, result, agent_id=agent_id
            ),
            on_tool_start=lambda call_id, name, arguments: self._on_tool_start(
                call_id, name, arguments, agent_id=agent_id
            ),
            on_delta=self._on_delta if stream_chat else None,
            on_message_start=self._on_message_start if stream_chat else None,
            on_usage=self._on_usage,
            on_state=lambda state, turn, max_turns: self._on_state(
                state, turn, max_turns, agent_id=agent_id
            ),
            on_compact=lambda info: self._on_compact(info, agent_id=agent_id),
        )

    def _set_pending_prompt(self, prompt) -> None:
        self._state.pending_prompt = prompt

    def _on_prompt_state(self, state: str) -> None:
        self._state.pending_prompt = self._prompts.pending()
        agent_id = ""
        if self._state.pending_prompt is not None:
            agent_id = self._state.pending_prompt.agent_id
        self._on_state(state, 0, agent_id=agent_id)

    def _on_proc(self, proc, register: bool) -> None:
        if register:
            self._live_procs.add(proc)
        else:
            self._live_procs.discard(proc)

    def _on_command_output(
        self, call_id: str, stream: str, text: str, agent_id: str = ""
    ) -> None:
        self._emit(
            CommandOutputChunk(
                call_id=call_id or "",
                stream=stream,
                text=text,
                agent_id=agent_id,
            )
        )

    def _start_lsp(self) -> None:
        self._stop_lsp()
        if not self.language.supported:
            return
        self._lsp = LSPManager(self._workspace)
        threading.Thread(
            target=self._warm_lsp,
            daemon=True,
            name="lsp-warm-start",
        ).start()

    def _warm_lsp(self) -> None:
        manager = self._lsp
        name = self.language.name
        if manager is None or not name:
            return
        with suppress(OSError, RuntimeError, ValueError, LSPTimeoutError):
            manager.warm_start(name)

    def _stop_lsp(self) -> None:
        if self._lsp is None:
            return
        with suppress(OSError, RuntimeError, LSPTimeoutError):
            self._lsp.shutdown_all()
        self._lsp = None

    def _on_edit(self, path: str, diff: str, tool: str, edit_id) -> None:
        clipped, _ = clip_text(diff, EVENT_SOFT_LIMIT)
        self._emit(
            FileEdited(
                path=path,
                diff=clipped,
                tool=tool,
                edit_id=str(edit_id) if edit_id is not None else "",
            )
        )
        if path in self._state.open_files:
            self._emit_file_content(path)
        if tool in {"create_file", "undo_edit"}:
            self._emit_tree()

    def _on_tool_start(
        self, call_id: str, name: str, arguments: dict, agent_id: str = ""
    ) -> None:
        self._tool_started[call_id] = time.monotonic()
        self._last_call_id = call_id
        import json

        self._emit(
            ToolCallStarted(
                call_id=call_id,
                name=name,
                arguments_json=json.dumps(arguments),
                agent_id=agent_id,
            )
        )
        self._touch_agent(agent_id, status="calling_tool", current_tool=name)

    def _on_tool(
        self,
        call_id: str,
        name: str,
        arguments: dict,
        result: str,
        agent_id: str = "",
    ) -> None:
        preview = result if len(result) <= 400 else result[:400] + "…"
        started = self._tool_started.pop(call_id, 0)
        duration = int((time.monotonic() - started) * 1000) if started else 0
        self._emit(
            ToolCallFinished(
                call_id=call_id,
                name=name,
                preview=preview,
                ok=not str(result).startswith("error:"),
                duration_ms=duration,
                agent_id=agent_id,
            )
        )
        self._state.stats.tool_calls += 1
        self._touch_agent(agent_id, current_tool="")

    def _on_message_start(self, message_id: str) -> None:
        self._stream_id = message_id

    def _on_delta(self, message_id: str, channel: str, text: str) -> None:
        if channel == "text" and message_id not in self._streamed_ids:
            self._emit(
                ChatMessageStarted(
                    id=message_id,
                    role="assistant",
                    ts=datetime.now(timezone.utc).isoformat(),
                )
            )
            self._streamed_ids.add(message_id)
        self._emit(ChatMessageDelta(id=message_id, channel=channel, text=text))

    def _on_usage(self, usage: Usage) -> None:
        stats = self._state.stats
        stats.prompt_tokens += usage.prompt_tokens
        stats.completion_tokens += usage.completion_tokens
        stats.reasoning_tokens += usage.reasoning_tokens
        stats.cached_tokens += usage.cached_tokens
        stats.total_tokens += usage.total_tokens
        stats.cost += usage.cost
        stats.requests += usage.requests or 1
        stats.turns += 1
        stats.last_turn_tokens += usage.total_tokens
        stats.last_turn_cost += usage.cost
        self._emit_stats()

    def _on_state(
        self,
        state: str,
        turn: int = 0,
        max_turns: int | None = None,
        agent_id: str = "",
    ) -> None:
        self._emit(
            AgentStateChanged(
                state=state,
                turn=turn,
                max_turns=self._config.max_turns if max_turns is None else max_turns,
                agent_id=agent_id,
            )
        )
        self._touch_agent(agent_id, status=state)

    def _on_compact(self, info: dict, agent_id: str = "") -> None:
        self._emit(
            ContextCompacted(
                strategy=str(info.get("strategy") or ""),
                messages_before=int(info.get("messages_before") or 0),
                messages_after=int(info.get("messages_after") or 0),
                chars_saved=int(info.get("chars_saved") or 0),
                summary=str(info.get("summary") or "")[:400],
                agent_id=agent_id,
            )
        )
        if info.get("strategy") == "overflow-retry":
            self._emit(
                WarningOccurred(
                    message=f"context budget reduced to {self._config.context_budget}"
                )
                )

    def _make_child_lsp(self, workspace: Path):
        if workspace.resolve() == self._workspace:
            return self._lsp
        if not self.language.supported:
            return None
        manager = LSPManager(workspace)
        name = self.language.name

        def warm() -> None:
            with suppress(OSError, RuntimeError, ValueError, LSPTimeoutError):
                manager.warm_start(name)

        threading.Thread(
            target=warm, daemon=True, name="lsp-worktree-warm"
        ).start()
        return manager

    def _on_agent_started(
        self,
        agent_id: str,
        profile: str,
        parent_id: str,
        task: str,
        worktree: str = "",
        branch: str = "",
        batch_id: str = "",
        batch_name: str = "",
    ) -> None:
        self._state.agents.append(
            AgentRow(
                id=agent_id,
                role="subagent",
                profile=profile,
                status="thinking",
                parent_id=parent_id,
                worktree=worktree,
                branch=branch,
                task=task,
                batch_id=batch_id,
                batch_name=batch_name,
            )
        )
        self._emit(
            AgentStarted(
                agent_id=agent_id,
                profile=profile,
                parent_id=parent_id,
                task=task,
                worktree=worktree,
                branch=branch,
                batch_id=batch_id,
                batch_name=batch_name,
            )
        )
        self._emit_agents()

    def _on_agent_result(self, agent_id: str, profile: str, result_text: str) -> None:
        if self._state.ended or self._state.session_id is None:
            return
        report = f"[agent {profile} {agent_id[:8]} finished]\n{result_text}"
        self._add_message(role="engine", text=report)
        self._persist()
        self._inbox.append(report)
        self._maybe_pump()

    def _on_worktree_settled(
        self,
        agent_id: str,
        profile: str,
        action: str,
        detail: str,
        branch: str,
        pr_url: str = "",
        ok: bool = True,
    ) -> None:
        if self._state.ended or self._state.session_id is None:
            return
        self._emit(
            WorktreeSettled(
                agent_id=agent_id,
                profile=profile,
                action=action,
                detail=detail or "",
                branch=branch,
                pr_url=pr_url,
                ok=ok,
            )
        )
        report = f"[worktree {profile} {agent_id[:8]} {action}]\n{detail}"
        self._add_message(role="engine", text=report)
        self._persist()
        self._inbox.append(report)
        self._maybe_pump()

    def _on_agent_finished(
        self, agent_id: str, profile: str, status: str, summary: str
    ) -> None:
        self._state.agents = [row for row in self._state.agents if row.id != agent_id]
        self._emit(
            AgentFinished(
                agent_id=agent_id,
                profile=profile,
                status=status,
                summary=summary or "",
            )
        )
        self._emit_agents()

    def _touch_agent(
        self,
        agent_id: str,
        *,
        status: str | None = None,
        current_tool: str | None = None,
    ) -> None:
        if not agent_id:
            return
        for row in self._state.agents:
            if row.id != agent_id:
                continue
            if status is not None:
                row.status = status
            if current_tool is not None:
                row.current_tool = current_tool
            self._emit_agents()
            return

    def _emit_agents(self) -> None:
        self._emit(AgentsUpdated(agents=[replace(row) for row in self._state.agents]))

    def _emit_stats(self) -> None:
        self._emit(StatsUpdated(stats=self._state.stats))

    def _cancel_history_replay(self) -> None:
        self._history_generation += 1
        task = self._history_task
        self._history_task = None
        if task is not None and not task.done():
            task.cancel()

    def _emit_snapshot(self, *, replay: bool = True) -> None:
        self._drop_missing_open_files()
        if replay:
            self._cancel_history_replay()
        snap = self.snapshot()
        history = list(snap.messages)
        tree = list(snap.file_tree)
        snap.messages = []
        snap.message_count = len(history)
        snap.file_tree_count = snap.file_tree_count or _count_tree(tree)
        snap.file_tree = []
        self._emit(SnapshotReady(snapshot=snap))
        if not replay:
            return
        self._emit_tree(tree)
        for path in list(self._state.open_files):
            self._emit_file_content(path)
        if not history:
            self._emit(ChatHistoryComplete(count=0))
            return
        generation = self._history_generation
        self._history_task = asyncio.get_running_loop().create_task(
            self._replay_history(history, generation)
        )

    def _emit_tree(self, tree=None) -> None:
        nodes = list(tree) if tree is not None else list_tree(self._workspace)
        if _approx_tree(nodes) > EVENT_SOFT_LIMIT:
            shallow = [
                type(node)(name=node.name, path=node.path, is_dir=node.is_dir, children=None)
                for node in nodes
            ]
            omitted = _count_tree(nodes) - len(shallow)
            self._emit(FileTreeUpdated(file_tree=shallow))
            self._emit(
                WarningOccurred(
                    message=f"file tree truncated; {omitted} entries omitted"
                )
            )
            return
        self._emit(FileTreeUpdated(file_tree=nodes))

    def _emit_file_content(self, path: str) -> None:
        try:
            rel, content = read_text(self._workspace, path)
        except (FileNotFoundError, WorkspacePathError) as exc:
            self._emit(ErrorOccurred(message=f"{path}: {exc}"))
            return
        clipped, omitted = clip_text(content, EVENT_SOFT_LIMIT)
        if omitted:
            clipped += "; use read_file"
            self._emit(
                WarningOccurred(
                    message=f"{rel} truncated; {omitted} bytes omitted"
                )
            )
        self._emit(FileContent(path=rel, content=clipped))

    async def _replay_history(self, history, generation: int) -> None:
        total = len(history)
        try:
            for index, message in enumerate(history):
                if generation != self._history_generation:
                    return
                self._emit(
                    ChatHistoryAdded(
                        id=message.id,
                        role=message.role,
                        text=message.text,
                        ts=message.ts,
                        index=index,
                        total=total,
                    )
                )
                await asyncio.sleep(0)
            if generation == self._history_generation:
                self._emit(ChatHistoryComplete(count=total))
        except asyncio.CancelledError:
            return

    def _drop_missing_open_files(self) -> None:
        kept = []
        for path in self._state.open_files:
            try:
                rel, _content = read_text(self._workspace, path)
            except (FileNotFoundError, WorkspacePathError):
                continue
            kept.append(rel)
        self._state.open_files = kept

    def _require_session(self) -> bool:
        if self._state.session_id is None:
            self._emit(ErrorOccurred(message="no active session; start one first"))
            return False
        return True

    def _persist(self) -> None:
        if self._state.session_id is None:
            return
        save_snapshot(
            self._db_path,
            self._state.snapshot(str(self._workspace), [], GitState.empty()),
        )

    def _add_message(
        self, role: str, text: str, *, message_id: str | None = None
    ) -> None:
        if role == "assistant" and not (text or "").strip():
            return
        message = ChatMessage(
            id=message_id or uuid4().hex,
            role=role,
            text=text,
            ts=datetime.now(timezone.utc).isoformat(),
        )
        self._state.messages.append(message)
        self._emit(
            ChatMessageAdded(
                id=message.id,
                role=message.role,
                text=message.text,
                ts=message.ts,
            )
        )

    def _emit(self, event: Event) -> None:
        for queue in list(self._subscribers):
            queue.put(event)


def _count_tree(nodes) -> int:
    total = 0
    for node in nodes:
        total += 1
        if node.children:
            total += _count_tree(node.children)
    return total


def _approx_tree(nodes) -> int:
    total = 0
    for node in nodes:
        total += len(node.name) + len(node.path)
        if node.children:
            total += _approx_tree(node.children)
    return total
