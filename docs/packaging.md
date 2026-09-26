# Packaging investigation: standalone binaries (prototype)

This is a packaging **investigation**, not a production release pipeline.
It documents a working approach and its known gaps so a follow-up PR can
turn it into something CI-driven and cross-platform.

## Tool choice: PyInstaller, onedir

Three candidates were evaluated: **PyInstaller**, **Nuitka**, and
**shiv/zipapp**. This investigation's conclusion is PyInstaller in
**onedir** mode:

- No C toolchain required at build time. Nuitka compiles to C and needs a
  real compiler on the build host; it also bundles an AGPLv3-licensed
  compiler component as part of its build step, which is a real policy
  consideration even though the license exception generally covers the
  *produced* binary, not the toolchain.
- Largest and most mature hook ecosystem of the three, which matters here
  specifically for compiled-extension wheels like the tree-sitter grammar
  packages — PyInstaller's hooks are far more likely to already know how
  to handle an edge case than either alternative's.
- Unlike shiv/zipapp, PyInstaller actually embeds a Python interpreter in
  the output. shiv's own docs are explicit that it still requires a
  matching system Python on `PATH` at run time — that defeats the goal of
  a single deployable binary with no host Python prerequisite.
- PyInstaller is not a cross-compiler, but neither are the other two: a
  Linux artifact must be built on Linux, a macOS artifact on macOS, no
  matter which of the three tools is used.

**onedir over onefile**, specifically:

- Faster repeated startup. This project's entry points are a long-running
  server (`app.py`) and a client invoked repeatedly (`headless_client.py`),
  not a run-once script, so avoiding onefile's per-launch self-extraction
  cost matters.
- onefile self-extracts to a temp directory on every launch; on hosts
  where `/tmp` is mounted `noexec` this fails outright. onedir has no such
  failure mode.
- onedir output is much easier to debug and inspect during rollout — you
  can literally see which `.so`/data files got collected in the output
  folder, rather than having to unpack a onefile archive to check.

## Build script

`scripts/build_binary.sh` builds two onedir bundles:

- `dist/agent-engine-server/` — wraps `app.py` (the JSON-IPC engine
  server, listening on `<workspace>/.engine/engine.sock`).
- `dist/agent-engine-client/` — wraps `headless_client.py` (the scripted
  one-shot client used by `dummy_client.py --auto`). The interactive
  Textual TUI mode of `dummy_client.py` is out of scope for this build.

It runs two separate PyInstaller invocations (one per entry point) rather
than a single combined spec, since the server and client have different
runtime footprints and this keeps each bundle smaller and the script
easier to read. It installs PyInstaller via pip if missing, and exits
non-zero with a clear message if that install fails or PyInstaller still
isn't runnable afterward.

For the tree-sitter grammar packages and several dependencies that do
enough dynamic/plugin-style importing to risk a silent missed submodule
(`openrouter`, `typesafe-sdk`, `textual`, `prometheus_client`), the
script passes `--collect-all` rather than relying on PyInstaller's default
import-graph analysis alone. `mcp` gets the same "enumerate everything"
treatment but through a custom hook rather than `--collect-all` — see
below.

### The `mcp.cli` / `typer` problem, and its fix

PyInstaller (6.22.3) was installed via pip successfully (PyPI was
reachable) and the script was first run against `app.py` with a blanket
`--collect-all mcp`. It did **not** complete: `--collect-all mcp` makes
PyInstaller call `collect_submodules('mcp')`, which imports every
submodule of the `mcp` package to enumerate hidden imports — including
`mcp.cli.cli`. That module does `import typer` at module scope and calls
`sys.exit(1)` if it's missing (per its own error message, `typer` is an
optional extra behind `pip install "mcp[cli]"`, not part of the
project's actual runtime dependency, and it isn't in `requirements.txt`).
`typer` was not installed in this sandbox, so the build aborted before
producing a bundle. The engine never imports `mcp.cli` at runtime, so
this was the wrong scope to begin with, not a missing dependency to add.

