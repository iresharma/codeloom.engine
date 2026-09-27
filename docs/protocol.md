# The protocol

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

## Commands (client → engine)

| Command | Fields | Effect |
|---|---|---|
| `StartSession` | `workspace`, `session_id?` | Starts a new session or resumes a stored one. Binds the agent loop, starts language servers, emits a snapshot. |
| `ListSessions` | — | Returns stored sessions, most recently saved first. |
| `SubmitUserMessage` | `text` | Runs an orchestrator turn. If the orch is already answering, the message is queued and played when that reply finishes. Children already running are not blocked. |
| `RequestSnapshot` | `replay?` | Re-emits session state. Default `replay=true` also streams history, open files, and the file tree. `replay=false` is the base `SnapshotReady` only (no messages). |
| `RequestOrchContext` | — | Returns the orch's current model context (system + notes + history). |
| `OpenFile` | `path` | Adds a file to the open set and returns its contents. |
| `CloseFile` | `path` | Removes a file from the open set. |
| `RequestGit` | — | Returns current git state including diffs. |
| `UndoLastEdit` | — | Reverts the most recent agent edit batch. |
| `AbortAgent` | `agent_id?` | With no id, cancels the in-flight orchestrator reply only (children keep working). With `agent_id`, cancels that subagent. |
| `AnswerPrompt` | `prompt_id`, `text` | Resolves a `UserPromptRequested` (command approval, etc.). |
| `Shutdown` | — | Ends the session, persists it, stops language servers. |

`StartSession` rejects a `workspace` that does not match the one the server was
booted with — one process serves exactly one workspace.

## Events (engine → client)

| Event | Fields | Meaning |
|---|---|---|
| `SnapshotReady` | `snapshot` | Full session state, with chat history stripped and streamed separately. |
| `ChatMessageAdded` | `id`, `role`, `text`, `ts`, `agent_id?` | A new message. `role` is `user`, `assistant`, `engine` (child report), or `tool`. Child assistant lines set `agent_id` and are not stored in orch history. |
| `ChatHistoryAdded` | `id`, `role`, `text`, `ts`, `index`, `total` | One replayed historical message, so clients can show progress. |
| `ChatHistoryComplete` | `count` | Replay finished. |
| `SessionList` | `sessions` | Result of `ListSessions`. |
| `FileContent` | `path`, `content` | Full contents of an opened or externally-changed file. |
| `FileEdited` | `path`, `diff`, `tool`, `edit_id` | An agent edit landed, with its unified diff. |
| `FileClosed` | `path` | A file left the open set. |
| `FileTreeUpdated` | `file_tree` | Workspace tree. Arrives after `SnapshotReady` (which no longer packs the tree) and after creates/undos. |
| `GitStateUpdated` | `git` | Branch, dirty flag, staged/unstaged/untracked lists, diffs. |
| `ChatMessageStarted` | `id`, `role`, `ts`, `agent_id?` | An assistant message is about to stream. Empty `agent_id` is the orchestrator. |
| `ChatMessageDelta` | `id`, `channel`, `text`, `agent_id?` | Incremental text or reasoning. The following `ChatMessageAdded` is canonical. |
| `ToolCallStarted` / `ToolCallFinished` | `call_id`, `name`, … | A tool began or finished. Replaces `role=tool` chat lines. |
| `CommandOutputChunk` | `call_id`, `stream`, `text` | Live stdout/stderr from `run_command`. |
| `AgentStateChanged` | `state`, `turn`, `max_turns`, `agent_id?` | idle / thinking / calling_tool / waiting_for_user / aborting / compacting. Empty `agent_id` is the orchestrator. `waiting_for_user` means **this** agent's prompt is on screen; another child queued on PromptBroker still shows `calling_tool`. |
| `AgentStarted` | `agent_id`, `profile`, `parent_id`, `task`, `worktree?`, `branch?`, `batch_id?`, `batch_name?` | A subagent began. Writers include the git worktree path and branch. Children spawned in the same orch reply share `batch_id` and a nickname from the user message. |
| `AgentFinished` | `agent_id`, `profile`, `status`, `summary` | A subagent returned a compacted result. |
| `AgentsUpdated` | `agents` | Full live agent list (`AgentRow`: id, profile, status, task, current_tool, batch_id, batch_name, worktree, branch). Emit after start, finish, or status/tool change. |
| `OrchContext` | `text` | The orch's current model context, for the client's debug popup. |
| `WorktreeSettled` | `agent_id`, `profile`, `action`, `detail`, `branch`, `pr_url?`, `ok` | The user chose merge / pr / keep / discard for a finished writer worktree. |
| `StatsUpdated` | `stats` | Tokens, cost, elapsed time. |
| `UserPromptRequested` | `prompt_id`, `question`, `kind`, `choices`, `agent_id?` | The engine is waiting on the user (command approval, etc.). |
| `ContextCompacted` | `strategy`, counts, `summary` | History was trimmed or summarized. |
| `ErrorOccurred` | `message` | Recoverable error. Never terminates the connection. |
| `WarningOccurred` | `message` | Advisory, e.g. an unsupported project language. |
| `JudgementMade` | `tag`, `subject`, `outcome`, `signals`, `enforced`, `latency_ms`, `agent_id?` | A TypeSafe verdict at a choke point (`exec_approval`, `call_verify`, `search_rerank`, `result_screen`). `enforced` is false in advisory mode. |
| `SessionEnded` | `reason` | Session closed. |

`tool` role messages are previews: the engine truncates tool output to 400
characters before emitting, and the registry independently caps any tool result
at 80,000 characters before it reaches the model.

## Snapshots and reconnection

`EngineSnapshot` is the reconnect payload: `session_id`, `workspace`,
`messages`, `ended`, `open_files`, `file_tree`, `git`, `language`,
`language_supported`, `message_count`, `agents` (live subagents with task,
status, `current_tool`, and `batch_id`).

Emitting it is a small dance designed to keep a large history from blocking the
event loop:

1. Open files that no longer exist on disk are dropped from the set.
2. Any in-flight history replay is cancelled via a generation counter.
3. `SnapshotReady` goes out with `messages` emptied and `message_count` set, so
   the client can size its UI immediately.
4. One `FileContent` per open file.
5. History replays as individual `ChatHistoryAdded` events from a background
   task that yields between messages, ending with `ChatHistoryComplete`.

`RequestSnapshot(replay=false)` stops after step 3: just the base
`SnapshotReady` (counts, git, agents, stats). No file contents, no history
replay. The TUI snapshot button uses that and shows the summary in a popup.

The generation counter matters: if a client requests a second snapshot while
the first replay is still streaming, the stale task notices the bumped
generation and stops rather than interleaving two histories.
