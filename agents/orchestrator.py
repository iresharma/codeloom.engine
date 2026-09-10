from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

from agents.agent_loop import AgentLoop
from agents.compactor import AgentResult, write_context_md
from agents.hooks import AgentHooks
from agents.profile import SKILLS, ProfileRegistry
from agents.subagent import Subagent
from runtime.prompts import PromptTimeout
from runtime.tools.git import (
    SETTLE_CHOICES,
    add_agent_worktree,
    apply_worktree,
    drop_empty_worktree,
    normalize_settle_action,
    remove_agent_worktree,
    worktree_has_changes,
)
from runtime.tools.tracker import FileTracker
from tools.base import Tool, ToolContext
from tools.registry import ToolRegistry

ORCH_SYSTEM = """You are the orchestrator for this workspace. You talk to the user. You do not implement changes, run tests, investigate bugs, or survey the codebase yourself.

Spawn a personality by calling it as a tool (ask, coder, tester, researcher, debugger, reviewer). Pass a specific task string: paths, expected outcome, constraints.

Who does the reading:
- You have no filesystem tools. You cannot list_files, search, or read_file. `ask` is the only reader. Any "how does this work", "find where X lives", or "survey the repo" is an `ask` spawn — even if you will later spawn coder.
- `coder` still read_file's a path before editing it (the write funnel requires that). That is not discovery. Your job is to put the paths and facts from ask into the coder task so coder does not have to search or find(1).

Spawn is fire-and-forget. The tool returns immediately with agent_id (and worktree/branch when the child writes). Do not wait for the child in this turn. Spawn several personalities in one turn only when the work is independent — coder on a feature and debugger on a customer escalation can run at the same time. Tell the user you started them. Do not claim the work is done until a child report arrives.

Dependent work is sequenced across turns, not inside one turn:
- Need understanding then an edit? Spawn ask now. When its report arrives as a follow-up, spawn coder with that report copied in: files, what to change, constraints. Never spawn coder in the same turn you would have needed ask's answer.
- After a code change, spawn tester and/or reviewer the same way — on the previous child's report, not by guessing.

Writers (coder, tester) run in a git worktree on a new branch under .engine/worktrees/. They will not collide with each other or with the user's checkout. Reviewer joins that worktree so it sees the writer's diff. The user is asked to merge, open a PR, keep, or discard after the writer and any reviewer on that tree have finished. A follow-up engine report says what they chose. Ask, researcher, and debugger use the main workspace.

For code questions, spawn ask. For edits, spawn coder. For verification, spawn tester. For external docs, spawn researcher. For "what's broken", spawn debugger. After a code change, spawn reviewer if a verdict is useful.

A coder/tester task must include: concrete paths, the change or check required, and any facts already learned (quote ask's report; do not say "see above"). If you do not have those yet, spawn ask first instead of coder.

Answer directly only when:
- the reply is already in this conversation or workspace notes
- the user asked a meta question (status, what just happened, which agents exist)

If a child returns status=incomplete, respawn once with a tighter task or tell the user. If spawn returns "spawn budget exhausted", too many children are already live — stop spawning and report what is running. leftover_questions: ask the user, then respawn if needed.

Do not call write tools or run_command. You do not have them.
"""


@dataclass
class _PendingSettle:
    agent_id: str
    profile: str
    dest: Path
    branch: str
    summary: str


