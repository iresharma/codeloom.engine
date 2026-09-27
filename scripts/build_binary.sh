#!/usr/bin/env bash
#
# build_binary.sh — packaging prototype (PyInstaller, --onedir).
#
# Builds standalone --onedir bundles for the two pure-Python entry points:
#   - app.py            (the server)
#   - dummy_client.py   (the headless client)
#
# Scope: this targets the pure-Python core (server + client + their Python
# deps from requirements.txt: tree-sitter grammar wheels, textual, mcp,
# openrouter, etc.) ONLY. It does NOT and cannot bundle:
#   - npx-spawned language servers (pyright, typescript-language-server)
#   - gopls (Go binary)
#   - Playwright browser binaries
# See docs/packaging.md for why, and for the scoped follow-up plan to
# close those gaps.
#
# Usage: scripts/build_binary.sh
#
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if ! command -v pyinstaller >/dev/null 2>&1; then
    echo "pyinstaller not found; attempting 'pip install pyinstaller'..."
    if ! pip install pyinstaller; then
        echo "error: pyinstaller is not installed and 'pip install pyinstaller' failed." >&2
        echo "Install it manually with: pip install pyinstaller" >&2
        exit 1
    fi
fi

# Coarse fix for tree-sitter grammar wheels: no PyInstaller/hooks-contrib
# hook exists for tree_sitter or the per-language grammar packages, so we
# brute-force collect everything with --collect-all / --collect-submodules.
# This is coarser (bigger bundle) than a hand-rolled hook using
# collect_dynamic_libs()/copy_metadata(), but it works. See docs/packaging.md.
TREE_SITTER_FLAGS=(
    --collect-all tree_sitter_python
    --collect-all tree_sitter_javascript
    --collect-all tree_sitter_typescript
    --collect-all tree_sitter_go
    --collect-submodules tree_sitter
)

echo "== Building app.py (server) =="
pyinstaller --onedir --name app --noconfirm "${TREE_SITTER_FLAGS[@]}" app.py

echo "== Building dummy_client.py (headless client) =="
pyinstaller --onedir --name dummy_client --noconfirm "${TREE_SITTER_FLAGS[@]}" dummy_client.py

cat <<EOF

Build complete.
  Server binary:  dist/app/app
  Client binary:  dist/dummy_client/dummy_client

Not bundled (must remain external on the target machine — see docs/packaging.md):
  - Node.js/npm/npx (needed for pyright-langserver, typescript-language-server)
  - gopls (Go toolchain / binary)
  - Playwright browser binaries (run 'playwright install' separately if/when
    Playwright is added as a dependency)
EOF
