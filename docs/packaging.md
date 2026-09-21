# Packaging the engine as a single deployable binary (prototype)

Status: prototype / proof-of-concept for Part B of the packaging investigation.
It packages the core Python code path with PyInstaller and documents, but does
not yet solve, four native-process trouble spots. Treat everything below the
"Trouble spots" heading as a scoped follow-up list, not a completed feature.

## Why PyInstaller

1. **Produces a genuinely Python-free binary.** The target machine needs no
   Python interpreter installed at all — PyInstaller embeds a full Python
   runtime plus the app's bytecode into the bundle. This is different from
   `shiv`/`zipapp`, which still require a compatible Python interpreter to
   already exist on the target machine; they only solve dependency bundling,
   not interpreter bundling.
2. **No C compiler needed on the build machine.** Nuitka needs gcc/clang/MSVC
   present at build time (it transpiles to C and compiles it) and is licensed
   AGPLv3, which is a much stronger copyleft than PyInstaller's GPLv2-with-a
   linking-exception. PyInstaller's exception explicitly permits distributing
   non-free/commercial applications built with it; Nuitka's AGPL does not give
   an equivalent blanket allowance.
3. **Largest hook ecosystem.** `pyinstaller-hooks-contrib` ships hundreds of
   hooks for exactly the class of import-hiding, dynamically-loaded, and
   native-extension packages this repo depends on (see `tree-sitter-*` below),
   plus the largest body of community precedent for debugging this class of
   problem.
4. **Documented subprocess-sanitization pattern.** PyInstaller's own docs
   describe restoring `LD_LIBRARY_PATH` (from the bootloader-set
   `LD_LIBRARY_PATH_ORIG`) / calling `SetDllDirectoryW(None)` before spawning
   external programs from a frozen app. This repo will need exactly that
   pattern for the npx/node/gopls/playwright subprocesses it spawns (see
   below), so building on the tool that already documents the fix is a
   material advantage over the alternatives.

Per-OS build: PyInstaller is not a cross-compiler. It must be run on the same
OS and CPU architecture as the deployment target (build on macOS for macOS,
Linux for Linux, etc). A cross-platform release means running the build on
one machine of each target OS/arch, e.g. in per-OS CI runners.

Mode: this prototype uses one-dir mode (`pyinstaller app.py`, i.e. no
`--onefile`). One-dir produces a folder of the interpreter, bundled
dependencies, and the launcher executable; startup is fast because nothing is
re-extracted per run. `--onefile` packs everything into a single compressed
executable that self-extracts to a temp directory on every launch, which is
slower to start every single time it runs. `--onefile` is deferred to a later
pass once the one-dir build's coverage and trouble spots below are settled.

## What this prototype covers

`scripts/build_binary.sh` runs two independent `pyinstaller` invocations
against the repo's two entry points:

- `pyinstaller --name engine-server app.py` → `dist/engine-server/engine-server`
- `pyinstaller --name engine-client dummy_client.py` → `dist/engine-client/engine-client`

Both were built and smoke-tested in this environment (macOS arm64, Python
3.14, PyInstaller 6.22.3):

- `dist/engine-server/engine-server /path/to/workspace` starts, creates
  `.engine/`, prints the same startup banner as `python app.py` would
  (workspace, engine dir, db path, socket path, Python version, platform),
  and successfully binds and listens on the Unix domain socket at
  `.engine/engine.sock`. No missing-hidden-import crashes were observed for
  `runtime/server.py` + `runtime/session.py` and their (eagerly-imported)
  dependency graph — this exercises the `openrouter`, `mcp`, `typesafe-sdk`,
  `prometheus_client` imports that load at process startup.
- `dist/engine-client/engine-client --help` runs and prints the full
  argparse help text (workspace positional, `--message`, `--auto`,
  `--timeout`, `--settle`), confirming `dummy_client.py`'s own imports
  (`protocol/*`, `runtime/tools/git.py`, and the `textual`-based TUI module it
  imports transitively) resolve inside the frozen bundle.

This only proves the core control-plane code path (accepting connections /
CLI parsing) is packageable. It does **not** exercise an actual end-to-end
session (LLM calls, tool execution, LSP, browser) — those paths lazily import
tree-sitter, spawn `npx`/`gopls`/Playwright subprocesses, etc., and are the
subject of the trouble spots below, which were investigated but intentionally
not fixed in this pass.

