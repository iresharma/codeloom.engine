# Language support

## Detection

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

## Tree-sitter

Grammars for Python, Go, JavaScript, TypeScript, and TSX load lazily from the
`tree-sitter-*` packages in `requirements.txt`. Tree-sitter powers the outline
tools, the symbol-scoped edits, and the syntax gate. It is fast enough to run
on every write and tolerant of broken files, which is exactly what a syntax
gate needs.

## Language servers

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
