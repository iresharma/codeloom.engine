---
name: TUI client protocol
overview: "Client commands the product TUI needs: path mutations, full file open, structured context, memory events, agent transcripts, and git-on-write."
todos:
  - id: path-funnel
    content: Funnel delete/rename/mkdir plus CreatePath/RenamePath/DeletePath commands
    status: pending
  - id: open-full
    content: OpenFile returns full UTF-8 up to STREAM_LIMIT
    status: pending
  - id: context-memory
    content: RequestContext/ContextBreakdown and RequestMemory/MemoryUpdated
    status: pending
  - id: transcripts-git
    content: Session-scoped child transcripts and GitStateUpdated after writes
    status: pending
isProject: false
---

# TUI client protocol

The product TUI in `workspace/TUI` speaks NDJSON to `engine.sock`. This slice adds the commands that TUI cannot fake without bypassing the write funnel, the memory store, or child history.

Dummy client (`dummy_client.py`) stays a protocol debugger. `RequestOrchContext` still dumps a text blob.

## Path mutations

Funnel primitives in `runtime/tools/edits.py`, journaled and undoable (file rename is one batch). Client commands, not LLM tools:

- `CreatePath(path, is_dir=False, content="")`
- `RenamePath(src, dest)`
- `DeletePath(path)`

Guarded by `guard_write_path`. After success: `FileEdited` or `PathChanged`, `FileTreeUpdated`, `GitStateUpdated`.

## OpenFile

Interactive `OpenFile` emits `FileContent` up to `STREAM_LIMIT`. Non-UTF-8 is `ErrorOccurred`. Snapshot replay may still clip at `EVENT_SOFT_LIMIT`.

## Context

`RequestContext(agent_id="")` → `ContextBreakdown` with sections `system`, `memory`, `skills`, `tools`, `messages` (chars + `tokens_est = chars // 4`), `budget`, last `prompt_tokens`, `compacted`. Empty `agent_id` is the orchestrator.

## Memory

`RequestMemory` → `MemoryUpdated`. Also emitted after `remember`, `touch`, and `ingest_result`.

## Transcripts

Session-scoped buffer (live children + last 32 finished). `RequestAgentTranscript(agent_id)` replays `ChatHistoryAdded` with `agent_id` set. Not persisted to SQLite.

## Git

Every funnel commit and path command emits `GitStateUpdated` (clipped diffs).
