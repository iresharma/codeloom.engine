# Engine

A headless backend for an AI coding agent.

Engine runs as a long-lived process bound to one workspace. It exposes a
newline-delimited JSON protocol over a Unix domain socket, so any client — a
TUI, a web app, an editor plugin, a test harness — can drive an LLM coding
agent without importing a single line of agent internals. The agent gets 24
tools for reading, searching, understanding, and editing code, backed by
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
  - [Commands](#commands-client--engine)
  - [Events](#events-engine--client)
  - [Snapshots and reconnection](#snapshots-and-reconnection)
- [The agent loop](#the-agent-loop)
- [Tools](#tools)
  - [Discovery and reload](#discovery-and-reload)
  - [The full tool catalogue](#the-full-tool-catalogue)
  - [Writing a new tool](#writing-a-new-tool)
- [The write funnel](#the-write-funnel)
  - [Read-before-edit](#read-before-edit)
  - [The write guard](#the-write-guard)
  - [File identity preservation](#file-identity-preservation)
  - [The syntax gate](#the-syntax-gate)
  - [Atomic writes and the edit journal](#atomic-writes-and-the-edit-journal)
  - [Undo](#undo)
- [Language support](#language-support)
  - [Detection](#detection)
  - [Tree-sitter](#tree-sitter)
  - [Language servers](#language-servers)
- [Persistence](#persistence)
- [Configuration](#configuration)
- [The reference client](#the-reference-client)
- [Testing](#testing)
- [Project layout](#project-layout)
- [Operational limits](#operational-limits)
- [Extending the engine](#extending-the-engine)
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
| Python 3.9+ | The protocol layer has an explicit fallback for 3.9's lack of runtime PEP 604 unions |
| An OpenRouter API key | Required for the agent loop; the engine boots without one but chat is disabled |
| `rg` (ripgrep) | Required by the `search` tool |
| `git` | Optional; enables git state reporting and improves language detection |
| Node.js / `npx` | Optional; needed for the Python and TypeScript language servers |
| `gopls` | Optional; needed for Go language server support |

A Unix-like OS is required — the transport is an `AF_UNIX` socket.

### Install

```bash
git clone <your-remote> engine
cd engine
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
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

In a second terminal, attach the reference client:

```bash
python dummy_client.py /path/to/your/project
```

Then drive it:

```
engine> start
engine> openfile src/main.py
engine> where is the retry logic in this codebase?
engine> undo
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
              │  AgentLoop (agents) │───────────────┘
              │  ≤8 tool turns      │
              └────┬───────────┬────┘
                   │           │
      ┌────────────▼──┐   ┌────▼─────────────────────────────┐
      │ OpenRouterLLM │   │ ToolRegistry  (tools/)  24 tools │
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

The line limit is 8 MiB (`STREAM_LIMIT`), well above asyncio's 64 KiB default,
because a snapshot of a large repository does not fit in 64 KiB. If an encoded
event would exceed the limit, the engine substitutes an `ErrorOccurred`
explaining the drop rather than corrupting the stream.

Decoding is registry-driven. `@command` and `@event` decorators populate
`COMMANDS` and `EVENTS` dicts keyed by class name, and `decode_command` /
`decode_event` dispatch on the `type` field. Unknown types, non-object
payloads, and malformed JSON all raise `ProtocolError`, which the server
reports as an `ErrorOccurred` without dropping the connection.

### Commands (client → engine)

| Command | Fields | Effect |
|---|---|---|
| `StartSession` | `workspace`, `session_id?` | Starts a new session or resumes a stored one. Binds the agent loop, starts language servers, emits a snapshot. |
| `ListSessions` | — | Returns stored sessions, most recently saved first. |
| `SubmitUserMessage` | `text` | Runs the agent loop against the message. |
| `RequestSnapshot` | — | Re-emits full session state. |
| `OpenFile` | `path` | Adds a file to the open set and returns its contents. |
| `CloseFile` | `path` | Removes a file from the open set. |
| `RequestGit` | — | Returns current git state including diffs. |
| `UndoLastEdit` | — | Reverts the most recent agent edit batch. |
| `AbortAgent` | `agent_id?` | Cancels the in-flight agent turn. `agent_id` is reserved for future subagents. |
| `AnswerPrompt` | `prompt_id`, `text` | Resolves a `UserPromptRequested` (command approval, etc.). |
| `Shutdown` | — | Ends the session, persists it, stops language servers. |

`StartSession` rejects a `workspace` that does not match the one the server was
booted with — one process serves exactly one workspace.

### Events (engine → client)

| Event | Fields | Meaning |
|---|---|---|
| `SnapshotReady` | `snapshot` | Full session state, with chat history stripped and streamed separately. |
| `ChatMessageAdded` | `id`, `role`, `text`, `ts` | A new message. `role` is `user`, `assistant`, or `tool`. |
| `ChatHistoryAdded` | `id`, `role`, `text`, `ts`, `index`, `total` | One replayed historical message, so clients can show progress. |
| `ChatHistoryComplete` | `count` | Replay finished. |
| `SessionList` | `sessions` | Result of `ListSessions`. |
| `FileContent` | `path`, `content` | Full contents of an opened or externally-changed file. |
| `FileEdited` | `path`, `diff`, `tool`, `edit_id` | An agent edit landed, with its unified diff. |
| `FileClosed` | `path` | A file left the open set. |
| `FileTreeUpdated` | `file_tree` | Workspace tree. Arrives after `SnapshotReady` (which no longer packs the tree) and after creates/undos. |
| `GitStateUpdated` | `git` | Branch, dirty flag, staged/unstaged/untracked lists, diffs. |
| `ChatMessageStarted` | `id`, `role`, `ts` | An assistant message is about to stream. |
| `ChatMessageDelta` | `id`, `channel`, `text` | Incremental text or reasoning. The following `ChatMessageAdded` is canonical. |
| `ToolCallStarted` / `ToolCallFinished` | `call_id`, `name`, … | A tool began or finished. Replaces `role=tool` chat lines. |
| `CommandOutputChunk` | `call_id`, `stream`, `text` | Live stdout/stderr from `run_command`. |
| `AgentStateChanged` | `state`, `turn`, `max_turns` | idle / thinking / calling_tool / waiting_for_user / aborting / compacting. |
| `StatsUpdated` | `stats` | Tokens, cost, elapsed time. |
| `UserPromptRequested` | `prompt_id`, `question`, `kind`, `choices` | The agent is waiting on the user. |
| `ContextCompacted` | `strategy`, counts, `summary` | History was trimmed or summarized. |
| `ErrorOccurred` | `message` | Recoverable error. Never terminates the connection. |
| `WarningOccurred` | `message` | Advisory, e.g. an unsupported project language. |
| `SessionEnded` | `reason` | Session closed. |

`tool` role messages are previews: the engine truncates tool output to 400
characters before emitting, and the registry independently caps any tool result
at 80,000 characters before it reaches the model.

### Snapshots and reconnection

`EngineSnapshot` is the reconnect payload: `session_id`, `workspace`,
`messages`, `ended`, `open_files`, `file_tree`, `git`, `language`,
`language_supported`, `message_count`.

Emitting it is a small dance designed to keep a large history from blocking the
event loop:

1. Open files that no longer exist on disk are dropped from the set.
2. Any in-flight history replay is cancelled via a generation counter.
3. `SnapshotReady` goes out with `messages` emptied and `message_count` set, so
   the client can size its UI immediately.
4. One `FileContent` per open file.
5. History replays as individual `ChatHistoryAdded` events from a background
   task that yields between messages, ending with `ChatHistoryComplete`.

The generation counter matters: if a client requests a second snapshot while
the first replay is still streaming, the stale task notices the bumped
generation and stops rather than interleaving two histories.

---

## The agent loop

`AgentLoop` (`agents/agent_loop.py`) is a straightforward OpenAI-style
tool-calling loop, capped at `EngineConfig.max_turns` (default 16). Each turn
either produces tool calls — which are executed and appended as `tool`
messages — or a final text answer. Exhausting the cap returns
`stopped after N tool turns`. A turn runs as an asyncio task so `AbortAgent`
can be read from the same client. One turn at a time; a second
`SubmitUserMessage` is refused until the current turn finishes or is aborted.

If any turn raises, the loop truncates history back to a marker taken before
the user message was appended. A failed exchange leaves no partial state
behind, so the next message starts from a coherent history.

Resuming a session calls `hydrate()`, which replays stored `user` and
`assistant` messages into the loop's history. Tool messages are not rehydrated:
their results are stale by the time a session resumes, and replaying them would
mislead the model about current file contents.

The system prompt encodes a deliberate cost hierarchy — cheap, instant tools
first, expensive ones only when needed:

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

### Discovery and reload

There is no registration list. `discover_tools()` walks the `tools/` package
with `pkgutil`, imports and reloads every module except `base.py`,
`registry.py`, and anything starting with `_`, then collects any object
carrying an `_engine_tool` attribute.

Import failures are captured as registry errors and surfaced to the client as
`ErrorOccurred` rather than crashing the session, so one broken tool module
does not take down the engine. Duplicate tool names are rejected the same way.

Because discovery runs on every `StartSession` and uses `importlib.reload`, you
can edit a tool and pick it up by restarting the session — no server restart.

### The full tool catalogue

24 tools in seven families.

**Navigation** — no language server needed.

| Tool | Purpose |
|---|---|
| `list_files` | Every workspace-relative path, one per line. |
| `read_file` | A numbered line window. Defaults to 200 lines from offset 1, capped at 400. Marks the file as read. |
| `search` | ripgrep across the workspace. Returns `path:line:text`. Default 80 matches, hard cap 200. |

**Tree-sitter** — instant, no server, works on partially broken files.

| Tool | Purpose |
|---|---|
| `list_symbols` | Outline of functions, classes, methods, types, imports. |
| `find_symbol` | One named definition's source plus the 1-based line/character of its name — the coordinate handoff into the LSP tools. |
| `get_node_at` | The node at a position: type, name, parent, enclosing definition, named children. |
| `query_tree` | A tree-sitter query. Presets: `imports`, `functions`, `classes`, `methods`, `calls`. Capped at 80 captures. |
| `parse_file` | Compact nested syntax tree with line ranges. Capped at 200 nodes and depth 8. |

**Language server** — real types, cross-file truth.

| Tool | Purpose |
|---|---|
| `goto_definition` | Resolve a symbol at a position. Understands imports and types. Max 20 locations. |
| `find_references` | Every usage across the indexed workspace. Max 50. |
| `hover` | Type signature and documentation. |
| `get_diagnostics` | Compiler and type-checker errors, warnings, hints. |
| `document_symbols` | Server-side outline with `SymbolKind`, catching interfaces and enums tree-sitter may miss. Max 200. |
| `rename_symbol` | Server-computed rename applied across every affected file as one undoable batch. |

**Text editing.**

| Tool | Purpose |
|---|---|
| `str_replace` | Replace one unique exact substring. Zero or multiple matches fail rather than guess. |
| `replace_lines` | Replace an inclusive 1-based line range, mirroring the `read_file` window. |
| `insert_at_line` | Insert before a 1-based line, or at `end+1` to append. |
| `create_file` | Create a new file. Fails if the path exists. Creates parent directories. |

**Structural editing.**

| Tool | Purpose |
|---|---|
| `replace_symbol` | Replace a whole function/class/type by name via tree-sitter. Robust to whitespace differences. |
| `insert_after_imports` | Insert after the last import block, or at the top if there are none. |
| `apply_patch` | Apply a unified diff. Line-number drift up to 5 lines is tolerated; any failing hunk rejects the entire patch. |

**Execution.**

| Tool | Purpose |
|---|---|
| `run_command` | Shell command in the workspace. No TTY. Default 120s timeout. Non-zero exit is information. |

**History.**

| Tool | Purpose |
|---|---|
| `undo_edit` | Revert the last edit batch, including multi-file renames. |
| `list_edits` | Recent edits in this session with their diffs. Default 20. |

### Writing a new tool

Drop a module in `tools/` and decorate a function. Nothing else.

```python
from tools.base import ToolContext, tool


@tool(description="Count lines in a workspace file.")
def count_lines(ctx: ToolContext, path: str) -> str:
    _rel, text = read_text(ctx.workspace, path)
    return str(len(text.splitlines()))
```

The JSON schema is inferred from the signature. Type hints map to JSON types,
`Optional[X]` unwraps to `X`, parameters without defaults become required, and
`ctx` / `context` / `self` are excluded. Pass `parameters={...}` explicitly when
you want richer descriptions per field, which every built-in tool does.

At call time only arguments matching the signature are forwarded — a model
hallucinating an extra keyword gets it dropped rather than causing a
`TypeError`. Sync and async functions both work. A `None` return becomes an
empty string, and exceptions become `error: {message}` strings so a tool crash
becomes something the model can read and react to instead of an aborted turn.

For anything that writes, do not touch the filesystem directly. Write a pure
mutation and hand it to the funnel:

```python
from runtime.tools.edits import apply_edit


@tool(description="Strip trailing whitespace from a file.")
async def strip_trailing(ctx: ToolContext, path: str) -> str:
    def mutate(src):
        return "\n".join(line.rstrip() for line in src.text.splitlines())

    return await apply_edit(ctx, path, mutate, "strip_trailing")
```

That single call buys the staleness check, the write guard, newline and
encoding preservation, the syntax gate, an atomic replace, a journal entry, the
`FileEdited` event, and undo support.

---

## The write funnel

Every write in the engine goes through `runtime/tools/edits.py`. Tools supply
a `str -> str` mutation; the funnel supplies the safety.

### Read-before-edit

`FileTracker` maps each workspace-relative path to the SHA-256 of the bytes
last read or written. `read_file` marks a file; the funnel then enforces two
rules:

- No recorded SHA → `error: read {path} before editing it`. The agent cannot
  edit a file it has not looked at.
- Recorded SHA differs from disk → the file changed underneath the agent, so
  the edit is refused rather than clobbering someone else's work.

The tracker is recreated on every session bind, so a resumed session starts
with no assumptions about what is on disk.

### The write guard

`guard_write_path()` refuses, before any bytes move:

- Writes through symlinks.
- Paths that resolve outside the workspace. Resolution happens first, then a
  `relative_to(workspace)` check, so `../` traversal and symlink escapes both
  fail.
- Anything inside `.git`, `.engine`, `.cursor`, `__pycache__`, `node_modules`,
  `.venv`, or `venv`.
- Lockfiles: `package-lock.json`, `uv.lock`, `poetry.lock`, `Cargo.lock`,
  `go.sum`.
- Secrets: `env.sh`, `.env`, and any `.env.*`.

### File identity preservation

`FileSource` captures how a file is actually written on disk — line ending
(`\n`, `\r\n`, or `\r`), UTF-8 BOM, whether it ends with a trailing newline,
and the dominant indent (tab, or 2/3/4/8 spaces). Mutations operate on
normalized text; `render()` re-applies the original identity on the way out.

A CRLF file stays CRLF. A file with no final newline keeps not having one. A
BOM survives. New files inherit their identity from a sibling with the same
extension, so a new `.py` next to CRLF Python files gets CRLF too.

Non-UTF-8 files are rejected outright rather than silently mangled.

### The syntax gate

Before committing, the funnel parses the new text with tree-sitter and compares
its `ERROR` and `MISSING` nodes against the old text's. An edit that introduces
*new* syntax faults is rejected.

The comparison is what makes this usable. A file that was already broken can
still be edited — otherwise the agent could never fix a syntax error. Only
newly introduced breakage is blocked.

### Atomic writes and the edit journal

Writes go to a temporary file, get `fsync`'d, then `os.replace` into position.
Creates use a temp file plus a hard link so an existing path cannot be
clobbered by a race. A reader never sees a half-written file.

Each committed edit is journaled to the `edits` table:

| Column | Contents |
|---|---|
| `id` | Autoincrement primary key |
| `session_id` | Owning session |
| `batch_id` | Groups multi-file edits such as a rename |
| `path` | Workspace-relative path |
| `tool` | Tool that produced the edit |
| `before` / `after` | Full byte snapshots. `NULL` before means the file did not exist; `NULL` after means it was deleted |
| `before_sha` / `after_sha` | SHA-256 of each side |
| `diff` | Unified diff |
| `created_dirs` | JSON array of directories created for this edit |
| `applied_at` | ISO-8601 UTC timestamp |

Storing full before/after bytes rather than diffs makes undo exact and
independent of patch application.

### Undo

`undo_edit` loads the most recent batch and reverses it, newest record first:

- A create becomes a delete, and any directories created for it are pruned.
- A delete becomes a restore from `before`.
- An edit restores the `before` bytes.

Before touching anything, undo verifies that each file's current SHA still
matches the recorded `after_sha`. If you edited a file by hand after the agent
touched it, undo refuses rather than discarding your change.

The undo itself is journaled with `tool="undo_edit"` and a fresh `batch_id`, so
the history stays append-only. Multi-file batches are all-or-nothing: a partial
failure rolls back the files already committed using their journal records.

---

## Language support

### Detection

At startup the engine identifies the workspace language. It prefers
`git ls-files` for the file list and falls back to a filesystem walk capped at
20,000 files, skipping the standard ignore directories.

It then counts files by extension and looks for root markers — `go.mod`,
`pyproject.toml`, `requirements.txt`, `package.json`, `tsconfig.json`,
`yarn.lock`, and friends. A single unambiguous marker wins, unless another
language has at least five files and more than twice the marked language's
count, which catches the polyglot repo whose `package.json` is incidental.

`python`, `go`, and `javascript` (TypeScript included) are supported. Fourteen
other languages — Rust, Ruby, Java, Kotlin, Swift, C, C++, C#, PHP,
Scala, Haskell, Elixir, Lua, Zig — are detected and named, but get no
tree-sitter or LSP support. Unsupported projects still work; the engine emits a
`WarningOccurred` and the agent falls back to `read_file` and `search`.

### Tree-sitter

Grammars for Python, Go, JavaScript, TypeScript, and TSX load lazily from the
`tree-sitter-*` packages in `requirements.txt`. Tree-sitter powers the outline
tools, the symbol-scoped edits, and the syntax gate. It is fast enough to run
on every write and tolerant of broken files, which is exactly what a syntax
gate needs.

### Language servers

| Language | Command | Language IDs |
|---|---|---|
| Python | `npx -y -p pyright pyright-langserver --stdio` | `python` |
| TypeScript | `npx -y typescript-language-server --stdio` | `typescript`, `typescriptreact` |
| JavaScript | `npx -y typescript-language-server --stdio` | `javascript`, `javascriptreact` |
| Go | `gopls serve` | `go` |

`LSPManager` keeps one client per distinct command, so JavaScript and
TypeScript share a single `typescript-language-server` process. `LSPClient`
speaks JSON-RPC over stdio with a background reader thread.

Warm start runs on a daemon thread at session bind: it initializes the server,
opens up to 500 workspace files so cross-file references resolve, and waits up
to 20 seconds for the first diagnostics. The session never blocks on it.

Timeouts: 30s for `initialize`, 15s for a normal request, 5s for `shutdown`
and for the process to exit before it is killed, 20s for warm-start
diagnostics, 5s for diagnostics after a change.

Document sync is SHA-based. The manager records the SHA of the text last sent
for each file, and `sync_if_stale()` pushes a `didChange` when disk has moved
on. After every write the funnel asks for fresh diagnostics and reports any
that are new, so the agent hears about the type error it just introduced in the
same tool result.

Rename is the one place the LSP writes. `textDocument/rename` returns a
workspace edit, which is normalized and applied through
`apply_workspace_edit()` as a single atomic, undoable batch. The engine never
lets the server touch disk directly.

---

## Persistence

Everything lives in `{workspace}/.engine/`:

| Path | Contents |
|---|---|
| `engine.sock` | Unix domain socket. Removed on clean shutdown. |
| `session.db` | SQLite: `sessions` and `edits` tables. |
| `context.md` | Long-term agent memory, appended across sessions. |

The `sessions` table holds `id`, `json`, `created_at`, `saved_at`. Saves are
upserts. Only durable state is persisted — the file tree, git state, and
detected language are stripped before writing, since all three are recomputed
from disk on load. Sessions are listed newest-saved-first.

State is persisted after every user message, agent reply, file open, file
close, and on shutdown.

---

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` | — | Required for chat. Placeholder values (`...`, `your-key`, `changeme`, `<OPENROUTER_API_KEY>`) are treated as unset. |
| `OPENROUTER_MODEL` | `openai/gpt-4o-mini` | Any OpenRouter model with tool-calling support. |
| `ENGINE_LLM_STREAM` | `1` | Set `0` to disable token streaming. |
| `ENGINE_LLM_TIMEOUT_S` | `600` | LLM request timeout. |
| `ENGINE_LLM_IDLE_S` | `90` | Stream idle timeout. |
| `ENGINE_MAX_TURNS` | `16` | Tool-calling turns per user message. |
| `ENGINE_EXEC_APPROVAL` | `auto` | `auto`, `always`, or `never`. |
| `ENGINE_EXEC_TIMEOUT_S` | `120` | Default `run_command` timeout. |
| `ENGINE_EXEC_FILE_LIMIT_MB` | `2048` | `ulimit -f` cap (POSIX 512-byte blocks). |
| `ENGINE_CONTEXT_BUDGET` | `120000` | Compaction trigger budget. |

Set them in the environment or in `env.sh` at the workspace root. `env.sh`
parsing is deliberately minimal — it handles `export`, `#` comments, and quoted
values, and it never overwrites a variable already set to a real value in the
environment.

Without a key the engine still starts and serves file, tree, git, snapshot, and
undo commands. Only `SubmitUserMessage` fails, with a clear error.

Keep `env.sh` out of version control. It is in `.gitignore`, and the write
guard refuses to let the agent write to it.

---

## The reference client

`dummy_client.py` is a small asyncio REPL that connects to the socket, sends
one command per line, and pretty-prints the event stream from a concurrent
reader task. It is the executable specification of the protocol — worth reading
before writing your own client.

```bash
python dummy_client.py [workspace]
```

At the `engine> ` prompt:

| Input | Sends |
|---|---|
| `help` | Command list, generated from the `COMMANDS` registry |
| `start [session_id]` | `StartSession` — omit the id for a fresh session |
| `listsessions` | `ListSessions` |
| `requestsnapshot` | `RequestSnapshot` |
| `openfile <path>` | `OpenFile` |
| `closefile <path>` | `CloseFile` |
| `requestgit` | `RequestGit` |
| `undo` | `UndoLastEdit` |
| `shutdown` | `Shutdown` |
| `exit` / `quit` | Disconnects the client; the server keeps running |
| *anything else* | `SubmitUserMessage` with the whole line as text |

A leading `/` is optional and command names are case-insensitive, so `/start`,
`start`, and `Start` are equivalent. Because unrecognized input becomes a chat
message, you can just type `where is the retry logic?` and hit enter. Ctrl-D
exits.

The client formats each event type for readability: snapshots collapse to a
summary line, file contents show the first 24 lines, and diffs show the first
80. Unknown event types fall back to pretty-printed JSON, so a client built
against an older protocol version still shows you something useful.

Writing your own client is three steps: open a Unix socket connection to
`{workspace}/.engine/engine.sock` with an 8 MiB stream limit, write
`json.dumps(command) + "\n"`, and read newline-delimited events in a loop. The
`protocol/` package is importable standalone if your client is also Python.

---

## Testing

```bash
pytest                      # everything
pytest -m "not lsp"         # skip tests that spawn a real language server
pytest tests/test_apply.py  # one module
```

`pytest.ini` sets `pythonpath = .` and `testpaths = tests`, so no install step
is needed.

Tests across 18 modules. The original write-path suite is unchanged; the new
modules cover the runtime foundation (config, streaming, turns, stats, shell,
prompts, compaction):

| Module | Covers |
|---|---|
| `test_primitives.py` | `str_replace`, `replace_lines`, `insert_at_line`, diff formatting |
| `test_syntax.py` | The syntax gate, including that already-broken files stay editable |
| `test_patch.py` | Unified diff parsing, fuzz offsets, all-or-nothing multi-hunk application |
| `test_identity_and_guard.py` | CRLF/BOM/trailing-newline round-trips, the write denylist, symlink refusal, sibling-inherited newlines |
| `test_apply.py` | Staleness detection, create conflicts, undo round-trip, directory pruning, read-before-edit |
| `test_symbols.py` | Tree-sitter symbol replacement and import insertion |
| `test_concurrency.py` | Concurrent edits to the same and different files — no hangs, exactly one winner per conflicting file |
| `test_tools_registry.py` | Tool discovery and workspace edits |
| `test_protocol.py` | Codec round-trips, snapshot and history streaming |
| `test_lsp_write.py` | Integration against real `gopls`: view refresh after writes, diagnostic resync, cross-file rename, undo batches |
| `test_config.py` | `EngineConfig.from_env`, `env.sh` ordering, malformed knobs |
| `test_llm_stream.py` | Streaming chunk assembly, idle timeout, usage extraction |
| `test_subscriber.py` | Bounded queues, delta-drop policy, size-field coverage |
| `test_turn_control.py` | Turn-as-task, busy refusal, abort mid-complete and between tools |
| `test_stats.py` | Usage accumulation and stats persistence |
| `test_shell.py` | `run_command` executor: denials, approval, output caps |
| `test_prompts.py` | `PromptBroker` ask/answer/cancel and confirm timeout |
| `test_compaction.py` | Tool-result trim, history invariant, overflow markers |

The `conftest.py` `ctx` fixture builds a `ToolContext` over `tmp_path` with a
real SQLite journal and a fresh `FileTracker`. The `seed()` helper writes a file
and marks it read, satisfying the read-before-edit guard.

`test_lsp_write.py` is marked `lsp` and skipped when `gopls` is absent. `gopls`
was chosen over the npx-based servers because it runs straight from `PATH`
without a package fetch, keeping the suite fast and hermetic. Everything else
runs offline with no external binaries — `test_concurrency.py` uses a `FakeLsp`
stub rather than a real server.

Linting has been run with ruff 0.16.5 using its defaults; no configuration file
is committed.

---

## Project layout

```
app.py                  entry point: parse args, boot session + server, install signal handlers
dummy_client.py         reference REPL client
env.sh                  API key and model (gitignored)
requirements.txt        runtime and test dependencies
pytest.ini              pythonpath, testpaths, the lsp marker

protocol/               the wire contract — no engine logic
  message.py            ProtocolMessage base: to_json/from_json, 3.9-safe hint resolution
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
  tools/                implementation layer — no LLM schemas here
    edits.py              THE WRITE FUNNEL: primitives, patches, atomic writes, journal, undo
    fileid.py             FileSource, newline/BOM/indent detection, guard_write_path
    tracker.py            FileTracker: path → SHA of last read/write
    fs.py                 workspace path resolution, tree listing, read windows
    sitter.py             tree-sitter parsing, queries, symbol edits, syntax gate
    lsp.py                LSPClient + LSPManager: JSON-RPC, lifecycle, diagnostics, rename
    search.py             ripgrep wrapper
    git.py                git state and tracked paths
    shell.py              asyncio subprocess executor for run_command

tools/                  LLM-facing tool definitions — thin wrappers over runtime/tools
  base.py               @tool decorator, ToolContext, schema inference
  registry.py           ToolRegistry, discover_tools, 80k result cap
  read_file.py list_files.py search.py sitter.py lsp.py
  edit_file.py edit_symbol.py apply_patch.py undo.py
  shell.py              run_command

agents/
  agent_loop.py         AgentLoop, DEFAULT_SYSTEM, max_turns from EngineConfig
  hooks.py              AgentHooks callbacks
  compactor.py          tool-result trim, summarization, overflow detection

llm/
  provider.py           LLMProvider Protocol, Usage, LLMResult, ToolCall
  openrouter.py         OpenRouterLLM streaming client, env.sh loading

tests/                  18 modules (write path + runtime foundation)
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
| Agent tool turns | 16 | `EngineConfig.max_turns` |
| Subscriber buffer | 4096 items / 1 MiB | `runtime/subscriber.py` |
| Per-event soft limit | 512 KiB | `EVENT_SOFT_LIMIT` |
| NDJSON fuse | 8 MiB | `STREAM_LIMIT` — last resort, not a design target |
| Command timeout | 120s default, 600s max | `runtime/tools/shell.py` |
| Command output | 30k / stream, 60k total | `runtime/tools/shell.py` |
| Context budget | 120,000 tokens | `EngineConfig.context_budget` |
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
`node_modules`, `.venv`, `venv`.

---

## Extending the engine

**A new tool.** Drop a module in `tools/`, decorate with `@tool`, restart the
session. See [Writing a new tool](#writing-a-new-tool).

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
