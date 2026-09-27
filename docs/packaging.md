# Packaging prototype

Status: prototype / investigation. `scripts/build_binary.sh` builds
standalone `--onedir` bundles for the pure-Python core (`app.py` server,
`dummy_client.py` headless client) using PyInstaller. This document records
why PyInstaller/onedir was chosen, what does not survive naive bundling,
and what a full solution would require.

## Chosen tool: PyInstaller, `--onedir`

**PyInstaller vs Nuitka vs shiv/zipapp:**

- **PyInstaller** was chosen because it has the broadest hook coverage for
  this dependency stack (textual, mcp, openrouter client, native
  tree-sitter wheels) via `pyinstaller-hooks-contrib`, and it's the most
  battle-tested option for freezing CPython apps with native-extension
  dependencies into a binary that needs no system Python.
- **Nuitka** was considered but rejected for this stack: it has weaker hook
  coverage for the packages here (tree-sitter grammars, textual, mcp) —
  we'd end up hand-rolling more compilation/inclusion configuration than
  with PyInstaller, for no clear runtime benefit for a CLI/server tool like
  this.
- **shiv/zipapp** were rejected because they still require a system Python
  interpreter to be present on the target machine (they package Python
  source into a zipapp, not a standalone executable), and they cannot
  freeze native extensions like the tree-sitter grammar `.so`/`.pyd` files
  into a self-contained artifact. That fails the "truly standalone binary"
  goal.

**`--onedir` vs `--onefile`:** `--onedir` was chosen over `--onefile`
because `--onefile` extracts itself to a temp directory on every launch,
adding startup latency and complicating debugging (stack traces/file paths
point into a throwaway extraction dir). `--onedir` keeps a stable directory
of files on disk that's easier to inspect and iterate on during this
prototype phase.

## What does NOT survive naive bundling

### tree-sitter grammars

`tree-sitter-python`, `tree-sitter-javascript`, `tree-sitter-typescript`,
and `tree-sitter-go` (imported lazily inside `runtime/tools/sitter.py`,
around lines 145-153/165/178) are native-extension wheels. We confirmed by
searching PyInstaller's built-in hooks and `pyinstaller-hooks-contrib` that
**no hook exists** for `tree_sitter` or the per-language grammar packages —
this was checked, not assumed. Real-world projects either hand-roll a hook
using `collect_dynamic_libs(lang_module)` + `copy_metadata()` per language
module, or brute-force it with `--collect-all`/`--collect-submodules` flags
on the CLI. `scripts/build_binary.sh` uses the brute-force flags
(`--collect-all tree_sitter_python --collect-all tree_sitter_javascript
--collect-all tree_sitter_typescript --collect-all tree_sitter_go
--collect-submodules tree_sitter`) as the pragmatic prototype fix. This
works but is coarser than a real hook (pulls in more than strictly needed).

### npx-spawned language servers (pyright, typescript-language-server)

`runtime/tools/lsp.py` spawns `["npx", "-y", "-p", "pyright",
"pyright-langserver", "--stdio"]` (~line 184) and `["npx", "-y",
"typescript-language-server", "--stdio"]` (~lines 199/205). These are
Node.js/npm packages invoked at runtime via `npx`. PyInstaller freezes
Python imports and native libs only — it cannot embed a Node.js runtime or
npm registry packages. Even the pip-installable `pyright` PyPI wrapper is
itself just a shim that locates/downloads Node and runs `npm install` for
the real `pyright` npm package at runtime; it is not freezable.

**Verdict:** Node.js + npm/npx must remain an external system dependency
that the deployment target installs separately. This is already true
today; this PR does not change it. Explicit non-goal.

### gopls

`runtime/tools/lsp.py` spawns `["gopls", "serve"]` (~line 211). `gopls` is
a statically-linked Go binary with no official standalone binary release
channel — the only documented install path is `go install
golang.org/x/tools/gopls@latest`, which requires a Go toolchain on the
target machine. PyInstaller cannot produce or absorb a Go binary.

**Verdict:** `gopls` must remain an external dependency, already true
today (`tests/test_lsp_write.py` already skips when `gopls` isn't on
`PATH`). Explicit non-goal.

### Playwright browser binaries

`runtime/tools/browser.py` lazily/optionally imports `playwright`; it's
absent from `requirements.txt` today and degrades to a `_MISSING` error
string when not installed. Browser binaries (chromium/firefox/webkit) are
downloaded separately via `playwright install` into an OS cache dir
(e.g. `~/.cache/ms-playwright`) — they are **not** part of the `playwright`
pip wheel. Playwright ships its own official PyInstaller hook inside the
package itself
(`playwright/_impl/__pyinstaller/hook-playwright.*_api.py`, auto-discovered
by PyInstaller), which correctly collects the Python driver-loader data
files — but that hook only grabs the Python package data, not the browser
binaries.

**Verdict:** if/when Playwright becomes a real dependency, the PyInstaller
hook "just works" for the Python side, but `playwright install` (and
optionally `playwright install-deps`) must still be run as a separate
post-install step on the target machine — browser binaries are never
bundled by PyInstaller.

## What a full solution would require (scoped follow-up)

This prototype intentionally does not solve the items above in code. A
full solution would need:

1. **tree-sitter**: replace the coarse `--collect-all` flags with a real
   hand-rolled PyInstaller hook (`hook-tree_sitter_python.py` etc.) using
   `collect_dynamic_libs()` + `copy_metadata()` per language module, to
   shrink the bundle and make the dependency explicit/reviewable.
2. **npx/pyright/typescript-language-server**: vendor a portable Node.js
   runtime plus a pre-installed `node_modules` directory as a sibling asset
   directory next to the frozen binary, and rewrite `runtime/tools/lsp.py`'s
   `npx` invocations to point at the vendored Node binary/`node_modules`
   instead of relying on a system-installed `npx`.
3. **gopls**: cross-compile/vendor prebuilt `gopls` binaries per
   OS/architecture as sibling assets (since there's no official binary
   release channel, this means running `go install` per target platform as
   part of the release pipeline and shipping the resulting binaries).
4. **Playwright**: either document `playwright install` (and
   `install-deps`) as a required post-install step in deployment docs, or
   vendor pre-fetched browser directories as sibling assets and point
   Playwright's `PLAYWRIGHT_BROWSERS_PATH` env var at them.

Each of the above is its own scoped change; none is attempted here.
