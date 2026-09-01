# Adding a new tool

This project auto-discovers tools: you drop a module under `tools/`, decorate a
function with `@tool`, and the engine picks it up on the next start (tests call
`discover_tools()` directly). There is no registry file to edit.

This doc explains the moving parts and the conventions to follow so a new tool
behaves consistently with the existing ones.

## The pieces

- `tools/base.py` — the `@tool` decorator, `Tool` dataclass (schema + dispatch),
  and `ToolContext` (what your function gets injected with).
- `tools/registry.py` — `discover_tools()` walks the `tools/` package, imports
  every module, and collects any function carrying an `_engine_tool` attribute
  (i.e. anything decorated with `@tool`). Two tools with the same name is a
  registration error, not a crash — it's recorded in `registry.errors`.
- `tools/*.py` — one module per tool (or a small family of related tools). By
  convention these are thin: they define the JSON schema/description and call
  into `runtime/tools/*.py` for the real implementation.
- `runtime/tools/*.py` — the actual logic (filesystem, git, shell, tree-sitter,
  LSP, editing). Framework-free: no `@tool`, no `ToolContext` awareness beyond
  taking a `Path`/plain args, so it's independently testable.

Splitting things this way means the LLM-facing schema and wiring lives in
`tools/`, and everything that touches disk, subprocesses, or tree-sitter lives
in `runtime/tools/`, unit-tested without going through the tool decorator at
all (see `tests/test_apply.py`, `tests/test_patch.py`, `tests/test_shell.py`,
etc.).

## Minimal example

```python
# tools/word_count.py
from tools.base import ToolContext, tool
from runtime.tools.fileid import read_source


@tool(description="Count words in a workspace text file.")
def word_count(ctx: ToolContext, path: str) -> str:
    src = read_source(ctx.workspace, path)
    if ctx.files is not None:
        ctx.files.mark(src.rel, src.raw_sha256)
    return str(len(src.text.split()))
```

That's it — restart the engine (or call `discover_tools()` in a test) and
`word_count` shows up in the tool list the LLM sees.

## `@tool` decorator details

```python
def tool(description=None, *, name=None, parameters=None) -> Callable
```

- `name` defaults to the function name — this is what the LLM calls, so name
  it the way you want it to appear in a tool call.
- `description` defaults to the function's docstring, then the function name.
  Keep it short, imperative, and mention any load-bearing constraint (e.g.
  "Read the file first", "no TTY", "1-based"). The LLM only ever sees the
  schema, not your source.
- `parameters` is a JSON-schema `dict`. If omitted, it's derived from the
  function signature via `_schema_from_fn`:
  - `ctx`/`context`/`self` parameters are skipped.
  - `str`/`int`/`float`/`bool` map to their JSON types; anything else (or
    unannotated) becomes `"string"`.
  - `Optional[X]` (i.e. `X | None`) unwraps to `X`'s type.
  - A parameter with no default becomes `required`.

  Auto-derivation is fine for simple internal helpers, but every tool actually
  shipped in this repo (`tools/edit_file.py`, `tools/read_file.py`,
  `tools/search.py`, ...) passes an explicit `parameters` dict with a
  `description` on every property. Do the same for anything the LLM will call
  directly — the per-field description is the only documentation the model
  gets, and matters a lot for correct usage.

## Function signature rules (`Tool.execute`)

- Your function can be sync or async. `Tool.execute` awaits the result if it's
  awaitable.
- If your function declares a `ctx` or `context` parameter, it is called with
  `fn(ctx, **kwargs)`; otherwise just `fn(**kwargs)`. Add `ctx: ToolContext` as
  the first parameter whenever you need the workspace path, file tracker, LSP,
  config, or callbacks.
- `kwargs` passed in are filtered down to your actual parameter names first —
  extra keys the model hallucinates are silently dropped rather than raising
  `TypeError`.
- The return value is coerced to `str` (`None` → `""`). Tools communicate
  failure by returning a string starting with `"error: "`, not by raising —
  see below.

## `ToolContext` fields

```python
@dataclass
class ToolContext:
    workspace: Path
    language: Any = None      # detected project language info
    lsp: Any = None           # LSPManager, or None if unavailable
    files: Any = None         # FileTracker: path -> sha256 of last read/write
    journal: Any = None       # path to the sqlite edit journal
    session_id: str | None = None
    on_edit: Any = None       # callback(rel, diff, tool_name, edit_id)
    config: Any = None        # EngineConfig
    ask_user: Any = None      # async prompt-the-human hook (approvals)
    on_output: Any = None     # streaming output callback (shell)
    on_proc: Any = None       # subprocess-registered callback (shell, for abort)
```

You don't need all of these — only take `ctx` at all if you use something off
it, and only reach for the fields your tool actually needs.

## Error handling: return strings, don't raise

`ToolRegistry.execute` wraps every call in a `try/except Exception` and turns
any exception into `f"error: {exc}"`, so a raw exception "works", but the
convention in this codebase is to **raise a domain error with a message that
already starts with `error:`** inside your `runtime/tools/*` implementation
(e.g. `EditError`, `WorkspacePathError`), or just return an `"error: ..."`
string directly from simple tools. This keeps messages predictable and
testable instead of leaking a raw Python traceback string to the model.

Also note the registry caps every tool result at `MAX_RESULT = 80_000` chars
(truncated with a `"\n...[truncated]"` suffix). You don't need to enforce this
yourself, but if your tool can produce very large output, truncate earlier and
more usefully than a hard byte cut (see `tools/search.py`'s `max_matches`,
`tools/shell.py`'s streaming caps, or `tools/read_file.py`'s line windows).

## If your tool touches the filesystem