Re-running `scripts/build_binary.sh` is safe: it reinstalls
`requirements.txt` + `pyinstaller` (idempotent via pip), removes the previous
`build/engine-server`, `build/engine-client`, and any stray `*.spec` files,
then re-invokes PyInstaller with `--noconfirm` so it overwrites `dist/`
without prompting.

## Trouble spots (investigated, not fixed — scoped follow-up)

### 1. tree-sitter grammars (`runtime/tools/sitter.py`)

`_languages()` (sitter.py:145-162) lazily imports `tree_sitter_go`,
`tree_sitter_javascript`, `tree_sitter_python`, `tree_sitter_typescript`, and
`tree_sitter.Language`, and builds five cached `Language` objects (python,
javascript, typescript, typescript_jsx, go — the last two both come from
`tree_sitter_typescript`). `parse_bytes()` (sitter.py:178-185) separately and
lazily imports `tree_sitter.Parser`.

Each `tree-sitter-<lang>` package (`tree-sitter-python`,
`tree-sitter-javascript`, `tree-sitter-typescript`, `tree-sitter-go` — all
listed unpinned in `requirements.txt`) ships a compiled native extension
(the actual parser `.so`/`.pyd`/`.dylib`), not pure Python. PyInstaller's
static import scanner walks Python `import` statements and package
`__init__.py` contents to find modules to bundle; it does not reliably
discover binary grammar files that a package loads via its own C-extension
loading path or ships as package data rather than importable submodules.
Because these imports are also *lazy* (inside a function, not at module load
time), PyInstaller's static analysis may not even see the `import
tree_sitter_go` etc. lines get executed unless it can trace the call graph —
in practice PyInstaller does handle function-local imports it can statically
see in the source, but whether the grammar binary itself gets copied into the
bundle's binary payload is the real open question.

Why it doesn't survive naive bundling: if the compiled grammar file isn't
copied into `dist/engine-server/`, the frozen binary will fail at first use
(first real tree-sitter tool call) with an import or "language not found"
error, even though the build itself succeeds — a failure mode this
prototype's smoke test would not catch since it never touches `sitter.py`.

Full solution (follow-up, not done here): explicitly verify with
`pyinstaller --collect-all tree_sitter_python --collect-all
tree_sitter_javascript --collect-all tree_sitter_typescript --collect-all
tree_sitter_go app.py` (or equivalent `datas=`/`binaries=` entries in a
`.spec` file) that each grammar's compiled binary lands in the one-dir
output, then add a smoke test that actually calls `sitter.parse_bytes()` for
each of the 5 language keys against the frozen binary before declaring this
solved. Do not assume `--collect-all` is sufficient without that empirical
check — the whole point of flagging this is that PyInstaller's coverage of
compiled-extension data files varies by package layout.

### 2. npx-spawned language servers: pyright / typescript-language-server (`runtime/tools/lsp.py`)

`LSPManager.SERVER_CONFIGS` (lsp.py:181-215) defines, for `python`:
`cmd=["npx", "-y", "-p", "pyright", "pyright-langserver", "--stdio"]`, and for
`typescript`/`javascript` (sharing one process):
`cmd=["npx", "-y", "typescript-language-server", "--stdio"]`. These are
spawned by `LSPClient.__init__` (lsp.py:32-40) via
`subprocess.Popen(cmd, cwd=cwd, stdin=PIPE, stdout=PIPE, stderr=PIPE)`.

Why it doesn't survive naive bundling: PyInstaller bundles this repo's
*Python* code and its Python dependency graph. It has no visibility into,
and does nothing to, Node.js, npm, or npx — those must already be installed
and on `PATH` on the machine that runs the frozen `engine-server` binary,
identical to the unfrozen `python app.py` story today. Freezing app.py does
not change this requirement at all; it's not "solved" or "broken" by
packaging, it's simply orthogonal and needs to be documented as a
prerequisite either way.

The actual new risk introduced by freezing: PyInstaller's bootloader sets
`LD_LIBRARY_PATH` (Linux) / uses `SetDllDirectoryW` (Windows) so the frozen
app's own bundled C-extensions (e.g. the tree-sitter native libs above) can
find their shared libraries at runtime. That environment is inherited by
every child process the frozen app spawns via `subprocess.Popen`, including
`npx`/`node`. If there's any ABI mismatch between what the bundled
`LD_LIBRARY_PATH` points at and what `node`'s own dynamic linker expects,
`npx`/`node` can fail to start or misbehave in a way that's specific to
running inside the frozen binary and does not repro when running
`python app.py` directly.

Full solution (follow-up, not done here): (a) document Node.js + npx
presence on the target machine's `PATH` as an explicit prerequisite in
deployment docs (same as today, just written down); (b) before each
`subprocess.Popen(cmd, ...)` call that launches an external program in
`lsp.py`, sanitize the environment passed to the child — restore
`LD_LIBRARY_PATH` from `LD_LIBRARY_PATH_ORIG` (the bootloader preserves the
original value under that name on Linux) or omit `SetDllDirectoryW`'s effect
on Windows — following PyInstaller's own documented pattern for this exact
problem, then add the resulting `env=` to `subprocess.Popen` in
`LSPClient.__init__`.

### 3. gopls (`runtime/tools/lsp.py`)

The `go` entry in `SERVER_CONFIGS` (lsp.py:209-214) is
`cmd=["gopls", "serve"]`, spawned through the same `LSPClient`/
`subprocess.Popen` mechanism as pyright/typescript-language-server. Unlike
npx-based tools, `gopls` has no auto-fetch step — it must already be
installed and on `PATH`. The existing test suite already encodes this
expectation: `tests/test_lsp_write.py:40` uses
`pytest.mark.skipif(shutil.which("gopls") is None, reason="gopls is not
installed")` to skip Go-LSP tests when the binary isn't present.

Why it doesn't survive naive bundling: identical story to npx tools —
freezing the Python code doesn't touch this at all. `gopls` is not bundled by
PyInstaller and was never going to be; it's a Go binary with no relationship
to the Python packaging step.

Full solution (follow-up, not done here): document `gopls` on `PATH` as a
target-machine prerequisite exactly like today (no packaging change needed),
and apply the same `LD_LIBRARY_PATH`/environment-sanitization treatment as
item 2 to the `subprocess.Popen(["gopls", "serve"], ...)` call site, since it
goes through the same `LSPClient.__init__` code path and is subject to the
identical inherited-environment risk.

### 4. Playwright + Chromium (`runtime/tools/browser.py`)

Playwright is a soft/optional dependency, not listed in `requirements.txt` at
all. `_ensure()` (browser.py:15-39) does
`try: from playwright.async_api import async_playwright; except ImportError:
return None`; every public function (`browser_open`, `browser_console`,
`browser_screenshot`, `browser_network`) checks for a `None` page/import and
returns the literal string `"error: browser tools unavailable (install
playwright and run playwright install chromium)"` (browser.py:6) instead of
raising, when Playwright isn't installed. When it is available, `_ensure()`
launches `await _play.chromium.launch(headless=True)` (browser.py:23).

Why it doesn't survive naive bundling — two separate problems: (a) since
Playwright isn't in `requirements.txt`, it won't be in the venv
`scripts/build_binary.sh` builds from unless a maintainer installs it
manually first, so today's prototype binary has the browser tools disabled
by design, matching the existing "soft dependency" behavior; (b) even if the
`playwright` Python package were added, its actual browser binaries
(Chromium, and optionally Firefox/WebKit) are not part of the pip wheel — they
are downloaded separately by running `playwright install chromium`, which
writes them to a cache directory (`~/.cache/ms-playwright` on Linux, an
analogous path on macOS/Windows). PyInstaller's import scanner has no way to
discover or bundle that cache directory; it only sees Python imports, not an
imperative post-install download step that writes files outside the package.

Full solution (needs an explicit decision, not made here — follow-up):
either (i) ship the `~/.cache/ms-playwright` (or equivalent) browser cache
directory alongside the frozen binary and point Playwright at it via the
`PLAYWRIGHT_BROWSERS_PATH` environment variable at startup, bundling it into
the one-dir `dist/engine-server/` output as extra `datas`, which grows the
distributable significantly (Chromium alone is >100MB) but makes the binary
self-contained; or (ii) keep `playwright install chromium` as a documented,
required post-install step on every target machine, keeping the frozen
binary itself small but reintroducing an external-state dependency identical
in spirit to npx/gopls above. This prototype does neither and simply
preserves today's soft-dependency behavior (`browser tools unavailable` when
Playwright isn't present).

## Explicitly out of scope for this change

Solving all four trouble spots above (empirical tree-sitter
`--collect-all` verification, LSP subprocess environment sanitization for
npx/node and gopls, and a Playwright/Chromium bundling decision +
implementation) is deliberately **not** done in this PR. This PR delivers
the build script and this investigation writeup only. Each trouble spot
above is scoped to be picked up as an independent follow-up.
