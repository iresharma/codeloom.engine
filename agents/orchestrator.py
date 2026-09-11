from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

from agents.agent_loop import AgentLoop
from agents.compactor import AgentResult
from agents.hooks import AgentHooks
from agents.profile import MEMORY, SKILLS, ProfileRegistry
from agents.subagent import Subagent
from runtime.config import CHILD_COMPACT_TRIGGER, CHILD_KEEP_FULL_TOOLS
from runtime.store.memory import ingest_result
from runtime.prompts import PromptTimeout
from runtime.tools.git import (
    SETTLE_CHOICES,
    add_agent_worktree,
    apply_worktree,
    commit_if_dirty,
    drop_empty_worktree,
    list_engine_worktrees,
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
- You have no filesystem tools. You cannot list_files, search, or read_file. `ask` is the only reader — after workspace memory has been checked (below).
- `coder` still read_file's a path before editing it (the write funnel requires that). That is not discovery. Your job is to put the paths and facts from memory or ask into the coder task so coder does not have to search or find(1).

Spawn is fire-and-forget. The tool returns immediately with agent_id (and worktree/branch when the child writes). Do not wait for the child in this turn. Spawn several personalities in one turn only when the work is independent — coder on a feature and debugger on a customer escalation can run at the same time. Tell the user you started them. Do not claim the work is done until a child report arrives.

Dependent work is sequenced across turns, not inside one turn:
- Need understanding then an edit? If workspace memory already has fresh paths and facts, spawn coder with those. Otherwise spawn ask now. When its report arrives as a follow-up, spawn coder with that report copied in: files, what to change, constraints. Never spawn coder in the same turn you would have needed ask's answer.
- After a code change, spawn tester and/or reviewer the same way — on the previous child's report, not by guessing.

Writers (coder, tester) run in a git worktree on a new branch under .engine/worktrees/. They will not collide with each other or with the user's checkout. Reviewer joins that worktree so it sees the writer's diff. The user is asked to merge, open a PR, keep, or discard after the writer and any reviewer on that tree have finished. A follow-up engine report says what they chose. Ask, researcher, and debugger use the main workspace.

Never spawn coder or tester to merge, push, check out the user's branch, or open a pull request. Writers cannot leave their worktree and cannot check out a branch already in use. When the user wants those changes applied — including after a keep — call settle_worktree with merge, pr, or discard. Use action=status if you need the agent_id or branch.

Check workspace memory before spawning ask:
- Fresh file notes or decision bullets that answer the question: reply from them. Quote the note. Do not spawn.
- STALE file notes, a missing path, or a question the notes do not cover: spawn ask. Put the stale or missing paths in the task. Do not quote a STALE note as fact.
- Deep "explain this subsystem" still spawns ask, but the task must start from the fresh notes (paths, purpose, entry points) rather than rediscovering them.

For edits, spawn coder. For verification, spawn tester. For a library, API, GitHub repo, error message, or anything not in this workspace, spawn researcher. For "what's broken", spawn debugger. After a code change, spawn reviewer if a verdict is useful.

A coder/tester task must include: concrete paths, the change or check required, and any facts already learned (quote fresh memory or ask's report; do not say "see above"). If you do not have those yet and memory does not cover them, spawn ask first instead of coder.

Answer directly when:
- the reply is already in this conversation or workspace memory (fresh file notes or decision sections)
- the user asked a meta question (status, what just happened, which agents exist)

Use remember for lasting engineering, product, or CI/CD decisions — not play-by-play or subagent transcripts. Ask/coder/researcher briefings are also persisted automatically on finish.

At most one ask and one researcher per user message. leftover_questions: put them in your answer and ask the user; do not spawn another ask or researcher to chase them. Respawn when status=incomplete, or status=max_turns for a writer, or the user explicitly asks to go deeper. If spawn returns "already spawned", answer with what you have.

If a child returns status=incomplete, respawn once with a tighter task or tell the user. If a child returns status=max_turns, spawn one writer (coder or tester) with the leftover / paths / files_touched from the report — do not rediscover the repo. Do not respawn ask or researcher on max_turns; tell the user the leftover. If a child returns status=stopped, tell the user; do not respawn. If spawn returns "spawn budget exhausted", too many children are already live — stop spawning and report what is running.

Do not call write tools or run_command. You do not have them.
"""


@dataclass
class _PendingSettle:
    agent_id: str
    profile: str
    dest: Path
    branch: str
    summary: str


def _apply_run_status(
    result: AgentResult, run_status: str, run_outcome: str
) -> AgentResult:
    """Merge child.run() status onto the compressor result.

    aborted/failed always win. incomplete (missing required tools) beats
    max_turns, stopped, and ok. max_turns and stopped only replace ok. A
    failed run keeps the compressor summary and only fills outcome from the
    exception when empty.
    """
    if run_status in {"aborted", "failed"}:
        result.status = run_status
        if (
            run_status == "failed"
            and run_outcome.startswith("error:")
            and not (result.outcome or "").strip()
        ):
            result.outcome = run_outcome
        return result
    if run_status in {"max_turns", "stopped"} and result.status == "ok":
        result.status = run_status
    return result


_CHILD_RUN_STATUSES = frozenset({"ok", "max_turns", "stopped"})


def _child_run_status(child) -> str:
    """Map a finished child's _exit_status onto the orch run status.

    aborted/failed are set by _run_child's except blocks, not here. An
    unexpected value becomes failed so it cannot look like a clean ok.
    """
    status = getattr(child, "_exit_status", None) or "ok"
    if status in _CHILD_RUN_STATUSES:
        return status
    return "failed"


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
        self._worktree_summaries: dict[str, str] = {}
        self._worktree_profiles: dict[str, str] = {}
        self._pending_settles: dict[str, _PendingSettle] = {}
        self._child_lsps: dict[str, object] = {}
        self._spawn_lock: asyncio.Lock | None = None
        self._user_survey_spawns: set[str] = set()
        self._survey_retry: set[str] = set()
        self._survey_retry_used: set[str] = set()
        self._inbox_turn = False
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

        kwargs.setdefault("tools", all_tools.subset(SKILLS + MEMORY))
        kwargs.setdefault("system_prompt", ORCH_SYSTEM)
        kwargs.setdefault("role", "orchestrator")
        kwargs.setdefault("concurrent_tools", True)
        kwargs.setdefault("write_globs", [])
        super().__init__(llm, **kwargs)
        for spec in profiles.as_tools(self.spawn):
            self._tools.register(spec)
        self._tools.register(_settle_worktree_tool(self))
        self._recover_worktrees()

    def reset_spawn_budget(self) -> None:
        self._aborting_all = False

    def reset_user_message_spawns(self) -> None:
        self._user_survey_spawns.clear()
        self._survey_retry.clear()
        self._survey_retry_used.clear()

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
            self._forget_worktree(agent_id)

    def _forget_worktree(self, agent_id: str) -> None:
        self._worktrees.pop(agent_id, None)
        self._worktree_branches.pop(agent_id, None)
        self._worktree_batches.pop(agent_id, None)
        self._worktree_summaries.pop(agent_id, None)
        self._worktree_profiles.pop(agent_id, None)
        self._pending_settles.pop(agent_id, None)

    def _recover_worktrees(self) -> None:
        workspace = self._ctx.workspace
        for agent_id, branch, dest in list_engine_worktrees(workspace):
            if agent_id in self._worktrees or agent_id in self._child_tasks:
                continue
            if not worktree_has_changes(workspace, dest):
                drop_empty_worktree(workspace, dest, branch)
                continue
            self._worktrees[agent_id] = dest
            self._worktree_branches[agent_id] = branch
            profile = ""
            if branch.startswith("engine/") and branch.count("/") >= 2:
                profile = branch.split("/", 2)[1]
            self._worktree_profiles[agent_id] = profile or "coder"

    def _remember_worktree(
        self, agent_id: str, dest: Path, branch: str, profile: str, batch_id: str
    ) -> None:
        self._worktrees[agent_id] = dest
        self._worktree_branches[agent_id] = branch
        self._worktree_batches[agent_id] = batch_id
        self._worktree_profiles[agent_id] = profile

    def describe_worktrees(self) -> str:
        self._recover_worktrees()
        if not self._worktrees:
            return "no open writer worktrees"
        lines = []
        for agent_id, dest in self._worktrees.items():
            branch = self._worktree_branches.get(agent_id, "")
            profile = self._worktree_profiles.get(agent_id, "")
            dirty = worktree_has_changes(self._ctx.workspace, dest)
            lines.append(
                f"agent_id={agent_id} profile={profile or '-'} "
                f"branch={branch} dirty={str(dirty).lower()} path={dest}"
            )
        return "\n".join(lines)

    async def apply_named_worktree(
        self, action: str, agent_id: str = "", branch: str = ""
    ) -> str:
        if action.strip().lower() in {"status", "list", ""}:
            return self.describe_worktrees()
        chosen = self._pick_worktree(agent_id=agent_id, branch=branch)
        if chosen is None:
            extra = self.describe_worktrees()
            return f"error: no matching writer worktree\n{extra}"
        aid, dest, wt_branch, profile, summary = chosen
        settle = self._settle_tasks.pop(aid, None)
        if settle is not None and not settle.done():
            settle.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await settle
        ok, detail, pr_url = await asyncio.to_thread(
            apply_worktree,
            self._ctx.workspace,
            dest,
            wt_branch,
            action,
            message=summary,
            title=summary,
            body=summary,
        )
        normalized = normalize_settle_action(action)
        if ok and normalized != "keep":
            self._forget_worktree(aid)
        if self._on_worktree_settled is not None:
            self._on_worktree_settled(
                aid, profile, normalized, detail, wt_branch, pr_url, ok
            )
        flag = "ok" if ok else "error"
        extra = f" {pr_url}" if pr_url else ""
        return f"{flag} {normalized} {wt_branch}{extra}\n{detail}"

    def _pick_worktree(
        self, agent_id: str = "", branch: str = ""
    ) -> tuple[str, Path, str, str, str] | None:
        self._recover_worktrees()
        needle = (agent_id or "").strip()
        want_branch = (branch or "").strip()
        matches: list[tuple[str, Path]] = []
        for aid, dest in self._worktrees.items():
            wt_branch = self._worktree_branches.get(aid, "")
            if needle and needle not in aid:
                continue
            if want_branch and want_branch not in wt_branch:
                continue
            matches.append((aid, dest))
        if not matches:
            return None
        if needle or want_branch:
            aid, dest = matches[0]
        else:
            with_work = [
                (aid, dest)
                for aid, dest in matches
                if worktree_has_changes(self._ctx.workspace, dest)
            ]
            pool = with_work or matches
            pool.sort(
                key=lambda item: item[1].stat().st_mtime if item[1].exists() else 0,
                reverse=True,
            )
            aid, dest = pool[0]
        wt_branch = self._worktree_branches.get(aid, "")
        profile = self._worktree_profiles.get(aid, "") or "coder"
        summary = self._worktree_summaries.get(aid) or (
            f"engine({profile}): {wt_branch or aid}"
        )
        return aid, dest, wt_branch, profile, summary

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
        self._inbox_turn = str(task).lstrip().startswith("[agent ")
        try:
            return await super().run(task)
        finally:
            self._batch_id = ""
            self._batch_name = ""
            self._inbox_turn = False

    async def spawn(self, profile_name: str, task: str) -> str:
        try:
            profile = self._profiles.get(profile_name)
        except KeyError:
            return f"error: unknown profile {profile_name}"
        if self._spawn_lock is None:
            self._spawn_lock = asyncio.Lock()
        async with self._spawn_lock:
            # First user turn (_inbox_turn False) may fan out ask+researcher
            # in parallel. Only leftover inbox turns after "[agent … finished]"
            # are blocked from respawning the same survey profile.
            if (
                profile_name in _SURVEY_ONCE
                and profile_name in self._user_survey_spawns
                and self._inbox_turn
            ):
                if profile_name not in self._survey_retry:
                    return (
                        f"error: already spawned {profile_name} this user message; "
                        "answer with what you have or ask the user"
                    )
                self._survey_retry.discard(profile_name)
                self._survey_retry_used.add(profile_name)
            if self._live_spawn_count() >= self._spawn_budget:
                return "error: spawn budget exhausted"
            agent_id = uuid4().hex
            self._reserved.add(agent_id)
            if profile_name in _SURVEY_ONCE:
                self._user_survey_spawns.add(profile_name)
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
                    self._remember_worktree(
                        agent_id, Path(path), branch, profile.name, batch_id
                    )
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
            self._user_survey_spawns.discard(profile_name)
            self._child_tasks.pop(agent_id, None)
            self._children.pop(agent_id, None)
            wt = self._worktrees.get(agent_id)
            self._forget_worktree(agent_id)
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
            status = _child_run_status(child)
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
        _apply_run_status(result, status, outcome)
        if (
            result.status == "incomplete"
            and profile.name in _SURVEY_ONCE
            and profile.name not in self._survey_retry_used
        ):
            self._survey_retry.add(profile.name)
        files = child._ctx.files
        survey_paths = (
            list(files.paths()) if files is not None and hasattr(files, "paths") else []
        )
        try:
            ingest_result(
                child._ctx.workspace,
                profile.name,
                result,
                survey_paths=survey_paths,
                store=self._ctx.workspace,
            )
        except Exception:  # noqa: BLE001
            pass
        self._shutdown_child_lsp(agent_id)
        owns_worktree = agent_id in self._worktrees
        should_settle = owns_worktree and status != "aborted" and not self._aborting_all
        if status == "aborted" and owns_worktree:
            wt = self._worktrees.get(agent_id)
            self._forget_worktree(agent_id)
            if wt is not None:
                with suppress(OSError):
                    await asyncio.to_thread(
                        remove_agent_worktree, self._ctx.workspace, wt
                    )
        if self._on_agent_finished is not None:
            self._on_agent_finished(
                agent_id,
                profile.name,
                result.status,
                result.summary,
                usage=child._usage,
            )
        if self._on_agent_result is not None and not self._aborting_all:
            self._on_agent_result(agent_id, profile.name, result.as_text())
        self._child_tasks.pop(agent_id, None)
        self._children.pop(agent_id, None)
        if should_settle:
            dest = self._worktrees.get(agent_id)
            if dest is not None:
                summary = result.summary or result.outcome or task
                self._worktree_summaries[agent_id] = (
                    f"engine({profile.name}): {(summary or 'worktree').strip()[:72]}"
                )
                self._pending_settles[agent_id] = _PendingSettle(
                    agent_id=agent_id,
                    profile=profile.name,
                    dest=dest,
                    branch=branch or self._worktree_branches.get(agent_id, ""),
                    summary=summary,
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
                self._forget_worktree(agent_id)
                return
            message = f"engine({profile}): {(summary or 'worktree').strip()[:72]}"
            self._worktree_summaries[agent_id] = message
            commit_err = await asyncio.to_thread(commit_if_dirty, dest, message)
            if commit_err:
                if self._on_worktree_settled is not None and not self._aborting_all:
                    self._on_worktree_settled(
                        agent_id, profile, "keep", commit_err, branch, "", False
                    )
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
                        timeout=0,
                        agent_id=agent_id,
                        profile=profile,
                    )
                except asyncio.CancelledError:
                    return
                except PromptTimeout:
                    answer = "keep"
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
                self._forget_worktree(agent_id)
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
            self._config,
            max_turns=profile.max_turns or self._config.max_turns,
            compact_trigger=CHILD_COMPACT_TRIGGER,
            keep_full_tools=CHILD_KEEP_FULL_TOOLS,
        )
        tools = self._all_tools.subset(profile.tool_names, profile=profile.name)
        child_model = (profile.model or self._config.child_model or "").strip() or None
        hooks = None
        if self._make_child_hooks is not None:
            hooks = self._make_child_hooks(agent_id, profile.name)

        async def ask_user(question, kind="text", **kwargs):
            if self._child_ask_user is None:
                return "no"
            kwargs["agent_id"] = agent_id
            kwargs["profile"] = profile.name
            return await self._child_ask_user(question, kind=kind, **kwargs)

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
            model=child_model,
        )


_SURVEY_ONCE = frozenset({"ask", "researcher"})


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


def _settle_worktree_tool(orch: Orchestrator) -> Tool:
    async def execute(
        ctx: ToolContext,  # noqa: ARG001
        action: str,
        agent_id: str = "",
        branch: str = "",
    ) -> str:
        return await orch.apply_named_worktree(
            action, agent_id=agent_id, branch=branch
        )

    return Tool(
        name="settle_worktree",
        description=(
            "Apply a finished writer worktree: merge into the current branch, "
            "open a pull request, keep it, or discard it. Use action=status to "
            "list open trees. Never spawn coder to merge or push."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": (
                        "merge, pr, keep, discard, or status. "
                        "merge applies the writer's branch onto the user's checkout. "
                        "pr pushes that branch and opens a GitHub pull request."
                    ),
                },
                "agent_id": {
                    "type": "string",
                    "description": "Writer agent_id (full or prefix). Omit to pick the latest tree with changes.",
                },
                "branch": {
                    "type": "string",
                    "description": "Optional branch name (or substring) if agent_id is unknown.",
                },
            },
            "required": ["action"],
        },
        fn=execute,
    )
