---
name: Dummy debug client
overview: Add a single interactive REPL dummy client that connects to the existing Unix socket, pretty-prints live events, and includes local-state debug commands so you can exercise the engine without building a TUI.
todos:
  - id: client-core
    content: "Write dummy_client.py: Unix socket connect, hello snapshot, send/ack, live event reader"
    status: completed
  - id: repl
    content: REPL commands for all existing protocol cmds plus raw/quit/help
    status: completed
  - id: debug-tools
    content: LocalMirror, pretty printers, filter/quiet/log/check/tree/git/agents/stats/chat
    status: completed
isProject: false
---

# Dummy client and debug tools

One file, [`dummy_client.py`](../../dummy_client.py), at the repo root. Interactive REPL only (as you chose). No TUI, no protocol changes.

## How you run it

Terminal 1:

```bash
python app.py .
```

Terminal 2:

```bash
python dummy_client.py
```

Defaults to `<cwd>/.engine/engine.sock` (same as [`app.py`](../../app.py)). Override with `--socket`.

Optional `--spawn` starts `app.py` as a subprocess if the socket is missing, so you can debug in one terminal.

## What it does

Connects with `asyncio.open_unix_connection`, reads the hello snapshot (`session_ready` from [`runtime/server.py`](../../runtime/server.py)), then:

- a background task prints every event line as it arrives
- the prompt accepts short commands and sends the matching JSON (`{"id": "...", "cmd": "..."}`)

Acks (`{"ok": true, "id": ...}`) are labeled separately from pushed events so you can tell request/response from the fan-out stream.

## REPL commands (engine protocol)

These map 1:1 onto [`protocol/commands.py`](../../protocol/commands.py):

- `say <text>` / `prompt <text>` — `submit_user_message`
- `answer <prompt_id> <text>` — `answer_prompt`
- `open <path>` / `close <path>`
- `snap` — `request_snapshot`
- `abort [agent_id]` — omit id to abort the whole run
- `shutdown`
- `start <workspace>` — `start_session` (rebind)
- `raw {json}` — send a raw line (for breaking the protocol on purpose)
- `quit`

## Debug tools (client-side, no engine changes)

Keep a **local mirror** of session state, seeded from the hello snapshot and updated as events arrive (same way a TUI will). Commands:

- `tree` — last file tree
- `git` — branch, dirty, status lines, diff lengths (not a full dump)
- `agents` — id, profile, status, current_tool
- `stats` — tokens, elapsed, active agents
- `chat` — last messages
- `pending` — current `UserPromptRequested` if any
- `file` — last opened file path + first lines
- `events [n]` — last N raw events from a ring buffer (default 20)
- `filter [type]` — only print that event type (`chat_message_added`, `agent_updated`, …); `filter` with no arg clears
- `quiet` / `verbose` — hide/show live event spam
- `log [path]` — append every event as JSONL to `.engine/debug.jsonl` (or the given path)
- `check` — send `request_snapshot` and diff it against the local mirror; print mismatches (this is the protocol-debug tool)

Pretty-print events compactly, not full JSON every time:

```
[chat] user: hello
[agent] sub-ab12 linter running tool=read_file
[git] main dirty=true 3 files
[prompt] id=a1f3  Need a commit message
```

`snap` / `check` can print a slightly richer block.

## Layout

All of this stays in [`dummy_client.py`](../../dummy_client.py):

- `EngineClient` — connect, send, read loop
- `LocalMirror` — apply snapshot + events
- `pretty_*` helpers
- `Repl` — parse lines, dispatch

No new dependencies. Stdlib only (`asyncio`, `json`, `argparse`).

## Out of scope

- TUI widgets
- New engine commands
- Replay/record of a full session (JSONL log is enough for now)
