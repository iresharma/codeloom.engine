from __future__ import annotations

import shutil
import subprocess

TOOLS = (
    ("python", (("python3", "--version"), ("python", "--version"))),
    ("node", (("node", "--version"),)),
    ("go", (("go", "version"),)),
    ("git", (("git", "--version"),)),
    ("gh", (("gh", "--version"),)),
    ("rg", (("rg", "--version"),)),
)


def runtime_info() -> str:
    lines = []
    for label, candidates in TOOLS:
        found = False
        for args in candidates:
            binary = shutil.which(args[0])
            if not binary:
                continue
            try:
                result = subprocess.run(
                    [binary, *args[1:]],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            text = (result.stdout or result.stderr or "").strip().splitlines()
            version = text[0] if text else "(unknown)"
            lines.append(f"{label}: {version}")
            found = True
            break
        if not found:
            lines.append(f"{label}: (not found)")
    return "\n".join(lines)
