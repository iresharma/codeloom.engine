#!/usr/bin/env bash
# build_binary.sh — package the engine server + headless client as
# standalone PyInstaller binaries.
#
# Why PyInstaller (see docs/packaging.md for the full writeup):
#   - This codebase is Python 3.10+, asyncio/stdlib-heavy, Unix-socket based,
#     with no numeric/heavy-C stack to worry about.
#   - PyInstaller ships with pyinstaller-hooks-contrib, which already has a
#     hook for `rich` (hook-rich.py) and community-documented patterns for
#     `textual` (collect_all), covering the TUI-adjacent deps.
#   - It needs no C compiler on the build machine. Nuitka compiles Python to
#     C and links it, so it requires a working C toolchain wherever you build
#     — a heavier and more fragile requirement than we need for this PR.
#   - shiv/zipapp were considered and rejected: tree-sitter's grammar
#     packages ship compiled `_binding` C extensions, and CPython's own
#     zipapp docs (Caveats section) say a package with a C extension "cannot
#     be run from a zip file". shiv's actual workaround is to self-extract
#     site-packages to ~/.shiv/<hash> on first run and import from real disk
#     — a materially weaker "single binary" guarantee than PyInstaller's
#     onedir/onefile modes, and it requires a writable $HOME on the target
#     machine.
#
# Scope of this script (intentionally small, see docs/packaging.md):
#   - app.py               -> dist/app/app                (the server)
#   - dummy_client.py      -> dist/dummy_client/dummy_client (headless client
#                              path only, i.e. `--message ... --auto`, which
#                              lazily imports headless_client.run_once; the
#                              textual TUI path via client_tui.py is NOT
#                              covered here — see docs/packaging.md's
#                              "full solution" section)
#
# Onedir vs onefile: we build --onedir, not --onefile. Onefile unpacks
# itself to a temp dir on every launch, which is slower to start and makes
# "a dynamically-discovered import is missing" failures (see the NOTE below)
# much harder to debug, because there's no directory to inspect. Onedir is
# still "one build artifact directory to ship" — you tar/zip dist/app/ and
# dist/dummy_client/ and copy them wherever you need — but gives you a real
# _internal/ tree you can `ls` when something doesn't import.
#
# NOTE (biggest real risk in this build, bigger than tree-sitter):
#   tools/registry.py::discover_tools() uses pkgutil.walk_packages() +
#   importlib.import_module() to dynamically import *every* module under the
#   `tools` package at server startup (runtime/session.py calls this at
#   EngineSession construction time). This includes tools/sitter.py,
#   tools/browser.py, tools/lsp.py, tools/http.py, etc., none of which are
#   ever spelled out as a static `import tools.sitter` anywhere for
#   PyInstaller's import-graph analysis to find. Left alone, PyInstaller will
#   NOT bundle those modules, and the frozen server will throw ImportError
#   for tools it discovers dynamically but the bundler's static analysis
#   never saw. We work around this below with --collect-submodules=tools and
#   --collect-submodules=runtime.tools, which force-include every module
#   under both packages regardless of whether anything imports them
#   statically.
#
# NOTE (tree-sitter): runtime/tools/sitter.py does
#   `import tree_sitter_python as tspython` etc. — real static imports, so
#   PyInstaller's binary walker should pick up each package's compiled
#   `_binding*.so` on its own. But there is no pyinstaller-hooks-contrib hook
#   for tree_sitter / tree_sitter_python / tree_sitter_javascript /
#   tree_sitter_typescript / tree_sitter_go (checked, absent as of writing),
#   so we add explicit --collect-all flags for all five packages as a
#   belt-and-suspenders measure to make sure the compiled extension and any
#   package_data lands in the frozen tree. This has NOT been empirically
#   verified by an actual build run in this environment — see
#   docs/packaging.md for the required follow-up (build, then inspect
#   dist/app/_internal/ for the *_binding*.so files).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

# --- 1. Make sure pyinstaller is available in the active venv/interpreter ---
# Mirrors the README's "pip install -r requirements.txt into a venv" flow;
# pyinstaller is a build-time-only tool so it deliberately is not added to
# requirements.txt (which is installed for running/testing the engine).
if ! python3 -m PyInstaller --version >/dev/null 2>&1; then
    echo "pyinstaller not found in the current interpreter; installing..."
    python3 -m pip install "pyinstaller>=6.0"
fi

# Shared hidden-import / collect flags. Both entry points pull in the same
# tools/ and runtime/tools/ packages (the server hosts them; the headless
# client only talks JSON over the socket but importing dummy_client.py drags
# in runtime.tools.git for settle-intent parsing), so we keep one list and
# reuse it for both builds instead of drifting two copies.
COMMON_FLAGS=(
    --noconfirm
    --clean
    # Force in everything discover_tools() will pull in at runtime that
    # PyInstaller's static analysis cannot see (see NOTE above).
    --collect-submodules=tools
    --collect-submodules=runtime.tools
    # Tree-sitter grammar packages: real static imports in
    # runtime/tools/sitter.py, but no upstream PyInstaller hook exists yet,
    # so collect everything (binaries + data + submodules) explicitly.
    --collect-all=tree_sitter
    --collect-all=tree_sitter_python
    --collect-all=tree_sitter_javascript
    --collect-all=tree_sitter_typescript
    --collect-all=tree_sitter_go
)

echo "=== Building server (app.py) -> dist/app/ ==="
python3 -m PyInstaller \
    "${COMMON_FLAGS[@]}" \
    --name app \
    --onedir \
    app.py

echo "=== Building headless client (dummy_client.py) -> dist/dummy_client/ ==="
# Scope note: this bundles dummy_client.py's headless (--message/--auto)
# path only. client_tui.py (the textual TUI) is intentionally out of scope
# for this build — see docs/packaging.md "Full solution would require".
python3 -m PyInstaller \
    "${COMMON_FLAGS[@]}" \
    --name dummy_client \
    --onedir \
    dummy_client.py

cat <<'EOF'

=== Build complete ===

Server:
  dist/app/app <workspace>

Client (headless path only):
  dist/dummy_client/dummy_client <workspace> --message "hello" --auto

Try it end to end:
  1. dist/app/app /path/to/your/project
  2. in another terminal:
     dist/dummy_client/dummy_client /path/to/your/project --message "openfile app.py" --auto

Target-machine prerequisites NOT bundled by this build (see docs/packaging.md):
  - Node.js + npm/npx on PATH (for the pyright / typescript-language-server
    tools; npx -y still does a first-run fetch that needs network access
    unless the npm cache is pre-warmed)
  - Go + gopls on PATH (for the Go language server tool)
  - Playwright browser binaries, if browser tools are needed: run
    `playwright install chromium` on the target machine, or ship the
    ~/.cache/ms-playwright directory alongside the binary and set
    PLAYWRIGHT_BROWSERS_PATH to point at it
EOF
