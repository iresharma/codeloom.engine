# Engine

![coverage](coverage.svg)

A headless backend for an AI coding agent.

Engine runs as a long-lived process bound to one workspace. It exposes a
newline-delimited JSON protocol over a Unix domain socket, so any client — a
TUI, a web app, an editor plugin, a test harness — can drive an LLM coding
agent without importing a single line of agent internals. The orchestrator
spawns named personalities (ask, coder, tester, …) that share tools for
reading, searching, understanding, and editing code, backed by
tree-sitter for instant syntax queries and the Language Server Protocol for
real type information.

The design goal is a hard boundary. Clients speak JSON; they never touch the
orchestrator, the tool registry, or the LLM client. Everything crossing the
socket is a dataclass with a `type` field.

---

## Table of contents

- [Why this exists](#why-this-exists)
- [Quick start](#quick-start)
- [Architecture](#architecture)
- [The protocol](#the-protocol)
- [The agent loop](#the-agent-loop)
  - [What a pull request says](#what-a-pull-request-says)
- [Tools](#tools)
- [The write funnel](#the-write-funnel)
- [Language support](#language-support)
- [Persistence](#persistence)
- [Configuration](#configuration)
- [TypeSafe judge](#typesafe-judge)
- [The reference client](#the-reference-client)
- [Testing](#testing)
- [Project layout](#project-layout)
- [Operational limits](#operational-limits)
- [Extending the engine](#extending-the-engine)
- [Implementation plans](#implementation-plans)
- [Troubleshooting](#troubleshooting)

---

## Why this exists

Most agent frameworks fuse the model loop, the tool implementations, and the
user interface into one process. That makes the interesting part — the tool
layer that actually touches your code — hard to test and impossible to reuse.

Engine separates them:

- **The core** (`EngineSession`) owns the workspace, the agent loop, the tool
  registry, and session state.
- **The contract** (`protocol/`) is a set of JSON dataclasses. Commands come
  in, events go out. Nothing else crosses the line.
- **The transport** (`EngineServer`) is NDJSON over a Unix socket today. The
  same message types would work over a WebSocket or HTTP without touching the
  core.

The practical payoff is that the write path can be paranoid. Because editing
is funnelled through one module rather than scattered across tool
implementations, every write gets staleness checks, a symlink and denylist
guard, newline and encoding preservation, a tree-sitter syntax gate, an atomic
replace, and a journal entry that makes it undoable. A tool author writes a
pure `str -> str` function and inherits all of it.

---

## Quick start

### Requirements

| Requirement | Notes |
|---|---|
| Python 3.10+ | Required by `typesafe-sdk` (the TypeSafe judge integration); the protocol layer relies on runtime PEP 604 unions |
| An OpenRouter API key | Required for the agent loop; the engine boots without one but chat is disabled |
| A TypeSafe API key | Optional; enables the judge (see [TypeSafe judge](#typesafe-judge)). Absent, the engine runs exactly as it does without it |
| `rg` (ripgrep) | Required by the `search` tool |
| `git` | Optional; enables git state reporting and improves language detection |
| `gh` (GitHub CLI) | Optional; GitHub tools (`gh_pr_*`, `github_search_code`, …). Authenticate with `gh auth login`. |
| Node.js / `npx` | Optional; needed for the Python and TypeScript language servers |
| `gopls` | Optional; needed for Go language server support |

A Unix-like OS is required — the transport is an `AF_UNIX` socket.

### Install

```bash
git clone <your-remote> engine
cd engine
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt       # or requirements-dev.txt for tests and lint
```

### Configure

Create `env.sh` in the workspace root. It is gitignored.

```bash
export OPENROUTER_API_KEY="sk-or-v1-..."
export OPENROUTER_MODEL="anthropic/claude-sonnet-5"
```

The engine reads `env.sh` from the workspace at startup, but real environment
variables win — a value already exported in your shell is never overwritten by
the file.

### Run

Start the server against a workspace (defaults to the current directory):

```bash
python app.py /path/to/your/project
```

It prints a startup banner and begins listening:

```
=== Engine Server Startup ===
workspace: /path/to/your/project
engine dir: /path/to/your/project/.engine
db path: /path/to/your/project/.engine/session.db
socket path: /path/to/your/project/.engine/engine.sock
python version: 3.11.9
platform: macOS-14.5-arm64
==============================
listening on /path/to/your/project/.engine/engine.sock
```

In a second terminal, attach the reference client. It opens a three-panel
TUI and starts a session on connect:

```bash
python -m clients.dummy /path/to/your/project
```

Type in the input bar at the bottom:

```
openfile src/main.py
where is the retry logic in this codebase?
undo
```

`SIGINT` or `SIGTERM` closes the active session cleanly, shuts down any
language servers, and unlinks the socket.

---

## Architecture

```
                    ┌──────────────────────────────────────────┐
   client(s) ──────►│  EngineServer            (runtime/server) │
   NDJSON over      │  one Unix socket, many concurrent clients │
   AF_UNIX          │  reads commands  ·  fans out events       │
                    └────────────────────┬─────────────────────┘
                                         │ decode_command / encode
                    ┌────────────────────▼─────────────────────┐
                    │  EngineSession          (runtime/session) │
                    │  state · subscribers · snapshots · LSP    │
                    └────┬──────────────────────────┬───────────┘
                         │ HANDLERS[type(command)]  │ emits Events
              ┌──────────▼──────────┐               │
              │  runtime/commands   │               │
              │  lifecycle·files·git│               │
              └──────────┬──────────┘               │
                         │                          │
              ┌──────────▼──────────┐               │
              │ Orchestrator        │───────────────┘
              │  spawns personalities│
              └────┬───────────┬────┘
                   │           │
      ┌────────────▼──┐   ┌────▼─────────────────────────────┐
      │ OpenRouterLLM │   │ ToolRegistry  (tools/)           │
      └───────────────┘   └────┬─────────────────────────────┘
                               │ every write goes through one funnel
                          ┌────▼──────────────────────────────┐
                          │ runtime/tools/edits.py            │
                          │ stale check → guard → syntax gate │
                          │ → atomic write → journal → diags  │
                          └────┬──────────────┬───────────────┘
                               │              │
                        ┌──────▼─────┐  ┌─────▼──────────┐
                        │ SQLite     │  │ LSPManager     │
                        │ .engine/   │  │ pyright·tsls·  │
                        │ session.db │  │ gopls          │
                        └────────────┘  └────────────────┘
```

### Concurrency model

The server is a single asyncio event loop. Each connected client gets two
tasks — one reading commands, one writing events from a per-client
`asyncio.Queue`. When either finishes, the other is cancelled and the client is
unsubscribed. `ConnectionError` and `BrokenPipeError` are swallowed; anything
else propagates.

Events fan out to every subscriber, so multiple clients watching the same
workspace all see the same stream. Commands from any client mutate the one
shared session.

Blocking work is kept off the loop. Language server requests run under
`asyncio.to_thread`, and the LSP warm start happens on a daemon thread so a
slow `pyright` boot never delays the first command. The write funnel's
`_prepare` / `_commit` / `_apply_sync` path is deliberately synchronous and
must stay that way: awaiting mid-write would open a read-modify-write race
between the staleness check and the atomic replace.

---

## The protocol

One JSON object per line, terminated by `\n`. Every message carries a `type`
field naming its dataclass. `None` fields are omitted on the wire.

Full reference: [docs/protocol.md](docs/protocol.md).

---

## The agent loop

The user talks only to the **orchestrator** (`agents/orchestrator.py`), which is an `AgentLoop` with no filesystem tools — only one tool per subagent personality (`ask`, `coder`, `tester`, `researcher`, `debugger`, `reviewer`) plus `remember` and `settle_worktree`. Personalities are discovered from `agents/profiles/` the same way tools are discovered from `tools/`.

A spawn is fire-and-forget. The personality tool returns immediately with `agent_id` (and `worktree` / `branch` for writers). The child runs in the background with a fresh history and an allowlisted tool set. When it finishes, `compress_for_parent` turns its transcript into an `AgentResult` (`status`, `summary`, `outcome`, `files_touched`, `leftover_questions`, `missing_checks`). `files_touched` is successful edits, not reads. `leftover_questions` is parsed from labeled `leftover:` / `leftover_questions:` lines in the LLM report (or the child's closer). That string is posted to the orch as an `engine` chat line and, if the orch is idle, starts a follow-up orch turn so it can brief the user or spawn the next step. Child tokens stream live as `ChatMessageStarted` / `ChatMessageDelta` / `ChatMessageAdded` with `agent_id` set; they never persist in orch chat history.

`AgentLoop` is still an OpenAI-style tool-calling loop. The orch is capped at `EngineConfig.max_turns` (default 16). Each child uses its profile `max_turns` (default 32). Hitting the cap is a checkpoint, not a kill: the engine asks **continue** (same history, another `turn_slice` of 16, at most `max_continues` of 3), **handoff** (orch may spawn one writer with leftover), or **stop**. `ENGINE_TURN_CONTINUE=never` skips the prompt and hands off. The orch may emit several personality calls in one model turn; those children run concurrently. A child's own tools stay sequential so read-before-write cannot race. `EngineConfig.max_spawns_per_turn` (default 8) caps how many children may be live at once.

`coder` and `tester` run in a git worktree (`workspace/.engine/worktrees/<agent_id>` on branch `engine/<profile>/<agent_id>`) so two writers — or a writer and your dirty checkout — do not collide. `reviewer` joins that worktree so `git_diff` sees the writer's changes. When the writer finishes, uncommitted edits are committed on that branch, then the engine prompts to **merge**, **open a PR**, **keep**, or **discard**. Natural-language replies such as "please merge it" count. A `WorktreeSettled` event and an `engine` chat line report what happened. Empty worktrees (no unique commits and a clean tree) are removed without asking. After a keep — or if the prompt was missed — the orch must call `settle_worktree` rather than spawn another coder; writers cannot check out the user's branch. Leftover engine worktrees are recovered on session start so a later merge/PR still finds them. `ask`, `researcher`, and `debugger` use the main workspace. If the workspace is not a git repo, spawn still starts on the main tree.

### What a pull request says

`settle_worktree pr` writes its title and body with **one extra model call**,
made at settle time from the facts rather than from chat: the original task
prompt verbatim, `git diff --stat` against the merge base, and what each writer
reported. The model returns `{"title", "body"}`; the title is at most 72
characters, cut on a word boundary if longer. A reply that is not JSON, is
missing a field, or whose body asks you something is rejected, and so is a
failed call — a bad reply never fails a settle.

The fallback is deterministic. It uses a summary set apart in the
orchestrator's last reply (a block between `---` rules or a `>` blockquote,
provided it never asks you anything), else the original task prompt plus the
stat. It never uses a lone writer's report. Either way the body is checked
against the stat: any changed top-level path it doesn't mention gets a
**Files changed** section built from the stat, with no model involved, so the
call can't quietly drop part of the diff. Commit subjects are cut on a word
boundary too.

You can keep talking to the orch while children run: a second `SubmitUserMessage` is queued if the orch is mid-reply, then played when that reply finishes. `AbortAgent` with no id cancels only the orch's current reply; with `agent_id` it cancels that child. Session shutdown still aborts every child and removes live worktrees.

If any orch turn raises, the loop truncates history back to a marker taken before the user message was appended. A child crash becomes `status=failed` on the follow-up report and does not truncate orch history.

Resuming a session hydrates **orch** chat only. Tool messages and subagent transcripts are not rehydrated.

The **coder** system prompt still encodes the cheaper-first cost hierarchy:

1. `search` or `list_files` to locate a file.
2. `list_symbols` to see what is in it.
3. `find_symbol` for one definition's source, which also returns a 1-based
   name position.
4. That position feeds `goto_definition`, `find_references`, or `hover` for
   cross-file and type questions.
5. `get_diagnostics` for type and lint errors.
6. `read_file` windows for surrounding context.

The prompt also states the editing contract the tools enforce anyway: always
`read_file` before editing, prefer `str_replace` with enough context to be
unique, use `rename_symbol` rather than search-and-replace on identifiers, and
call `undo_edit` when something goes wrong.

---

## Tools

There is no registration list. `discover_tools()` walks the `tools/` package
with `pkgutil`, imports and reloads every module except `base.py`,
`registry.py`, and anything starting with `_`, then collects any object
carrying an `_engine_tool` attribute.

Full reference: [docs/tools.md](docs/tools.md).

---

## The write funnel

Every write in the engine goes through `runtime/tools/edits.py`. Tools supply
a `str -> str` mutation; the funnel supplies the safety.

Full reference: [docs/write-funnel.md](docs/write-funnel.md).

---

## Language support

At startup the engine identifies the workspace language. It prefers
`git ls-files` for the file list and falls back to a filesystem walk capped at
20,000 files, skipping the standard ignore directories.

Full reference: [docs/language-support.md](docs/language-support.md).

---

## Persistence

Everything lives in `{workspace}/.engine/`:

| Path | Contents |
|---|---|
| `engine.sock` | Unix domain socket. Removed on clean shutdown. |
| `session.db` | SQLite: `sessions` and `edits` tables. |
| `memory.json` | Structured workspace memory: file notes (`purpose` / `entry_points` / `constraints`) keyed by SHA-256, plus engineering / product / CI/CD / other decisions. Filled by `remember` and by ingesting ask/coder/researcher briefings. Injected into every agent prompt. |

The `sessions` table holds `id`, `json`, `created_at`, `saved_at`. Saves are
upserts. Only durable state is persisted — the file tree, git state, and
detected language are stripped before writing, since all three are recomputed
from disk on load. Sessions are listed newest-saved-first.

State is persisted after every user message, agent reply, file open, file
close, and on shutdown. File notes and decisions are written when an agent
calls `remember`, and when ask / coder / researcher finish (the labeled
briefing is ingested with no extra model call). `touch` on `read_file` and
successful edits only refreshes `seen_sha` for files that already have a
note; empty touches are not stored. A file note is marked `STALE` when the
current disk hash does not match the hash stored with the note.

---

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` | — | Required for chat. Placeholder values (`...`, `your-key`, `changeme`, `<OPENROUTER_API_KEY>`) are treated as unset. |
| `OPENROUTER_MODEL` | `openai/gpt-4o-mini` | Any OpenRouter model with tool-calling support. |
| `OPENROUTER_CHILD_MODEL` | (unset) | Fallback model for profiles that do not set `AgentProfile.model`. |
| `ENGINE_MODEL_CHEAP` / `ENGINE_MODEL_STRONG` | (both unset; inherit `OPENROUTER_MODEL`) | Phase 5's intent router picks `ENGINE_MODEL_STRONG` for turns classified `edit` + multi-file; nothing changes until set. |
| `ENGINE_LLM_STREAM` | `1` | Set `0` to disable token streaming. |
| `ENGINE_LLM_TIMEOUT_S` | `600` | LLM request timeout. |
| `ENGINE_LLM_IDLE_S` | `90` | Stream idle timeout. |
| `ENGINE_MAX_TURNS` | `16` | Tool-calling turns per user message. |
| `ENGINE_MAX_SPAWNS_PER_TURN` | `8` | Live concurrent subagents (not reset each orch reply). |
| `ENGINE_EXEC_APPROVAL` | `auto` (`judged` if a TypeSafe key is set) | `auto`, `always`, `never`, or `judged`. |
| `ENGINE_EXEC_TIMEOUT_S` | `120` | Default `run_command` timeout. |
| `ENGINE_EXEC_FILE_LIMIT_MB` | `2048` | `ulimit -f` cap (POSIX 512-byte blocks). |
| `ENGINE_CONTEXT_BUDGET` | `120000` | Compaction trigger budget. |
| `ENGINE_PUSHGATEWAY_URL` | (unset) | Prometheus Pushgateway base URL. Unset disables pushes; metrics still accumulate in-process. |
| `ENGINE_METRICS_JOB` | `engine` | Pushgateway job name. Grouping key `instance` is the session id unless overridden. |
| `ENGINE_METRICS_INSTANCE` | (session id) | Pushgateway grouping key `instance`. Set to a stable name (e.g. `baseline`) when comparing runs. |
| `ENGINE_METRICS_PUSH_INTERVAL_S` | `2` | Debounce between pushes; turn end, agent finish, and session close always flush. |
| `TYPESAFE_API_KEY` | — | Enables the judge (see below). `TYPESAFE_JEV_API_KEY` is accepted as an alias. Placeholder values (`...`, `your-key`, `changeme`) are treated as unset, same as `OPENROUTER_API_KEY`. |
| `ENGINE_JUDGE` | `advisory` | `off`, `advisory`, `calibrated`, or `enforcing`. `advisory` emits events only. `calibrated` is the recommended one-line profile: enforce exec / search / screen / intent, plus write (secret-block only — see below); merge stays advisory. |
| `ENGINE_JUDGE_MODEL` | `jev-latest` | TypeSafe model string. |
| `ENGINE_JUDGE_TIMEOUT_MS` | `800` | Hard per-call ceiling; a slow judge degrades to no opinion, not a slow turn. |
| `ENGINE_JUDGE_CACHE_SIZE` | `512` | LRU entries keyed by a hash of state + questions. |
| `ENGINE_JUDGE_EXEC` / `_TOOLS` / `_SEARCH` / `_SCREEN` / `_COMPACTION` / `_DIAGNOSTICS` / `_LOOP` / `_INTENT` / `_WRITE` / `_MERGE` | (inherits `ENGINE_JUDGE`, except `_WRITE` — see below) | Per-site override, so e.g. exec approval can enforce while result screening stays advisory. |

Set them in the environment or in `env.sh` at the workspace root. `env.sh`
parsing is deliberately minimal — it handles `export`, `#` comments, and quoted
values, and it never overwrites a variable already set to a real value in the
environment.

Without a key the engine still starts and serves file, tree, git, snapshot, and
undo commands. Only `SubmitUserMessage` fails, with a clear error.

Keep `env.sh` out of version control. It is in `.gitignore`, and the write
guard refuses to let the agent write to it. The same guard covers
`TYPESAFE_API_KEY` for free, since it denies writes by filename, not by
variable.

---

## TypeSafe judge

The engine can optionally consult [TypeSafe](https://typesafe.ai)'s System
One API (`typesafe-sdk`, model `jev-latest`) at a handful of choke points:
`run_command` approval, tool-call verification, search re-ranking,
tool-result screening for prompt injection, compaction-by-relevance,
post-write diagnostics triage, loop progress control, intent routing
(a "locate" turn can resolve via search + rerank and seed `ask` with
`run_with_context`, never the orchestrator's own history; see
`agents/resolver.py`), a semantic write gate, and a
subagent merge gate (scores a subagent's result — drop / summarize / admit
in full — before it re-enters the orchestrator's context; see
`Orchestrator._apply_merge_gate`). TypeSafe is a fast
calibrated classifier, not an agent — the engine sends one `state` blob plus typed
questions (`Noul` for yes/no, `Choice` for picking one of a closed set,
`Score` for an ordinal rating) and gets back a probability and confidence per
question. It never generates an edit, a search query, or a file path;
**the LLM generates, TypeSafe judges, code decides.**

Full reference: [docs/typesafe-judge.md](docs/typesafe-judge.md).

---

## The reference client

`clients/dummy.py` launches a small Textual TUI (`clients/tui.py`) that connects
to the socket, starts a session, and splits the event stream into three
panels. It is the executable specification of the protocol — worth reading
before writing your own client.

Full reference: [docs/reference-client.md](docs/reference-client.md).

---

## Testing

`pytest.ini` sets `pythonpath = .` and `testpaths = tests`, so no install step
is needed. The default run is capped at 4 pytest-xdist workers (`-n logical
--maxprocesses=4 --dist loadfile`); CI overrides that with `-n 2 --dist
loadscope`. Uncapped `-n auto` on a 12-core machine used to leave several
multi-gigabyte Python processes behind after the suite (or after Ctrl-C).
Pass `-n0` to disable workers entirely for a single-file debug run.

Full reference: [docs/testing.md](docs/testing.md).

---

## Project layout

```
app.py                  entry point: parse args, boot session + server, install signal handlers
clients/
  dummy.py              reference client: command parser + entry (python -m clients.dummy)
  headless.py           unattended --message --auto driver
  tui.py                Textual 3-panel UI: chat, protocol, tools
env.sh                  API key and model (gitignored)
requirements.txt        runtime dependencies
requirements-dev.txt    test and lint dependencies
pyproject.toml          ruff configuration
pytest.ini              pythonpath, testpaths, the lsp marker

protocol/               the wire contract — no engine logic
  message.py            ProtocolMessage base: to_json/from_json
  commands.py           11 client→engine dataclasses + COMMANDS registry
  events.py             22 engine→client dataclasses + EVENTS registry
  snapshot.py           EngineSnapshot, ChatMessage, SessionSummary, FileTreeNode, GitState, Stats, PendingPrompt
  codec.py              NDJSON encode/decode, 8 MiB STREAM_LIMIT, ProtocolError

runtime/
  server.py             EngineServer: Unix socket, per-client read/write tasks
  session.py            EngineSession: state, subscribers, snapshots, turn task, LSP lifecycle
  config.py             EngineConfig.from_env — process-wide knobs, loaded once
  subscriber.py         Bounded event queue with delta-drop policy
  prompts.py            PromptBroker: ask / answer / cancel_all
  language.py           workspace language detection
  commands/             one handler per command, registered via @handles
    lifecycle.py          StartSession, ListSessions, SubmitUserMessage, RequestSnapshot, Shutdown
    files.py              OpenFile, CloseFile, UndoLastEdit
    git.py                RequestGit
    agent.py              AbortAgent, AnswerPrompt
  store/
    sqlite.py             sessions table: init, save, load, list
    state.py              SessionState in-memory model
    edits.py              edits table: record, recent, last_batch
    memory.py             structured workspace memory (files + decisions)
  tools/                implementation layer — no LLM schemas here
    edits.py              THE WRITE FUNNEL: primitives, patches, atomic writes, journal, undo
    fileid.py             FileSource, newline/BOM/indent detection, guard_write_path
    tracker.py            FileTracker: path → SHA of last read/write
    fs.py                 workspace path resolution, tree listing, read windows
    sitter.py             tree-sitter parsing, queries, symbol edits, syntax gate
    lsp.py                LSPClient + LSPManager: JSON-RPC, lifecycle, diagnostics, rename
    search.py             ripgrep wrapper
    git.py                git state, log/show/blame/range, tracked paths
    github.py             gh wrappers: PRs, issues, Actions, code search
    pkg.py docs.py osv.py registries, official docs, OSV advisories
    http.py               structured HTTP + OpenAPI list
    scan.py envinfo.py dep_why.py  TODOs, local versions, lockfile why
    shell.py              asyncio subprocess executor for run_command
    web.py                HTTP fetch and Brave search
    browser.py            Playwright headless browser (optional)
    writeglob.py          profile write-path globs

tools/                  LLM-facing tool definitions — thin wrappers over runtime/tools
  base.py               @tool decorator, ToolContext, schema inference
  registry.py           ToolRegistry, discover_tools, subset, 80k result cap
  read_file.py list_files.py search.py sitter.py lsp.py
  edit_file.py edit_symbol.py apply_patch.py undo.py
  shell.py              run_command
  git.py github.py web.py browser.py skills.py remember.py
  pkg.py docs.py osv.py http.py scan.py runtime_info.py dep_why.py

agents/
  agent_loop.py         shared tool-calling loop
  orchestrator.py       user-facing coordinator, spawn tools
  subagent.py           personality instance
  profile.py            AgentProfile, ProfileRegistry, discover_profiles
  profiles/             ask, coder, tester, reviewer, researcher, debugger
  hooks.py              AgentHooks callbacks
  compactor.py          mid-loop compact + compress_for_parent

runtime/skills/         SKILL.md discovery + lexical catalog
runtime/mcp/            mcp.json client, tool bridge, token store

llm/
  provider.py           LLMProvider Protocol, Usage, LLMResult, ToolCall
  openrouter.py         OpenRouterLLM streaming client, env.sh loading

tests/                  unit tests (write path, runtime, skills, MCP fakes)

docs/
  adding-a-*.md         how to add a tool, command, profile, skill, MCP server
  protocol.md tools.md write-funnel.md language-support.md
  typesafe-judge.md reference-client.md testing.md
                        reference sections split out of this README
  judge.md              judge design: the phased checks the judge runs
  archive/impl-plans/   historical design plans, in build order
```

The split between `runtime/tools/` and `tools/` is deliberate.
`runtime/tools/` holds real implementations with real signatures, testable
without an LLM in the loop. `tools/` holds thin wrappers whose job is to
describe those implementations to a model. The suite exercises
`runtime/tools/` directly, which is why it runs in seconds without an API key.

---

## Operational limits

| Limit | Value | Where |
|---|---|---|
| NDJSON line | 8 MiB | `protocol/codec.py` |
| Agent tool turns | orch 16, children 32; cap is a continue/handoff/stop checkpoint (slice 16, max 3 continues) | `EngineConfig.max_turns` / `turn_slice` / `max_continues` / `AgentProfile.max_turns` |
| Live subagents | 8 | `EngineConfig.max_spawns_per_turn` |
| Subscriber buffer | 4096 items / 1 MiB | `runtime/subscriber.py` |
| Per-event soft limit | 512 KiB | `EVENT_SOFT_LIMIT` |
| NDJSON fuse | 8 MiB | `STREAM_LIMIT` — last resort, not a design target |
| Command timeout | 120s default, 600s max | `runtime/tools/shell.py` |
| Command output | 30k / stream, 60k total | `runtime/tools/shell.py` |
| Context budget | 120,000 tokens | `EngineConfig.context_budget` |
| Compact trigger / keep full tools | orch 0.7 / 3; children never compact in-loop (`trigger=2.0`), overflow fuse + `compress_for_parent` on finish | `EngineConfig.compact_trigger` / `keep_full_tools` |
| Child models | tester `anthropic/claude-haiku-4.5`; ask and others inherit `OPENROUTER_MODEL` | `AgentProfile.model` / `OPENROUTER_CHILD_MODEL` |
| Survey spawn cap | first user turn may fan out; leftover inbox turns spawn at most one `ask` and one `researcher` | `Orchestrator.reset_user_message_spawns` |
| `github_file` window | 12,000 chars default, 50,000 hard cap | `runtime/tools/github.py` |
| Child report summary / outcome | 400 / 32,000 chars; a clipped closer is `incomplete` so ask/researcher may respawn once | `SUMMARY_CLIP` / `OUTCOME_CLIP` |
| Tool result to model | 80,000 chars | `tools/registry.py` |
| Tool preview in events | 400 chars | `runtime/session.py` |
| `read_file` window | 200 default, 400 max | `runtime/tools/fs.py` |
| Search matches | 80 default, 200 max | `runtime/tools/search.py` |
| LSP index | 500 files | `runtime/tools/lsp.py` |
| References / definitions / symbols | 50 / 20 / 200 | `runtime/tools/lsp.py` |
| Tree-sitter query captures | 80 | `runtime/tools/sitter.py` |
| `parse_file` nodes / depth | 200 / 8 | `runtime/tools/sitter.py` |
| Diff in a tool result | 4,000 chars | `runtime/tools/edits.py` |
| Patch hunk fuzz | 5 lines | `runtime/tools/edits.py` |
| Language detection walk | 20,000 files | `runtime/language.py` |
| Subprocess timeout (git, rg) | 10s | `runtime/tools/{git,search}.py` |

Ignored everywhere: `.git`, `.engine`, `.cursor`, `__pycache__`,
`node_modules`, `.venv`, `venv`, `.ruff_cache`, and other cache/build dirs
(see `SKIP_NAMES` / `should_skip_name` in `runtime/tools/fs.py`).

---

## Extending the engine

**A new tool.** Drop a module in `tools/`, decorate with `@tool`, restart the
session. See [Writing a new tool](docs/tools.md#writing-a-new-tool).

**A new subagent personality.** Drop a module in `agents/profiles/` that
exports `PROFILE = AgentProfile(...)`. The orch sees it as a tool. See
[docs/adding-a-profile.md](docs/adding-a-profile.md).

**A skill.** Drop `{name}/SKILL.md` under `.engine/skills/` (or `.cursor/skills/`).
See [docs/adding-a-skill.md](docs/adding-a-skill.md).

**An MCP server.** Add an entry to `.engine/mcp.json`. The engine is the MCP
client; agents see `mcp_{server}_{tool}`. See
[docs/adding-an-mcp-server.md](docs/adding-an-mcp-server.md).

**A new command.** Add a `@command` dataclass to `protocol/commands.py`, write
a `@handles(YourCommand)` function in `runtime/commands/`, and import it from
that package's `__init__`. The codec picks it up from the registry
automatically.

**A new event.** Add an `@event` dataclass to `protocol/events.py` and emit it
via `session._emit()`. Clients that do not know the type will fall back to raw
JSON rather than breaking.

**A new language.** Add extensions and root markers to `runtime/language.py`,
add the language to `SUPPORTED`, wire a tree-sitter grammar into
`runtime/tools/sitter.py`, and add a server config to `runtime/tools/lsp.py`.

**A new transport.** `EngineServer` only depends on `subscribe()`,
`unsubscribe()`, and `handle()`. A WebSocket or HTTP-plus-SSE server
implementing the same three calls needs no changes to the core.

**A different LLM provider.** `AgentLoop` needs one method:
`complete(messages, tools) -> LLMResult`. Implement that against any
tool-calling API and pass it in place of `OpenRouterLLM`.

---

## Implementation plans

Design notes for how the engine was built, in order, live in
[docs/archive/impl-plans/](docs/archive/impl-plans/README.md). They are historical — the
how-to guides above are the source of truth for extending the running
system.

---

## Troubleshooting

**`no server socket at .../engine.sock`** — the engine is not running, or it is
running against a different workspace. Start it with `python app.py <workspace>`
and make sure both sides point at the same directory.

**`set OPENROUTER_API_KEY`** — no key, or a placeholder value. Put a real key in
`env.sh` or export it. Note that a real environment variable takes precedence
over `env.sh`, so a stale export in your shell will shadow the file.

**`read {path} before editing it`** — working as intended. The agent must
`read_file` a path before it can edit it.

**`{path} changed on disk since you read it`** — also working as intended.
Something modified the file after the agent read it. Have the agent re-read and
retry.

**LSP tools return "no language server"** — either the project language is
unsupported (only Python, Go, and JavaScript/TypeScript have servers), or the
binary is missing. `npx` is needed for `pyright` and
`typescript-language-server`; `gopls` must be on `PATH`. The tree-sitter tools
work regardless and are the intended fallback.

**First LSP call is slow or empty** — warm start indexes up to 500 files and the
first `npx` invocation may download a package. Diagnostics fill in as the server
catches up.

**`search` fails** — `rg` is not on `PATH`. Install ripgrep.

**Undo refuses** — the file's SHA no longer matches what the journal recorded,
meaning it changed after the agent's edit. Undo will not discard those changes.
Revert manually or use `list_edits` to see the recorded diff.

**A tool is missing after you added it** — check the client for an
`ErrorOccurred` naming your module; import errors and duplicate tool names are
reported there rather than crashing the session. Discovery only reruns on
`StartSession`.