Don't hand-roll path handling. Reuse the existing primitives:

- **Reading**: `runtime.tools.fileid.read_source(workspace, path)` — resolves
  the path safely, decodes UTF-8, and detects newline/BOM/indent style. If the
  result may later be edited, call `ctx.files.mark(src.rel, src.raw_sha256)` so
  the read-before-write check (below) is satisfied.
- **Writing/editing**: don't write files directly. Route through
  `runtime.tools.edits.apply_edit(ctx, path, mutate, tool_name, creating=False)`,
  where `mutate(src: FileSource) -> str` returns the *new full text*. This
  single choke point gives you, for free:
  - path containment (`guard_write_path`: no escaping the workspace, no
    symlink escapes, no writing into `.git`/`.engine`/lockfiles/`.env`).
  - the **read-before-write** check: refuses if `ctx.files` doesn't have a
    matching sha256 for the current on-disk content (you must `read_file` — or
    otherwise `mark()` — before editing; and it refuses if the file changed
    since you last read it).
  - `syntax_gate`: rejects the edit if it introduces *new* tree-sitter parse
    errors in the changed region (for `.py`/`.go`/`.js`/`.ts`).
  - preserved newline style / trailing newline / encoding / indent detection.
  - atomic write + an entry in the sqlite edit journal, so `undo_edit` and
    `list_edits` work on your tool's edits automatically.
  - LSP diagnostics are snapshotted before/after and only new ones reported.
  - an `on_edit` callback fire so the UI/journal sees the change.

  Look at `tools/edit_file.py` for the pattern:

  ```python
  async def my_edit_tool(ctx: ToolContext, path: str, ...) -> str:
      def mutate(src: FileSource) -> str:
          return compute_new_text(src.text, ...)

      return await apply_edit(ctx, path, mutate, "my_edit_tool")
  ```

  Multi-file edits (e.g. a rename) should batch through
  `apply_workspace_edit(ctx, edits_by_path, tool_name)` instead, which prepares
  every file first and rolls back everything already committed if any file
  fails partway — never leaves a half-applied edit in the workspace.

  Important invariant if you ever touch this layer directly: the internal
  `_prepare`/`_commit`/`_apply_sync` functions **must never contain an
  `await`** — the whole write pipeline relies on running to completion without
  yielding to the event loop so concurrent tool calls can't interleave and
  corrupt a file. This is called out explicitly in the module docstring; don't
  break it when reusing these helpers.

- **Listing/searching**: reuse `runtime/tools/fs.py` (`list_tree`,
  `SKIP_NAMES`, `resolve_in_workspace`) and `runtime/tools/search.py` rather
  than re-implementing workspace containment or the skip-list (`.git`,
  `.engine`, `node_modules`, venvs, ...).

## If your tool runs a subprocess

Go through `runtime/tools/shell.py`'s helpers rather than `subprocess`
directly, or model your tool on `tools/shell.py` if it's fundamentally a new
kind of process execution:

- Run in its own process group so a timeout can kill the whole tree.
- Respect `ctx.ask_user` for anything not read-only/allow-listed — don't add a
  new way to execute arbitrary commands without an approval path.
- Cap captured output; stream via `ctx.on_output` in chunks instead of
  buffering unbounded output in memory.
- Never raise the timeout above the existing hard cap silently, and keep
  `sudo` / anything touching `.engine` hard-denied regardless of approval mode.

## If your tool needs source structure (tree-sitter) or types (LSP)

- Cheap, no server required: use `runtime/tools/sitter.py` (`list_symbols`,
  `find_symbol`, `get_node_at`, `query_tree`, `parse_file`). Prefer this for
  "what's in this file" questions.
- Needs a running language server: use `ctx.lsp` (`LSPManager`), following
  `tools/lsp.py`'s pattern — **always check `ctx.lsp is not None` first** and
  return a clear "LSP not available" string otherwise (there's a shared
  `_LSP_MISSING` message constant to reuse), and run the blocking client call
  via `asyncio.to_thread`.

## Registering & testing

Nothing to register by hand. To verify discovery works:

```python
from tools.registry import discover_tools

registry = discover_tools()
assert "my_tool" in registry._tools
assert not registry.errors
```

See `tests/test_tools_registry.py` for the existing pattern, and
`tests/conftest.py`'s `ctx` fixture for a ready-made `ToolContext` (backed by
`tmp_path`, a real sqlite journal, and a `FileTracker`) to exercise your tool
against in tests — including the `seed()` helper for writing a file with a
specific newline style before testing an edit tool against it.

## Checklist before adding a tool

- [ ] Real logic lives in `runtime/tools/<name>.py` (or an existing module),
      independent of `ToolContext`/`@tool`, and is unit-testable on its own.
- [ ] `tools/<name>.py` is a thin wrapper: schema + description + call into
      the runtime helper.
- [ ] Explicit `parameters` schema with a `description` on every field, and a
      clear top-level `description` (mention 1-based/0-based, required
      ordering, side effects, size limits — whatever a caller needs to use it
      correctly on the first try).
- [ ] Filesystem access goes through `resolve_in_workspace`/`guard_write_path`
      and, for writes, `apply_edit`/`apply_workspace_edit` — never raw
      `open()`/`Path.write_text()` on a user-supplied path.
- [ ] Failure paths return/raise `"error: ..."`-prefixed messages, not bare
      exceptions or silent empty results.
- [ ] Large/unbounded output is capped/paginated by the tool itself, not left
      for the registry's blanket 80,000-char cutoff to chop mid-content.
- [ ] A test using `discover_tools()` and the `ctx` fixture covers the new
      tool's happy path and at least one failure path.
