#!/usr/bin/env bash
# Build one-dir PyInstaller binaries for the engine server and dev client.
#
# Part B packaging prototype. See docs/packaging.md for the full writeup of
# why PyInstaller was chosen and what is/isn't covered by this build.
#
# Assumption: this script does NOT create a venv for you. Activate the venv
# you want to build with before running it (or run it in CI right after
# `python -m venv .venv && source .venv/bin/activate`). That keeps the script
# simple and avoids guessing at venv tooling (venv vs virtualenv vs uv) that
# isn't otherwise used in this repo. Re-running is safe: pip install is
# idempotent and PyInstaller is invoked with --noconfirm to overwrite any
# previous dist/build output.
#
# Explicitly OUT OF SCOPE for this prototype (see docs/packaging.md):
#   - tree-sitter grammar binaries (tree_sitter_{python,javascript,typescript,go})
#     may or may not be picked up by PyInstaller's static import scanner; this
#     script does not add --collect-all for them yet.
#   - npx-spawned language servers (pyright, typescript-language-server) still
#     require Node.js + npx on the target machine's PATH; not bundled.
#   - gopls still must be preinstalled on the target machine's PATH; not bundled.
#   - Playwright's Chromium download (via `playwright install chromium`) is not
#     bundled or automated by this script.
#
# Usage:
#   ./scripts/build_binary.sh
#
# Output:
#   dist/engine-server/engine-server   (wraps app.py)
#   dist/engine-client/engine-client   (wraps dummy_client.py)

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "==> Installing runtime + build dependencies (requirements.txt + pyinstaller)"
python3 -m pip install --disable-pip-version-check -r requirements.txt pyinstaller

echo "==> Cleaning previous PyInstaller work directories (build/, *.spec)"
rm -rf build/engine-server build/engine-client
rm -f engine-server.spec engine-client.spec

echo "==> Building dist/engine-server (entry point: app.py)"
pyinstaller \
    --name engine-server \
    --noconfirm \
    app.py

echo "==> Building dist/engine-client (entry point: dummy_client.py)"
pyinstaller \
    --name engine-client \
    --noconfirm \
    dummy_client.py

echo "==> Done."
echo "    Server binary: dist/engine-server/engine-server"
echo "    Client binary: dist/engine-client/engine-client"
echo ""
echo "Note: PyInstaller builds are platform/arch-specific (not a cross-compiler)."
echo "Run this script on the same OS/arch you intend to deploy to."
