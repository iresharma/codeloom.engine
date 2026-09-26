# Packaging the engine as a deployable binary

Right now the engine runs as `python app.py <workspace>` / `python
dummy_client.py <workspace>` inside a venv. This doc investigates turning
that into a single deployable binary, records what was actually found (as
opposed to assumed) about the known trouble spots, and scopes what a full
solution would still require. The companion build script is
`scripts/build_binary.sh`.

## Tool choice: PyInstaller

Three tools were considered: **PyInstaller**, **Nuitka**, and
**shiv/zipapp**. PyInstaller is the one used here.

- The codebase is Python 3.10+, asyncio/stdlib-heavy, `AF_UNIX`-socket
  based, with no numeric or heavy-C stack (no numpy/scipy/torch-style
  extension modules to worry about beyond tree-sitter's small grammar
  bindings).
- PyInstaller has a mature hook ecosystem. `pyinstaller-hooks-contrib`
  already ships `hook-rich.py`, and there are well-documented community
  patterns for `textual` using `collect_all`. Between the two, the TUI-path
  dependencies (used by `client_tui.py`, out of scope for this PR but worth
  noting for the future) are already covered by prior art.
- It needs no C compiler on the build machine. Nuitka compiles Python to C
  and links the result, which means every build machine (and potentially
  every target platform) needs a working C toolchain. That's a heavier,
  more fragile requirement than this project needs to take on for a first
  packaging pass.
- shiv/zipapp were disqualified as a true single-binary solution. CPython's
  own zipapp docs (Caveats section) state plainly: "If your application
  depends on a package that includes a C extension, that package cannot be
  run from a zip file." tree-sitter's grammar packages
  (`tree_sitter_python`, `tree_sitter_go`, etc.) are exactly this — a
  compiled `_binding` C extension per package. shiv's actual answer to this
  is to self-extract site-packages to `~/.shiv/<hash>` on first run and
  import from real disk, which is a materially weaker guarantee than a
  PyInstaller onedir/onefile build, and it requires a writable `$HOME` on
  the target machine (not guaranteed in every deploy environment).

## What was built

`scripts/build_binary.sh` runs two PyInstaller builds, `--onedir` each:

- `app.py` → `dist/app/app` (the server)
- `dummy_client.py` → `dist/dummy_client/dummy_client` (client, **headless
  path only** — i.e. `--message "..." --auto`, which lazily imports
  `headless_client.run_once`)

**Onedir, not onefile.** Onefile self-extracts to a temp directory on every
launch, which is slower to start and, more importantly, hides the exact
failure this project is most at risk of (see "the pkgutil risk" below):
a missing dynamically-discovered module shows up as an ImportError with no
directory to go inspect. Onedir still ships as a single artifact directory
— `tar czf app.tar.gz dist/app/` — but leaves a real `_internal/` tree you
can `ls` and diff against expectations.

The TUI path (`client_tui.py`, using `textual` + `rich`) is intentionally
**not** built here, to keep this PR's scope small, per the requester's own
framing of "you do not need to solve full packaging in one PR."

## The pkgutil / discover_tools risk (bigger than tree-sitter)

`tools/registry.py::discover_tools()`:

```python
for info in pkgutil.walk_packages(tools_pkg.__path__, prefix):
    ...
    module = importlib.import_module(info.name)
```

This is called from `runtime/session.py` at `EngineSession` construction —
i.e. on every server startup — and it dynamically imports **every module**
under the `tools` package (`tools/sitter.py`, `tools/browser.py`,
`tools/lsp.py`, `tools/http.py`, `tools/github.py`, ...). Nothing in the
codebase does `import tools.sitter` as a literal, top-level statement
anywhere PyInstaller's static import-graph analysis would see it walking
from `app.py`. Left alone, this means PyInstaller will simply not bundle
those modules (or their transitive dependencies, like the tree-sitter
grammar packages `tools/sitter.py` re-exports through
`runtime/tools/sitter.py`), and the frozen server will throw `ImportError`
the first time `discover_tools()` tries to import a tool module that never
made it into the bundle.

This is a bigger practical risk than the tree-sitter question below,
because it silently affects **every** module under `tools/` and
`runtime/tools/`, not just the four named trouble spots. The build script
works around it with `--collect-submodules=tools` and
`--collect-submodules=runtime.tools`, which force PyInstaller to include
every submodule of both packages regardless of whether a static import
graph would ever find it.

## The four named trouble spots

### a. Tree-sitter grammar packages

Each of `tree_sitter_python`, `tree_sitter_javascript`,
`tree_sitter_typescript`, `tree_sitter_go` ships a compiled `_binding`
extension module (ABI3-tagged, so one wheel covers a range of CPython
minor versions) plus `.scm` tree-sitter query files as `package_data`.

`runtime/tools/sitter.py` only uses `Language`, `Parser`, `Query`,
`QueryCursor`, and each package's `language()` (or `language_typescript()`
/ `language_tsx()`) accessor — it does not load any of the `.scm` query
files that ship as package data. That means the "does PyInstaller bundle
non-Python package data" question, which is a real gap for some packages,
is **not a blocker here**: the only thing that has to land in the frozen
tree is each package's compiled `_binding*.so`.

For a plain static `import tree_sitter_go as tsgo`, PyInstaller's binary
dependency walker is supposed to pick up compiled extension modules
automatically. However, there is currently no `pyinstaller-hooks-contrib`
hook for `tree_sitter`, `tree_sitter_python`, `tree_sitter_javascript`,
`tree_sitter_typescript`, or `tree_sitter_go` (checked the hooks-contrib
hook list; absent as of writing). The build script therefore adds explicit
`--collect-all` flags for all five packages as a safety net that also
captures any data files, in case the plain import path misses something.

