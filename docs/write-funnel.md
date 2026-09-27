# The write funnel

Every write in the engine goes through `runtime/tools/edits.py`. Tools supply
a `str -> str` mutation; the funnel supplies the safety.

## Read-before-edit

`FileTracker` maps each workspace-relative path to the SHA-256 of the bytes
last read or written. `read_file` marks a file; the funnel then enforces two
rules:

- No recorded SHA → `error: read {path} before editing it`. The agent cannot
  edit a file it has not looked at.
- Recorded SHA differs from disk → the file changed underneath the agent, so
  the edit is refused rather than clobbering someone else's work.

The tracker is recreated on every session bind, so a resumed session starts
with no assumptions about what is on disk.

## The write guard

`guard_write_path()` refuses, before any bytes move:

- Writes through symlinks.
- Paths that resolve outside the workspace. Resolution happens first, then a
  `relative_to(workspace)` check, so `../` traversal and symlink escapes both
  fail.
- Anything inside `.git`, `.engine`, `.cursor`, `__pycache__`, `node_modules`,
  `.venv`, `venv`, `.ruff_cache`, and other cache/build dirs.
- Lockfiles: `package-lock.json`, `uv.lock`, `poetry.lock`, `Cargo.lock`,
  `go.sum`.
- Secrets: `env.sh`, `.env`, and any `.env.*`.

## File identity preservation

`FileSource` captures how a file is actually written on disk — line ending
(`\n`, `\r\n`, or `\r`), UTF-8 BOM, whether it ends with a trailing newline,
and the dominant indent (tab, or 2/3/4/8 spaces). Mutations operate on
normalized text; `render()` re-applies the original identity on the way out.

A CRLF file stays CRLF. A file with no final newline keeps not having one. A
BOM survives. New files inherit their identity from a sibling with the same
extension, so a new `.py` next to CRLF Python files gets CRLF too.

Non-UTF-8 files are rejected outright rather than silently mangled.

## The syntax gate

Before committing, the funnel parses the new text with tree-sitter and compares
its `ERROR` and `MISSING` nodes against the old text's. An edit that introduces
*new* syntax faults is rejected.

The comparison is what makes this usable. A file that was already broken can
still be edited — otherwise the agent could never fix a syntax error. Only
newly introduced breakage is blocked.

## Atomic writes and the edit journal

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

## Undo

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
