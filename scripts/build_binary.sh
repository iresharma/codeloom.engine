#!/usr/bin/env bash
#
# build_binary.sh — package the engine server and headless client as
# standalone (onedir) binaries with PyInstaller.
#
# Usage:
#   ./scripts/build_binary.sh
#
# Output:
#   dist/agent-engine-server/   onedir bundle wrapping app.py (the server)
#   dist/agent-engine-client/   onedir bundle wrapping headless_client.py
#
# Prerequisites:
#   - Run this on the OS/arch you intend to ship for. PyInstaller does not
#     cross-compile: a Linux artifact must be built on Linux, a macOS
#     artifact must be built on macOS. There is no way around this with
#     PyInstaller (same is true of the other candidates evaluated —
#     Nuitka, shiv/zipapp — see docs/packaging.md).
#   - A Python matching the project's floor (see requirements.txt / repo
#     docs) with this repo's dependencies importable (either activate the
#     project's existing venv first, or let pip install into whatever
#     interpreter runs this script).
#   - Network access to PyPI to install PyInstaller if it isn't already
#     present.
#
# This produces onedir bundles, not onefile: startup is faster on repeated
# invocations (this is a server + CLI invoked repeatedly, not a run-once
# script), it avoids onefile's failure mode on hosts where /tmp is mounted
# noexec (onefile self-extracts to a temp dir on every launch), and the
# collected .so/data files are visible on disk for debugging.
#
# NOT covered by this build (see docs/packaging.md for why and what a full
# fix would require): Node.js/npx for the pyright/typescript-language-server
# LSP backends, a gopls binary on PATH for Go, Playwright's browser
# binaries (`playwright install chromium`), and ripgrep (`rg`) for the
# search tool. All of these are external processes/binaries looked up on
# PATH at runtime, not Python imports, so PyInstaller's import-graph
# analysis cannot and does not bundle them.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

PYTHON="${PYTHON:-python3}"
DIST_DIR="dist"
WORK_DIR="build/pyinstaller"

echo "== build_binary.sh: PyInstaller onedir build =="
echo "python: $($PYTHON --version 2>&1)"
echo "platform: $(uname -s) $(uname -m)"

if ! "$PYTHON" -m PyInstaller --version >/dev/null 2>&1; then
    echo "PyInstaller not found for $PYTHON; installing..."
    if ! "$PYTHON" -m pip install --quiet pyinstaller; then
        echo "error: could not install pyinstaller via pip." >&2
        echo "       install it manually (e.g. '$PYTHON -m pip install pyinstaller')" >&2
        echo "       and re-run this script." >&2
        exit 1
    fi
fi

if ! "$PYTHON" -m PyInstaller --version >/dev/null 2>&1; then
    echo "error: pyinstaller was installed but is not runnable via '$PYTHON -m PyInstaller --version'." >&2
    exit 1
fi

echo "pyinstaller: $("$PYTHON" -m PyInstaller --version 2>&1)"

# Packages whose default import-graph analysis is unreliable enough to be
# worth forcing in wholesale rather than trusting PyInstaller's hooks:
# the four tree-sitter grammar packages are compiled-C-extension wheels,
# and mcp/openrouter/typesafe-sdk/textual/prometheus_client all do enough
# dynamic importing (plugin registries, entry points) that a missed
# submodule is a silent runtime failure rather than a build error.
COLLECT_ALL=(
    tree_sitter
    tree_sitter_python
    tree_sitter_javascript
    tree_sitter_typescript
    tree_sitter_go
    openrouter
    typesafe_sdk
    textual
    prometheus_client
)

collect_flags=()
for pkg in "${COLLECT_ALL[@]}"; do
    collect_flags+=(--collect-all "$pkg")
done

# mcp gets narrower treatment than the packages above: its optional
# `mcp.cli` submodule does `import typer` at module scope and calls
# sys.exit(1) if typer isn't installed. typer lives behind the optional
# `mcp[cli]` extra and is not in requirements.txt — the engine never
# imports mcp.cli at runtime, so pulling it into analysis at all is wrong
# scope, not a missing dependency to add.
#
# Neither `--collect-all mcp` nor `--collect-submodules mcp` can be fixed
# by pairing them with `--exclude-module mcp.cli`: excludes only apply
# inside Analysis, which runs *after* collect_submodules() has already
# imported mcp.cli to enumerate it and already raised SystemExit. The
# `filter=` argument of `collect_submodules()` (unexposed on the CLI) is
# the only hook to skip mcp.cli *before* it's imported, so this is done
# via a small custom hook file instead of a CLI flag.
collect_flags+=(--additional-hooks-dir scripts/pyinstaller_hooks)

echo
echo "-- building agent-engine-server (app.py) --"
"$PYTHON" -m PyInstaller \
    --name agent-engine-server \
    --onedir \
    --noconfirm \
    --distpath "$DIST_DIR" \
    --workpath "$WORK_DIR" \
    "${collect_flags[@]}" \
    app.py

echo
echo "-- building agent-engine-client (headless_client.py) --"
"$PYTHON" -m PyInstaller \
    --name agent-engine-client \
    --onedir \
    --noconfirm \
    --distpath "$DIST_DIR" \
    --workpath "$WORK_DIR" \
    "${collect_flags[@]}" \
    headless_client.py

echo
echo "== done =="
echo "server bundle: $DIST_DIR/agent-engine-server/"
echo "client bundle: $DIST_DIR/agent-engine-client/"
echo
echo "Reminder: Node.js (for pyright/typescript-language-server via npx),"
echo "gopls, and Playwright's browsers (playwright install chromium) are"
echo "NOT bundled and must be provisioned on the host separately. See"
echo "docs/packaging.md."
