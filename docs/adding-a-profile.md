# Adding a subagent profile

Profiles are discovered the same way tools are: drop a module under
`agents/profiles/`, export `PROFILE = AgentProfile(...)`, and the orchestrator
picks it up on the next `StartSession`. There is no registry file to edit.

The orchestrator sees each profile as a **tool named after the profile**. Calling
`ask` / `coder` / `tester` / … starts that child and returns immediately with
`agent_id` (and `worktree` / `branch` when the profile writes). The child runs in
the background. Tokens stream to the client tagged with `agent_id`. When it finishes, its transcript is compacted into an
`AgentResult` string and delivered to the orch as a follow-up message (not as
the original tool result). The child never chats with the user.

## Who reads vs who writes

The orchestrator is the planner, not a third reader. Typical edit:

1. Spawn **ask** with the survey question. Stop. Do not also spawn coder in that turn.
2. When ask's report arrives, spawn **coder** with paths, facts, and the change copied into the task.
3. Coder `read_file`s those named paths (required before an edit) and writes. It should not `find` / search the tree unless a named path is missing.

Skip a separate planner personality — that is the orch. Independent work (coder on a feature and debugger on an escalation) still runs in the same turn.

## Minimal example

```python
# agents/profiles/docs.py
from agents.profile import NAV, AgentProfile

PROFILE = AgentProfile(
    name="docs",
    description="Write or update markdown docs. Cannot edit application code.",
    system_prompt="You only touch documentation files. ...",
    tool_names=NAV + ["str_replace", "create_file", "read_file"],
    write_globs=["**/*.md", "**/docs/**"],
    required_tools=[],
    max_turns=8,
    needs_worktree=False,
)
```

Restart the session (or call `discover_profiles()` in a test). The orch model
will see a `docs` tool with a `task` argument.

## Examples

Three end-to-end recipes, from simplest to most involved.

### Example A: a profile with no tools at all

Some personalities only need to reason over the task text and conversation
history — no filesystem, no shell, nothing. Set `tool_names=[]` and
`write_globs=[]` so it is a hard no-op even if a future edit to the profile
accidentally adds a tool name.

```python
# agents/profiles/planner.py
from __future__ import annotations
from agents.profile import AgentProfile

PLANNER_SYSTEM = """You are a planning-only agent. You do not read files,
run commands, or edit anything. Given a task description and any facts
already provided in the prompt, produce a short ordered plan (steps, owners,
risks). If you need information you were not given, say so explicitly and
stop — do not guess at file paths or code contents."""

PROFILE = AgentProfile(
    name="planner",
    description=(
        "Turns a fuzzy task into an ordered plan using only the text it was "
        "given. Has no tools — cannot read the repo or verify anything. Use "
        "only when the caller already has the facts and just wants them "
        "organized."
    ),
    system_prompt=PLANNER_SYSTEM,
    tool_names=[],
    write_globs=[],
    required_tools=[],
    max_turns=4,
    needs_worktree=False,
)
```

Nothing else to wire up — dropping this file under `agents/profiles/` is the
entire registration step. Restart the session (or call `discover_profiles()`
in a test) and the orch model will see a `planner(task=...)` tool.

### Example B: a profile reusing existing tools

Most new personalities just need a different combination of tools that
already exist, scoped to a narrower purpose than `coder`. The `docs` profile
above is one instance of this; here it is again with the reasoning made
explicit. It combines the shared `NAV` bundle (`list_files`, `read_file`,
`search`) with two specific `EDIT` tools (`str_replace`, `create_file`), and
restricts `write_globs` so it can only touch markdown/docs paths even though
`str_replace`/`create_file` could otherwise write anywhere in the workspace:

```python
# agents/profiles/docs.py
from __future__ import annotations
from agents.profile import NAV, AgentProfile

DOCS_SYSTEM = """You write and update markdown documentation. You may read
any file for context, but you may only create or edit files under docs/ or
files ending in .md. Never touch application code."""

PROFILE = AgentProfile(
    name="docs",
    description="Write or update markdown docs. Cannot edit application code.",
    system_prompt=DOCS_SYSTEM,
    tool_names=NAV + ["str_replace", "create_file"],
    write_globs=["**/*.md", "docs/**"],
    required_tools=[],
    max_turns=8,
    needs_worktree=False,
)
```

The key decision here is `write_globs`. `coder` sets `write_globs=None`
(unrestricted — any path the write funnel allows), because it is meant to
edit application code anywhere. A scoped personality like `docs` should
instead pass an explicit glob list; the write funnel enforces it, so even if
`str_replace` or `create_file` is called against `agents/orchestrator.py` the
call is rejected — the system prompt is not what keeps this personality
scoped, `write_globs` is.

### Example C: a profile plus a brand-new custom tool

Sometimes the tools you need do not exist yet. This example adds a
`todo_scanner` tool (counts `TODO`/`FIXME` comments in a file) and a new
`auditor` profile that uses it.

1. **Framework-free logic** in `runtime/tools/todos.py`:

```python
# runtime/tools/todos.py
from __future__ import annotations
from runtime.tools.fileid import read_source

MARKERS = ("TODO", "FIXME")

def count_markers(workspace: str, path: str) -> int:
    src = read_source(workspace, path)
    return sum(1 for line in src.text.splitlines() if any(m in line for m in MARKERS))
```

1. **Thin LLM-facing wrapper** in `tools/todos.py`, using the `@tool`
  decorator from `tools/base.py`:

```python
# tools/todos.py
from tools.base import ToolContext, tool
from runtime.tools.fileid import read_source
from runtime.tools.todos import count_markers

@tool(
    description="Count TODO/FIXME markers in a workspace text file. Read-only.",
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Relative path to scan."}},
        "required": ["path"],
    },
)
def todo_scanner(ctx: ToolContext, path: str) -> str:
    src = read_source(ctx.workspace, path)
    if ctx.files is not None:
        ctx.files.mark(src.rel, src.raw_sha256)
    return str(count_markers(ctx.workspace, path))
```

   No registry edit needed — `discover_tools()` in `tools/registry.py` finds
   any function carrying the `_engine_tool` attribute that `@tool` attaches.

1. **New profile** in `agents/profiles/auditor.py` that references the new
  tool purely by name, alongside the shared `NAV` bundle:

```python
# agents/profiles/auditor.py
from __future__ import annotations
from agents.profile import NAV, AgentProfile

AUDITOR_SYSTEM = """You scan the codebase for TODO/FIXME markers and report
on technical debt. Use todo_scanner on files the caller names; use search
first only if no paths were given."""

PROFILE = AgentProfile(
    name="auditor",
    description="Scans named files for TODO/FIXME markers and reports counts.",
    system_prompt=AUDITOR_SYSTEM,
    tool_names=NAV + ["todo_scanner"],
    write_globs=[],
    required_tools=["todo_scanner"],
    max_turns=6,
    needs_worktree=False,
)
```

   `required_tools=["todo_scanner"]` means if the child never actually calls
   it, `AgentResult.status` comes back `"incomplete"` — a soft signal, not a
   hard stop.

1. `discover_tools()` and `discover_profiles()` both run together in
  `runtime/session.py`'s `_bind_loop()`, so restarting the session (or
   calling both functions directly in a test) picks up the new tool and the
   new profile at the same time — there is nothing to wire by hand in either
   direction.
2. Add tests: a `discover_tools()` assertion that `todo_scanner` is present
  with no registry errors (see the pattern in `tests/test_tools_registry.py`),
   and — if `auditor` should be a permanent builtin personality — add
   `"auditor"` to the name set asserted in
   `tests/test_profiles.py::test_discover_builtin_profiles`.



## `AgentProfile` fields

- `name` — tool name the orch calls. Must be unique.
- `description` — shown to the orch so it can choose. Be specific about what
this personality will not do.
- `system_prompt` — the child's only system prompt (plus workspace memory rendered from
`.engine/memory.json`). Subagents freeze that system text on the first model call so
prompt-cache prefixes stay stable; `remember()` during the child's own run is visible
as a tool result, not as a rewritten system block. The orchestrator re-renders every turn.
Ask, coder, and researcher briefings are ingested into the main workspace
`memory.json` when the child finishes (hashed in the child's tree, including
worktrees). Mid-run `remember` can still write a better structured note; ingest
will not overwrite a **fresh** file note. STALE notes (disk hash ≠ `note_sha`)
are rewritten from the briefing.
- `tool_names` — allowlist from `discover_tools()`. Unknown names become
registry errors, not a crash. Never include other personality names. Include
`MEMORY` (`remember`) unless the personality truly has nothing to persist.
- `write_globs` — `None` means any workspace path the write funnel already
allows; `[]` means no writes even if an edit tool was wired by mistake; a
list is matched against the relative path (`**/tests/**`, `**/*.md`, …).
- `required_tools` — if these names never appear in the child's tool trace,
`AgentResult.status` is `incomplete` (the child still exits). Used by `coder`
(`get_diagnostics`) and `tester` (`run_command`).
- `max_turns` — child's own cap, independent of the orch. Built-in profiles use 32. Hitting it prompts continue (same child, another `turn_slice`), handoff (orch may spawn one writer with leftover), or stop (no respawn). `ENGINE_TURN_CONTINUE=never` skips the prompt and hands off. Do not lower researcher without `Stats.agent_runs` showing frequent `max_turns`; spawn-once still blocks a sibling researcher, so a hard cut just wastes the survey.
- `model` — optional OpenRouter model id for this personality. `ask` and `tester` use Haiku; researcher/coder/debugger/reviewer inherit `OPENROUTER_MODEL`. `OPENROUTER_CHILD_MODEL` overrides only profiles that leave `model` unset.
- `needs_worktree` — if true, the child runs in a git worktree on a new branch
under `.engine/worktrees/` so writers do not collide. `coder` and `tester`
set this. When the child finishes with changes, those edits are committed on
the writer branch, then the user is prompted to merge, open a PR, keep, or
discard — after any `join_worktree` personality (reviewer) on that tree has
also finished. Later "please merge it" goes through `settle_worktree`, not a
new writer spawn.
- `join_worktree` — if true, the child reuses a live writer worktree (same
batch if possible) instead of creating one. `reviewer` sets this so it sees
the writer's diff. If no worktree is open, it uses the main checkout.



## Hard vs prompt-only

The allowlist and `write_globs` are enforced. Do not rely on the system prompt
alone to keep a personality read-only. `required_tools` is reported, not a
hard stop. Which test runner to invoke stays prompt-only.

## Checklist

- [ ] `PROFILE` is a module-level `AgentProfile` in `agents/profiles/<name>.py`
- [ ] `tool_names` is an allowlist, not "all tools"
- [ ] Read-only personalities set `write_globs=[]`
- [ ] Writers that must stay in a subdirectory set `write_globs`
- [ ] Writers that must stay isolated from other writers set `needs_worktree`
- [ ] Review-only personalities that should see a writer's diff set `join_worktree`
- [ ] Description tells the orch when to pick this personality
- [ ] A test uses `discover_profiles()` and checks the name is present