# The reference client

`clients/dummy.py` launches a small Textual TUI (`clients/tui.py`) that connects
to the socket, starts a session, and splits the event stream into three
panels. It is the executable specification of the protocol — worth reading
before writing your own client.

```
+---------------------------+----------------------+
|                           | Agents · N           |
|                           | nickname / profile   |
|  Agent chat               +----------------------+
|                           | Protocol             |
|                           | commands + events    |
|                           +----------------------+
|                           | Tools                |
|                           | name / args / result |
+---------------------------+----------------------+
| > type a message or /command   [snapshot] [context]
+--------------------------------------------------+
```

- **Chat** — user, assistant, and `engine` (child reports) messages, streamed
  deltas, history replay, and outstanding prompts. Assistant and engine
  replies render as markdown (headings, lists, code fences); user lines stay
  literal. An unclosed ` ``` ` fence stays as source until it closes so the
  rest of a stream is not swallowed as code.
- **Agents** — live subagents grouped by batch nickname (`batch_name`) plus a
  short `batch_id`: count, profile, task, status, current tool, worktree, and
  streamed child tokens (`ChatMessageStarted` / `Delta` / `Added` with
  `agent_id`). Fed by `AgentsUpdated`, `SnapshotReady`, and those chat events.
- **Protocol** — every command this client sends, plus inbound events that are
  not chat, tools, the agents panel, or an inspect popup (files, git, stats,
  errors, `AgentStarted` / `AgentFinished`). `JudgementMade` is logged here
  too; F7 filters the log to those events.
- **Tools** — live tool cards with arguments, shell chunks, status, duration,
  and the 400-char result preview. Cards tagged with `agent_id` when a child
  is calling the tool. Each `JudgementMade` is pinned here as a coloured
  judge card (`allow` / `prompt` / `block` / `ranked` / `flag`) so a
  verdict is visible next to the tool it gated.
- **Snapshot** — **snapshot** button / F5 / `snapshot` opens a popup of the
  base `SnapshotReady` (session, language, git, stats, agents, message count).
  It does not replay chat history.
- **Context** — **context** button / F6 / `context` command opens a popup of
  the orch's current model context (`OrchContext`: system prompt, workspace
  notes, history).
- **Judgements** — F7 filters the protocol log to `JudgementMade`. The same
  events are always pinned as coloured cards on the tools panel.

```bash
python -m clients.dummy [workspace]
python -m clients.dummy [workspace] --message "the task" --auto
python -m clients.dummy [workspace] --message "the task" --auto --timeout 1800
```

`--message --auto` is the headless driver: it starts a session, submits one user message, auto-answers prompts (exec/confirm `yes`, worktree settle `keep` unless `--settle pr|merge|discard`, MCP auth `no`, turn-cap `continue`), prints formatted events to stdout, and exits when the orchestrator has been idle for a second with no live children. `--timeout` is an optional wall-clock fuse (seconds); `0` or omitted waits until idle. `--auto` without `--message` is an error.

The input bar uses the same grammar as the old REPL:

| Input | Sends |
|---|---|
| `help` | Command list, generated from the `COMMANDS` registry |
| `start [session_id]` | `StartSession` — omit the id for a fresh session |
| `listsessions` | `ListSessions` |
| `requestsnapshot` / `snapshot` / `snap` | `RequestSnapshot(replay=false)` — **snapshot** button / F5 opens a popup of the base state (no history replay) |
| `context` / `orchcontext` | `RequestOrchContext` — also the **context** button and F6 |
| `openfile <path>` | `OpenFile` |
| `closefile <path>` | `CloseFile` |
| `requestgit` | `RequestGit` |
| `undo` | `UndoLastEdit` |
| `abort` | `AbortAgent` — orch reply only; children keep running |
| `abort <agent_id>` | `AbortAgent` for one subagent |
| `shutdown` | `Shutdown` |
| `exit` / `quit` | Disconnects the client; the server keeps running |
| *anything else* | `SubmitUserMessage` with the whole line as text |

A leading `/` is optional and command names are case-insensitive, so `/start`,
`start`, and `Start` are equivalent. Because unrecognized input becomes a chat
message, you can just type `where is the retry logic?` and hit enter. The
client sends `StartSession` on connect. Ctrl-C or `exit` disconnects.

The client formats each event type for readability: snapshots collapse to a
summary (including live `agents`), `AgentsUpdated` reprints the running set
grouped by batch, file contents show the first 24 lines, and diffs show the
first 80. Unknown event types fall back to pretty-printed JSON, so a client
built against an older protocol version still shows you something useful.

Writing your own client is three steps: open a Unix socket connection to
`{workspace}/.engine/engine.sock` with an 8 MiB stream limit, write
`json.dumps(command) + "\n"`, and read newline-delimited events in a loop. The
`protocol/` package is importable standalone if your client is also Python.