**This was not empirically verified in this environment** — no PyInstaller
build was actually run (no confirmed working PyInstaller install / no
attempt made, per the task's own instruction not to spend effort chasing a
real build). Required follow-up: run `scripts/build_binary.sh` for real,
then inspect `dist/app/_internal/` (PyInstaller onedir's internal-files
directory) to confirm `tree_sitter_python/_binding*.so`,
`tree_sitter_go/_binding*.so`, etc. actually landed. If they didn't, add a
custom hook file `hooks/hook-tree_sitter_python.py` (and one per grammar
package) using `collect_dynamic_libs` + `collect_data_files`, the same
pattern hooks-contrib's `hook-rich.py` uses, and point PyInstaller at the
`hooks/` directory with `--additional-hooks-dir`.

### b. npx-spawned language servers (pyright, typescript-language-server)

`runtime/tools/lsp.py` spawns these as subprocesses:

```python
cmd=["npx", "-y", "-p", "pyright", "pyright-langserver", "--stdio"]
cmd=["npx", "-y", "typescript-language-server", "--stdio"]
```

These are external Node.js tools resolved via `PATH` at subprocess-spawn
time. Packaging `app.py` into a PyInstaller binary changes nothing about
how this works, because it's a `subprocess`/PATH lookup, not a Python
import — bundling the calling process does not bundle the thing it shells
out to. Plainly stated: **the target machine still needs Node.js and
npm/npx on `PATH`**, and `npx -y` still performs its normal first-run
fetch-and-cache of the package, which needs network access unless the npm
cache has been pre-warmed on that machine.

### c. gopls

Also in `runtime/tools/lsp.py`:

```python
cmd=["gopls", "serve"]
```

No `npx` involved — `gopls` is expected directly on `PATH`. Same
conclusion as (b): this is an external Go-toolchain binary, entirely
unaffected by how the Python process that spawns it was packaged. The
target machine needs Go's `gopls` installed and on `PATH`.

### d. Playwright browser binaries

`runtime/tools/browser.py` imports `playwright.async_api` lazily and falls
back to a fixed error string (`_MISSING = "error: browser tools
unavailable (install playwright and run playwright install chromium)"`) if
the import fails. `playwright` is not in `requirements.txt` — it is an
optional dependency today.

If it were a real declared dependency, the `playwright` *Python* package
(its API surface and small native shims) could be bundled like any other
Python package, no differently from tree-sitter's bindings. The part that
genuinely does not survive bundling is the actual browser engine binaries
— Chromium/Firefox/WebKit, hundreds of MB each — that `playwright install
chromium` downloads into a cache directory that lives **outside** the
Python package tree entirely: `~/.cache/ms-playwright` on Linux,
`~/Library/Caches/ms-playwright` on macOS. No bundler's dependency walk
touches that directory, because from the bundler's point of view it isn't
a Python import at all; it's a side-effect of a separate CLI command
(`playwright install`) that downloads platform-specific browser archives
over the network at a time of your choosing, not at import time.

A full solution needs one of:
- a documented post-deploy step (`playwright install chromium` run once on
  the target machine after unpacking the binary), or
- shipping the `~/.cache/ms-playwright` (or equivalent) directory as a
  separate asset alongside the binary, with `PLAYWRIGHT_BROWSERS_PATH` set
  in the environment to point at wherever it was unpacked.

Neither is attempted here; this doc states the gap rather than solving it,
per the intentionally-scoped-down PR.

## Full solution would require (scoped follow-up plan)

This PR is a starting point, not a finished packaging story. A full
solution would still need:

1. **An actual PyInstaller build run**, followed by manual verification
   that `tree_sitter_python/_binding*.so` (and the equivalent for the other
   four grammar packages) actually landed in `dist/app/_internal/`. If they
   didn't, add custom hooks under `hooks/hook-tree_sitter_<lang>.py` using
   `collect_dynamic_libs` + `collect_data_files` (same shape as
   hooks-contrib's `hook-rich.py`) and pass
   `--additional-hooks-dir=hooks` to PyInstaller.
2. **A decision on whether to bundle the TUI path**
   (`client_tui.py` + `textual` + `rich`), and if so, the hooks/collect
   flags that go with it (`--collect-all textual`, and either relying on
   hooks-contrib's existing `hook-rich.py` or adding an explicit
   `--collect-all rich` if that hook proves insufficient in practice).
3. **A documented target-machine prerequisites list**, since none of the
   external tools below can or should be bundled into the Python binary:
   - Node.js + npm/npx on `PATH`, with network access for `npx -y`'s
     first-run fetch (or a pre-warmed npm cache)
   - Go + `gopls` on `PATH`
   - optionally, a pre-provisioned Playwright browser cache directory plus
     `PLAYWRIGHT_BROWSERS_PATH` pointed at it, if browser tools are needed
4. **CI matrix consideration** if binaries are needed for more than one
   platform. This repo is already Unix-only (`app.py` uses `AF_UNIX`
   sockets), so at minimum macOS and Linux builds are needed; no Windows
   build is required or meaningful here.
5. **Resolving whether `rich` needs explicit pinning** in
   `requirements.txt` before packaging the TUI path. `client_tui.py`
   imports `rich` directly (`from rich.console import Group`, etc.), but
   `rich` is not listed in `requirements.txt` — it is presumably pulled in
   transitively by `textual`. PyInstaller needs `rich` importable in the
   build environment regardless of how it got there, so before packaging
   the TUI path this should be pinned explicitly rather than relying on
   textual's transitive dependency resolution to keep providing it.