class Orchestrator(AgentLoop):
    def __init__(
        self,
        llm,
        *,
        all_tools: ToolRegistry,
        profiles: ProfileRegistry,
        spawn_budget: int = 8,
        make_child_hooks: Callable[[str, str], AgentHooks] | None = None,
        make_child_lsp: Callable | None = None,
        on_agent_started: Callable | None = None,
        on_agent_finished: Callable | None = None,
        on_agent_result: Callable | None = None,
        on_worktree_settled: Callable | None = None,
        child_ask_user: Callable | None = None,
        child_on_output: Callable | None = None,
        child_on_edit: Callable | None = None,
        child_on_proc: Callable | None = None,
        write_lock=None,
        **kwargs,
    ):
        self._all_tools = all_tools
        self._profiles = profiles
        self._spawn_budget = spawn_budget
        self._child_tasks: dict[str, asyncio.Task] = {}
        self._settle_tasks: dict[str, asyncio.Task] = {}
        self._children: dict[str, Subagent] = {}
        self._reserved: set[str] = set()
        self._worktrees: dict[str, Path] = {}
        self._worktree_branches: dict[str, str] = {}
        self._worktree_batches: dict[str, str] = {}
        self._pending_settles: dict[str, _PendingSettle] = {}
        self._child_lsps: dict[str, object] = {}
        self._spawn_lock: asyncio.Lock | None = None
        self._make_child_hooks = make_child_hooks
        self._make_child_lsp = make_child_lsp
        self._on_agent_started = on_agent_started
        self._on_agent_finished = on_agent_finished
        self._on_agent_result = on_agent_result
        self._on_worktree_settled = on_worktree_settled
        self._child_ask_user = child_ask_user
        self._child_on_output = child_on_output
        self._child_on_edit = child_on_edit
        self._child_on_proc = child_on_proc
        self._write_lock = write_lock
        self._aborting_all = False
        self._batch_id = ""
        self._batch_name = ""
        self._skills = kwargs.get("skills")
        self._on_skill_activated = kwargs.get("on_skill_activated")

        kwargs.setdefault("tools", all_tools.subset(SKILLS))
        kwargs.setdefault("system_prompt", ORCH_SYSTEM)
        kwargs.setdefault("role", "orchestrator")
        kwargs.setdefault("concurrent_tools", True)
        kwargs.setdefault("write_globs", [])
        super().__init__(llm, **kwargs)
        for spec in profiles.as_tools(self.spawn):
            self._tools.register(spec)
        self._tools.register(_write_context_tool(self._ctx.workspace))

    def reset_spawn_budget(self) -> None:
        self._aborting_all = False

    def _live_spawn_count(self) -> int:
        live = sum(1 for task in self._child_tasks.values() if not task.done())
        return live + len(self._reserved)

    def abort_all_children(self) -> None:
        self._aborting_all = True
        for task in list(self._child_tasks.values()):
            if not task.done():
                task.cancel()
        for task in list(self._settle_tasks.values()):
            if not task.done():
                task.cancel()

    def abort_child(self, agent_id: str) -> bool:
        task = self._child_tasks.get(agent_id)
        if task is None or task.done():
            settle = self._settle_tasks.get(agent_id)
            if settle is None or settle.done():
                return False
            settle.cancel()
            return True
        task.cancel()
        return True

    async def wait_children(self) -> None:
        tasks = list(self._child_tasks.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def wait_settle(self) -> None:
        tasks = list(self._settle_tasks.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def cleanup_worktrees(self) -> None:
        self._pending_settles.clear()
        for agent_id, dest in list(self._worktrees.items()):
            with suppress(OSError):
                remove_agent_worktree(self._ctx.workspace, dest)
            self._worktrees.pop(agent_id, None)
            self._worktree_branches.pop(agent_id, None)
            self._worktree_batches.pop(agent_id, None)

    def has_live_children(self) -> bool:
        return any(not task.done() for task in self._child_tasks.values())

    def schedule_flush_settles(self) -> None:
        if self._aborting_all:
            return
        if self.has_live_children():
            return
        for item in list(self._pending_settles.values()):
            if item.agent_id in self._settle_tasks:
                continue
            self._pending_settles.pop(item.agent_id, None)
            settle = asyncio.get_running_loop().create_task(
                self._settle_worktree(
                    item.agent_id,
                    item.profile,
                    item.dest,
                    item.branch,
                    item.summary,
                )
            )
            self._settle_tasks[item.agent_id] = settle

    def _worktree_to_join(self, batch_id: str) -> tuple[str, Path | None, str]:
        chosen = ""
        for agent_id, dest in self._worktrees.items():
            if dest is None or not dest.is_dir():
                continue
            if batch_id and self._worktree_batches.get(agent_id) == batch_id:
                chosen = agent_id
                break
            chosen = agent_id
        if not chosen:
            return "", None, ""
        return chosen, self._worktrees[chosen], self._worktree_branches.get(chosen, "")

    async def run(self, task: str) -> str:
        self._batch_id = uuid4().hex
        self._batch_name = batch_nickname(task)
        try:
            return await super().run(task)
        finally:
            self._batch_id = ""
            self._batch_name = ""

    async def spawn(self, profile_name: str, task: str) -> str:
        try:
            profile = self._profiles.get(profile_name)
        except KeyError:
            return f"error: unknown profile {profile_name}"
        if self._spawn_lock is None:
            self._spawn_lock = asyncio.Lock()
        async with self._spawn_lock:
            if self._live_spawn_count() >= self._spawn_budget:
                return "error: spawn budget exhausted"
            agent_id = uuid4().hex
            self._reserved.add(agent_id)
            batch_id = self._batch_id or uuid4().hex
            batch_name = self._batch_name or batch_nickname(task)
        worktree = ""
        branch = ""
        warning = ""
        child_workspace = self._ctx.workspace
        try:
            if profile.needs_worktree:
                path, branch, err = await asyncio.to_thread(
                    add_agent_worktree,
                    self._ctx.workspace,
                    agent_id,
                    profile.name,
                )
                if err:
                    warning = f" (worktree unavailable: {err}; using main workspace)"
                    branch = ""
                else:
                    worktree = path
                    child_workspace = Path(path)
                    self._worktrees[agent_id] = Path(path)
                    self._worktree_branches[agent_id] = branch
                    self._worktree_batches[agent_id] = batch_id
            elif profile.join_worktree:
                owner, joined, joined_branch = self._worktree_to_join(batch_id)
                if joined is not None:
                    worktree = str(joined)
                    branch = joined_branch
                    child_workspace = joined
                    extra_note = f" (joined {owner[:8]} worktree)"
                    warning = (warning + extra_note) if warning else extra_note
            child = self._make_subagent(profile, agent_id, child_workspace, bool(worktree))
            self._children[agent_id] = child
            run_task = asyncio.get_running_loop().create_task(
                self._run_child(agent_id, profile, child, task, worktree, branch)
            )
            self._child_tasks[agent_id] = run_task
        except Exception as exc:  # noqa: BLE001
            self._reserved.discard(agent_id)
            self._child_tasks.pop(agent_id, None)
            self._children.pop(agent_id, None)
            wt = self._worktrees.pop(agent_id, None)
            self._worktree_branches.pop(agent_id, None)
            self._worktree_batches.pop(agent_id, None)
            self._pending_settles.pop(agent_id, None)
            if wt is not None:
                await asyncio.to_thread(remove_agent_worktree, self._ctx.workspace, wt)
            return f"error: {exc}"
        self._reserved.discard(agent_id)
        if self._on_agent_started is not None:
            self._on_agent_started(
                agent_id,
                profile.name,
                self.agent_id,
                task,
                worktree=worktree,
                branch=branch,
                batch_id=batch_id,
                batch_name=batch_name,
            )
        extra = f" worktree={worktree} branch={branch}" if worktree else ""
        return (
            f"started agent_id={agent_id} profile={profile.name} "
            f"batch_id={batch_id} batch_name={batch_name}{extra}{warning}"
        )

    async def _run_child(
        self,
        agent_id: str,
        profile,
        child: Subagent,
        task: str,
        worktree: str,
        branch: str,
    ) -> None:
        status = "ok"
        outcome = ""
        try:
            child.set_catalog_query(task)
            text = await child.run(task)
            if str(text).startswith("stopped after"):
                status = "max_turns"
            outcome = text
        except asyncio.CancelledError:
            status = "aborted"
            outcome = "(aborted)"
        except Exception as exc:  # noqa: BLE001
            status = "failed"
            outcome = f"error: {exc}"
        try:
            result = await child.finish(status)
        except asyncio.CancelledError:
            result = AgentResult(status="aborted", outcome="(aborted)")
        except Exception as exc:  # noqa: BLE001
            result = AgentResult(status="failed", outcome=f"error: {exc}")
        if status in {"aborted", "failed", "max_turns"}:
            result.status = status
        if status == "failed" and outcome.startswith("error:"):
            result.outcome = outcome
        note = (
            f"subagent {agent_id} ({profile.name}): {result.status} — "
            f"{(result.summary or result.outcome)[:200]}"
        )
        if worktree:
            note += f" worktree={worktree} branch={branch}"
        try:
            write_context_md(self._ctx.workspace, note)
        except OSError:
            pass
        self._shutdown_child_lsp(agent_id)
        owns_worktree = agent_id in self._worktrees
        should_settle = owns_worktree and status != "aborted" and not self._aborting_all
        if status == "aborted" and owns_worktree:
            wt = self._worktrees.pop(agent_id, None)
            self._worktree_branches.pop(agent_id, None)
            self._worktree_batches.pop(agent_id, None)
            self._pending_settles.pop(agent_id, None)
            if wt is not None:
                with suppress(OSError):
                    await asyncio.to_thread(
                        remove_agent_worktree, self._ctx.workspace, wt
                    )
        if self._on_agent_finished is not None:
            self._on_agent_finished(
                agent_id, profile.name, result.status, result.summary
            )
        if self._on_agent_result is not None and not self._aborting_all:
            self._on_agent_result(agent_id, profile.name, result.as_text())
        self._child_tasks.pop(agent_id, None)
        self._children.pop(agent_id, None)
        if should_settle:
            dest = self._worktrees.get(agent_id)
            if dest is not None:
                self._pending_settles[agent_id] = _PendingSettle(
                    agent_id=agent_id,
                    profile=profile.name,
                    dest=dest,
                    branch=branch or self._worktree_branches.get(agent_id, ""),
                    summary=result.summary or result.outcome or task,
                )

    async def _settle_worktree(
        self,
        agent_id: str,
        profile: str,
        dest: Path,
        branch: str,
        summary: str,
    ) -> None:
        try:
            if self._aborting_all or self._worktrees.get(agent_id) is None:
                return
            has_changes = await asyncio.to_thread(
                worktree_has_changes, self._ctx.workspace, dest
            )
            if not has_changes:
                await asyncio.to_thread(
                    drop_empty_worktree, self._ctx.workspace, dest, branch
                )
                self._worktrees.pop(agent_id, None)
                self._worktree_branches.pop(agent_id, None)
                self._worktree_batches.pop(agent_id, None)
                return
            question = (
                f"{profile} work on {branch} is ready. Merge into the current "
                "branch, open a pull request, keep the worktree, or discard it?"
            )
            answer = "keep"
            if self._child_ask_user is not None:
                try:
                    answer = await self._child_ask_user(
                        question,
                        kind="choice",
                        choices=list(SETTLE_CHOICES),
                        default="keep",
                        agent_id=agent_id,
                        profile=profile,
                    )
                except asyncio.CancelledError:
                    return
                except PromptTimeout:
                    answer = "keep"
            message = f"engine({profile}): {(summary or 'worktree').strip()[:72]}"
            ok, detail, pr_url = await asyncio.to_thread(
                apply_worktree,
                self._ctx.workspace,
                dest,
                branch,
                answer,
                message=message,
                title=message,
                body=summary or message,
            )
            action = normalize_settle_action(answer)
            if ok and action != "keep":
                self._worktrees.pop(agent_id, None)
                self._worktree_branches.pop(agent_id, None)
                self._worktree_batches.pop(agent_id, None)
            if self._on_worktree_settled is not None and not self._aborting_all:
                self._on_worktree_settled(
                    agent_id, profile, action, detail, branch, pr_url, ok
                )
        finally:
            self._settle_tasks.pop(agent_id, None)

    def _shutdown_child_lsp(self, agent_id: str) -> None:
        mgr = self._child_lsps.pop(agent_id, None)
        if mgr is None or mgr is self._ctx.lsp:
            return
        shutdown = getattr(mgr, "shutdown_all", None)
        if shutdown is not None:
            with suppress(OSError, RuntimeError):
                shutdown()

    def _make_subagent(
        self, profile, agent_id: str, workspace: Path, isolated: bool
    ) -> Subagent:
        child_config = replace(
            self._config, max_turns=profile.max_turns or self._config.max_turns
        )
        tools = self._all_tools.subset(profile.tool_names, profile=profile.name)
        hooks = None
        if self._make_child_hooks is not None:
            hooks = self._make_child_hooks(agent_id, profile.name)

        async def ask_user(question, kind="text", **kwargs):
            if self._child_ask_user is None:
                return "no"
            return await self._child_ask_user(
                question,
                kind=kind,
                agent_id=agent_id,
                profile=profile.name,
                **kwargs,
            )

        def on_output(call_id, stream, text):
            if self._child_on_output is not None:
                self._child_on_output(call_id, stream, text, agent_id)

        lsp = self._ctx.lsp
        if isolated and self._make_child_lsp is not None:
            lsp = self._make_child_lsp(workspace)
            if lsp is not None and lsp is not self._ctx.lsp:
                self._child_lsps[agent_id] = lsp

        write_lock = self._write_lock
        if isolated:
            write_lock = asyncio.Lock()

        return Subagent(
            profile,
            llm=self._llm,
            tools=tools,
            workspace=workspace,
            hooks=hooks,
            language=self._ctx.language,
            lsp=lsp,
            files=FileTracker(),
            journal=self._ctx.journal,
            session_id=self._ctx.session_id,
            on_edit=self._child_on_edit,
            config=child_config,
            ask_user=ask_user,
            on_output=on_output,
            on_proc=self._child_on_proc,
            agent_id=agent_id,
            parent_id=self.agent_id,
            write_lock=write_lock,
            skills=self._skills,
            on_skill_activated=self._on_skill_activated,
        )


def batch_nickname(task: str, *, limit: int = 48) -> str:
    text = (task or "").strip()
    if text.startswith("[agent ") and " finished]" in text:
        head = text.split(" finished]", 1)[0]
        bits = head[7:].split()
        profile = bits[0] if bits else "agent"
        text = f"after {profile}"
    else:
        text = " ".join(text.replace("\n", " ").split())
    if not text:
        return "untitled"
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def _write_context_tool(workspace) -> Tool:
    async def execute(ctx: ToolContext, note: str) -> str:
        write_context_md(ctx.workspace if ctx is not None else workspace, note)
        return "ok"

    return Tool(
        name="write_context",
        description="Append a short lasting note to workspace memory (.engine/context.md).",
        parameters={
            "type": "object",
            "properties": {
                "note": {
                    "type": "string",
                    "description": "A short fact or decision to remember.",
                }
            },
            "required": ["note"],
        },
        fn=execute,
    )