The obvious-looking fix — pairing `--collect-submodules mcp` (or
`--collect-all mcp`) with `--exclude-module mcp.cli` — was tried and
**does not work**: `--exclude-module` only takes effect inside PyInstaller's
`Analysis` step, which runs *after* the CLI-flag-generated spec file's
top-level `collect_submodules('mcp')` call has already executed and
already raised (`sys.exit(1)` inside `mcp/cli/__init__.py`'s `from .cli
import app`, which is a `SystemExit` — not caught by
`collect_submodules`'s own error handling, which only catches
`Exception`). This was reproduced directly in this sandbox: the build
still aborted with the same `typer` traceback even with `--exclude-module
mcp.cli` added.

The actual fix: `collect_submodules()` takes a `filter=` callable that
can skip `mcp.cli` and everything under it *before* it is ever imported
— but that parameter isn't exposed by the `--collect-submodules` /
`--collect-all` CLI flags. `scripts/pyinstaller_hooks/hook-mcp.py` is a
small custom PyInstaller hook that calls `collect_submodules('mcp',
filter=...)` directly with that filter, and `scripts/build_binary.sh`
wires it in via `--additional-hooks-dir scripts/pyinstaller_hooks`
instead of `--collect-all mcp`. With this hook in place, the build was
re-run in this sandbox against both entry points and **completed
successfully**:

- `dist/agent-engine-server/` (wrapping `app.py`) was produced, and
  running `./dist/agent-engine-server/agent-engine-server --help` printed
  the expected argparse usage text and exited 0.
- `dist/agent-engine-client/` (wrapping `headless_client.py`) was
  produced, and running `./dist/agent-engine-client/agent-engine-client
  --help` exited 0 (silently, matching the unpackaged `python3
  headless_client.py --help` behavior — not a packaging regression).

Both bundles were ~67MB onedir trees under `dist/`, dominated by the
bundled CPython interpreter plus `textual`/`prometheus_client`/tree-sitter
payloads. `dist/`, `build/`, and the generated `.spec` files were deleted
after this verification run to keep the worktree clean, per the same
policy documented above (re-runnable, not checked in).

The four tree-sitter grammar packages were reached and collected without
incident once past the `mcp` fix, which is at least consistent with
(though not a rigorous proof of) the claim below that PyInstaller's
default compiled-extension handling and/or `--collect-all` correctly
picks them up.

## What does not survive naive bundling

### tree-sitter grammar packages

`tree_sitter_python`, `tree_sitter_javascript`, `tree_sitter_typescript`,
and `tree_sitter_go` ship as prebuilt, compiled wheels — there is no
runtime download or compile step (see `runtime/tools/sitter.py`, which
loads them via e.g. `Language(tspython.language())`). PyInstaller's
import-graph analysis generally picks up compiled-extension wheels like
any other imported package and copies the shared library alongside the
bundle. Whether that "just works" here with zero extra flags, or needs
`--collect-all`/a custom hook because the actual `.so`/`.dylib` load path
is resolved dynamically rather than via a normal Python import, was
**not build-verified** in this sandbox (the build did not get that far —
see above). `--collect-all` is included for all four in
`scripts/build_binary.sh` as the safer default given that uncertainty.

### npx-spawned language servers (pyright, typescript-language-server)

`runtime/tools/lsp.py`'s `LSPManager.SERVER_CONFIGS` spawns
`["npx", "-y", "-p", "pyright", "pyright-langserver", "--stdio"]` and
`["npx", "-y", "typescript-language-server", "--stdio"]` via plain
`subprocess.Popen`, looking up `npx`/Node on `PATH` at run time.
PyInstaller only bundles what is imported in Python — it has no visibility
into strings passed to `subprocess.Popen`. The packaged binary therefore
still requires Node.js (with `npx`) installed on the host, exactly as
today unpackaged. `npx -y` may also do a network fetch on first use if the
target package isn't already cached locally — packaging does not change
that either.

### gopls

Same story: `runtime/tools/lsp.py` spawns `["gopls", "serve"]` via
`subprocess.Popen`, looked up on `PATH`. This is launched as an external
process, not imported, so PyInstaller cannot and does not bundle it.
`gopls` must be pre-installed on the host.

### Playwright's browser binaries

`runtime/tools/browser.py` soft-imports
`from playwright.async_api import async_playwright`; the Python package
itself can be bundled like any other dependency. But Playwright's actual
browser binaries (Chromium etc.) are large, downloaded separately via
`playwright install chromium`, and are not part of the pip package at
all — PyInstaller's import-graph analysis never sees them. This is
already a documented soft-fail today, unpackaged: if Playwright isn't
installed, every browser tool returns
`"error: browser tools unavailable (install playwright and run playwright
install chromium)"`. Packaging does not remove the need for that
separate post-install step; it remains an explicit host prerequisite (or,
for a "full" variant, a build-time step — see below).

### ripgrep (`rg`)

`runtime/tools/search.py` looks up `rg` via `shutil.which("rg")` and
raises `RuntimeError("rg not found; install ripgrep")` if it's missing.
Same category as the above: an external binary on `PATH`, not a Python
import, so it is not bundled and must be pre-installed on the host.

## What a full solution would require (explicit follow-up, not solved here)

- A CI matrix building on `ubuntu-latest` and `macos-latest` per release
  (PyInstaller cannot cross-compile), publishing one artifact per OS/arch.
- The `mcp` collection issue found above is fixed (see
  `scripts/pyinstaller_hooks/hook-mcp.py`); the build now completes for
  both `app.py` and `headless_client.py` in this sandbox. What remains
  before relying on this in CI is exercising it on Linux (only macOS
  arm64 was verified here) and on a cleaner venv than this dev sandbox's.
- If LSP support must work with zero host prerequisites: vendoring a
  pinned Node.js runtime plus pre-fetched `npx` packages (pyright,
  typescript-language-server) per OS/arch, and vendoring `gopls` binaries
  per OS/arch — both nontrivial, version-pinned, and doubling the matrix
  surface.
- A documented `playwright install chromium` post-install step, or a
  separate optional "full" build variant that runs that install during
  the build and bundles the resulting browser cache directory alongside
  the app.
- Re-running (and likely re-verifying `--collect-all` needs for) the build
  whenever the pinned Python floor or the tree-sitter grammar package
  versions change, since these are native-ABI bindings.

None of the above is implemented in this PR. This document and
`scripts/build_binary.sh` are the investigation's output: a re-runnable
starting point plus a written record of what does and does not survive
naive bundling, and why.
