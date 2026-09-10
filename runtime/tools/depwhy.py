from __future__ import annotations

import shutil
from pathlib import Path

from runtime.tools.git import exec_cmd

ECOSYSTEMS = ("npm", "go", "pypi", "crates")
OUT_CAP = 20_000


def dep_why(workspace: Path, ecosystem: str, name: str) -> str:
    eco = (ecosystem or "").strip().lower()
    name = (name or "").strip()
    if eco not in ECOSYSTEMS:
        return f"error: ecosystem must be one of {', '.join(ECOSYSTEMS)}"
    if not name:
        return "error: name is required"
    workspace = Path(workspace).resolve()
    commands = {
        "npm": (["npm", "ls", name, "--depth=20"], "npm"),
        "go": (["go", "mod", "why", name], "go"),
        "pypi": (["python3", "-m", "pip", "show", name], "python3"),
        "crates": (["cargo", "tree", "-i", name, "--prefix", "none"], "cargo"),
    }
    args, binary = commands[eco]
    if not shutil.which(binary):
        return f"error: {binary} not installed"
    result = exec_cmd(workspace, args, timeout=60)
    text = (result.stdout or result.stderr or "").strip()
    if result.returncode != 0 and not text:
        return f"error: {binary} failed"
    if not text:
        return "(no output)"
    if len(text) > OUT_CAP:
        return text[:OUT_CAP] + "\n...[truncated]"
    return text
