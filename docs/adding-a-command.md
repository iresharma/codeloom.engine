# Adding a new command

Commands are the client → server half of the engine's Unix-socket protocol
(NDJSON over `.engine/engine.sock`); events are the server → client half. This
doc covers adding a **command**. If you also need a new outbound message, see
the analogous `@event` pattern in `protocol/events.py` (same idea, registered
in `EVENTS` instead of `COMMANDS`).

Like tools, commands are declared once and picked up by decorator-based
registries — no central dispatch `if/elif` chain to extend.

## The pieces

- `protocol/message.py` — `ProtocolMessage`, the base dataclass every command
  and event inherits. Gives you `to_json()`/`from_json()` for free based on
  dataclass fields and type hints (including `Optional[X]` and `list[X]` of
  another `ProtocolMessage`).
- `protocol/commands.py` — one `@command @dataclass` class per command. The
  `@command` decorator registers the class in `COMMANDS: dict[str, type]` by
  class name, which `decode_command` uses to route an incoming `{"type": ...}`
  JSON object back to the right dataclass.
- `protocol/codec.py` — `encode`/`decode_command`/`decode_event`. NDJSON: one
  JSON object per line, always with a `"type"` field.
- `runtime/commands/register.py` — `HANDLERS: dict[type, callable]` and the
  `@handles(CommandType)` decorator that fills it.
- `runtime/commands/*.py` — handler implementations, grouped by area
  (`lifecycle.py`, `files.py`, `git.py`, `agent.py`). Add a new function to an
  existing module if your command fits, or add a new module.
- `runtime/commands/__init__.py` — imports every handler module for its side
  effects (so the `@handles` decorators run and populate `HANDLERS`). **You
  must add your new module here if you create one** — this is the one place
  that isn't fully automatic (unlike `tools/`, which walks the package
  directory itself).
- `runtime/session.py` — `EngineSession.handle(command)` looks up
  `HANDLERS[type(command)]` and calls it (awaiting if async). This is also
  where most of the state your handler will touch lives (`_state`, `_workspace`,
  `_emit`, `_persist`, ...).

## Minimal example

```python
# protocol/commands.py
@command
@dataclass
class RenameSession(ProtocolMessage):
    name: str
```

```python
# runtime/commands/lifecycle.py (or a new module)
from protocol.commands import RenameSession
from protocol.events import ErrorOccurred
from runtime.commands.register import handles


@handles(RenameSession)
def rename_session(session, command: RenameSession) -> None:
    if not session._require_session():
        return
    if not command.name.strip():
        session._emit(ErrorOccurred(message="name must not be empty"))
        return
    session._state.name = command.name.strip()
    session._persist()
```

If it's a new module, add it to `runtime/commands/__init__.py`:

```python
from runtime.commands import lifecycle as _lifecycle  # noqa: F401
from runtime.commands import my_module as _my_module  # noqa: F401  # add this
```

That's the whole integration — the codec already knows how to decode
`RenameSession` (dataclass fields + `@command`), and `EngineSession.handle`
already knows how to route it to `rename_session` (via `HANDLERS`).

## Defining the command dataclass

- Inherit `ProtocolMessage`, decorate with `@command` **then** `@dataclass`
  (order matters: `@command` reads `cls.__name__` after the dataclass is
  built, so `@dataclass` must be the inner decorator, applied first).
- Field types matter — `from_json`/`to_json` use them:
  - Plain `str`/`int`/`float`/`bool`/`list[str]` round-trip as-is.
  - `X | None` fields are omitted from the JSON entirely when `None`
    (`to_json` skips `None` values) and decode back to `None` from either a
    missing key or an empty string.
  - A field typed as another `ProtocolMessage` subclass (or `list[...]` of
    one) is recursively encoded/decoded via that class's own `to_json`/
    `from_json` — see `SnapshotReady.snapshot: EngineSnapshot` or
    `AnswerPrompt`/`UserPromptRequested` for nested-object precedent.
  - No custom types beyond dataclasses/primitives/lists — the codec has no
    hook for arbitrary encoders.
- Zero-field commands are still dataclasses with `pass` (`ListSessions`,
  `RequestSnapshot`, `Shutdown`) — keep that even if there's nothing to carry,
  so the type still exists for lookup/round-trip/handler dispatch.
- Naming: the class name **is** the wire `"type"` value and doubles as the
  registry key. Don't rename a shipped command lightly — it's part of the
  client/server contract (`dummy_client.py` and any real client match on these
  names).

Round-trip test convention (see `tests/test_protocol.py`):

```python
def test_rename_session_round_trip():
    parsed = decode_command(encode(RenameSession(name="foo")))
    assert isinstance(parsed, RenameSession)
    assert parsed.name == "foo"
```

## Writing the handler

Signature: `def handler(session: EngineSession, command: CommandType) -> None`
(or `async def ... -> None` if you need to `await` anything — `handle()`
awaits the result automatically if it's awaitable; look at
`undo_last_edit`/`shutdown` for the async pattern and `open_file`/`abort_agent`
for the sync one).

Conventions every existing handler follows:

- **Guard first.** Nearly every handler starts with:
  ```python
  if not session._require_session():
      return
  ```
  This checks `session._state.session_id is not None` and emits
  `ErrorOccurred("no active session; start one first")` itself if not — don't
  duplicate that message, just return early on `False`. The only handlers that
  skip this are ones that make sense with no active session (`StartSession`
  itself, `ListSessions`).
- **Never raise for expected failures.** Path errors, unknown ids, bad input —
  catch them and `session._emit(ErrorOccurred(message=...))`, then `return`.
  A handler is still allowed to let a genuinely unexpected exception propagate;
  `EngineServer._read_commands` catches anything a handler raises and turns it
  into `ErrorOccurred(f"{CommandType} failed: {exc}")` so the connection
  survives, but that's a fallback, not something to rely on for normal
  validation failures.
- **Emit, don't return.** Handlers return `None`; all output goes through
  `session._emit(some_event)`. A single command can emit zero, one, or several
  events (`RequestGit` emits `GitStateUpdated` and, conditionally, a
  `WarningOccurred`).
- **Persist state you changed.** If the handler mutates `session._state`,
  call `session._persist()` before/after emitting so a crash or reconnect
  doesn't lose it (see `open_file`, `close_file`, `start_session`).
- **Reuse `runtime/tools/*` helpers, don't reimplement.** File path handling
  (`resolve_in_workspace`, `relative_posix`, `WorkspacePathError`), edits
  (`runtime.tools.edits`), git (`runtime.tools.git.read_state`) — the command
  handlers are thin glue over the same primitives the agent's tools use, not a
  parallel implementation. `undo_last_edit` is the clearest example: it builds
  a `ToolContext` by hand and calls the exact same `runtime.tools.edits.undo_last`
  the `undo_edit` tool uses, so a human-triggered undo and an agent-triggered
  undo behave identically and share one journal.
- **Cap anything unbounded before emitting.** If your event payload could be
  large (a diff, file content, a big string), clip it yourself — see
  `request_git` clipping `staged_diff`/`unstaged_diff` to
  `EVENT_SOFT_LIMIT // 2` via `clip_text` and reporting the omitted bytes as a
  `WarningOccurred`. Don't rely on `EngineServer._write_events`' hard
  `STREAM_LIMIT` (8 MiB) drop-and-replace-with-error as your truncation
  strategy — that's a last-resort safety net, not a truncation feature.

## If your command needs a new event

Add it the same way, in `protocol/events.py`:

```python
@event
@dataclass
class SessionRenamed(ProtocolMessage):
    name: str
```

Then, if the event carries a payload that can be arbitrarily large (text,
diffs, lists), add it to `SIZE_FIELDS` in `runtime/subscriber.py` — that table
is what the per-client `Subscriber` queue uses to estimate memory pressure and
decide what to evict under backpressure. There's a comment there stating
explicitly: *"Adding a new event with a large string and not extending this
table is a test failure."* Small/enum-like string fields only need adding to
`SMALL_STRING_FIELDS` if they're not already covered by the flat-256 fallback.
Check `tests/test_subscriber.py` for the pattern that enforces this.

## Handler `session` object cheat sheet

The `session` argument is the live `EngineSession`. Frequently used members:

| Member | What it's for |
|---|---|
| `session._workspace` | resolved `Path` the whole session is bound to |
| `session._state` | current `SessionState` (open files, messages, session id) |
| `session._require_session()` | guard: emits error + `False` if no session started |
| `session._emit(event)` | broadcast an event to every connected client |
| `session._persist()` | write `_state` to sqlite (no-op if no session id) |
| `session._config` | `EngineConfig` |
| `session.language` | detected `LanguageInfo` for the workspace |
| `session._lsp` | `LSPManager` or `None` |
| `session._files` | shared `FileTracker` (read-before-write bookkeeping) |
| `session._db_path` | sqlite path, for building a `ToolContext` if a handler needs to call into `runtime/tools` |
| `session.start_turn(text)` / `session.abort_turn()` | agent turn control |
| `session._prompts` | `PromptBroker` for the ask-user/approval flow (`AnswerPrompt` uses this) |

## Registering & testing

Nothing to register in a lookup table by hand beyond the two decorators, but
remember the one manual step: **import your new handler module from
`runtime/commands/__init__.py`** if you added one, or the `@handles` decorator
never runs and `HANDLERS` won't contain your command — `session.handle()` will
emit `unknown command: YourCommand` even though the dataclass decodes fine.

Test both layers:

```python
# codec round-trip
def test_rename_session_round_trip():
    parsed = decode_command(encode(RenameSession(name="foo")))
    assert parsed.name == "foo"

# handler behavior, against a real EngineSession
async def run():
    session = EngineSession(tmp_path, tmp_path / "session.db")
    await session.start()
    queue = session.subscribe()
    await session.handle(StartSession(workspace=str(tmp_path)))
    await session.handle(RenameSession(name="foo"))
    ...
```

See `tests/test_protocol.py` for round-trip tests and
`tests/test_turn_control.py`/`tests/test_concurrency.py` for handler-level
tests that drive a real `EngineSession` end to end. `dummy_client.py` is also a
convenient manual smoke-test harness — it maps lowercase command names to
classes via `COMMANDS` and can send any registered command from the CLI.

## Checklist before adding a command

- [ ] Dataclass added to `protocol/commands.py`, decorated `@command` then
      `@dataclass`, fields typed precisely (`X | None`, `list[str]`, nested
      `ProtocolMessage`, not "whatever `Any`").
- [ ] Codec round-trip test added.
- [ ] Handler added under `runtime/commands/`, decorated `@handles(...)`.
- [ ] New handler module (if any) imported from `runtime/commands/__init__.py`.
- [ ] Handler guards with `session._require_session()` unless it's meant to
      work without an active session.
- [ ] All failure paths `_emit(ErrorOccurred(...))` and `return` instead of
      raising for anything a client could plausibly trigger.
- [ ] State mutations are followed by `session._persist()`.
- [ ] Any new/changed event payload that can be large is added to
      `SIZE_FIELDS` (and clipped at the source if unbounded) in
      `runtime/subscriber.py`.
- [ ] A handler-level test drives it through a real `EngineSession`, not just
      the codec round-trip.
