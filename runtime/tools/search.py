from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from runtime.tools.fs import (
    SKIP_NAMES,
    WorkspacePathError,
    relative_posix,
    resolve_in_workspace,
)

DEFAULT_MAX_MATCHES = 80
MAX_MATCHES = 200


def search(
    workspace: Path,
    pattern: str,
    path: str = "",
    glob: str = "",
    max_matches: int = DEFAULT_MAX_MATCHES,
) -> str:
    rg = shutil.which("rg")
    if not rg:
        raise RuntimeError("rg not found; install ripgrep")
    if not pattern:
        raise ValueError("pattern is required")

    workspace = workspace.resolve()
    target = workspace
    if path:
        target = resolve_in_workspace(workspace, path)

    take = min(max(1, max_matches), MAX_MATCHES)
    command = [
        rg,
        "--line-number",
        "--no-heading",
        "--color=never",
        "--no-messages",
        "--max-columns",
        "200",
        "--max-columns-preview",
    ]
    for name in sorted(SKIP_NAMES):
        command.extend(["--glob", f"!{name}/**"])
        command.extend(["--glob", f"!{name}"])
    if glob:
        command.extend(["--glob", glob])
    command.append("--")
    command.append(pattern)
    command.append(str(target))

    try:
        result = subprocess.run(
            command,
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("rg timed out") from exc

    if result.returncode not in (0, 1):
        err = (result.stderr or result.stdout or "rg failed").strip()
        raise RuntimeError(err)

    lines = [line for line in result.stdout.splitlines() if line]
    rewritten = [_rewrite_path(workspace, line) for line in lines]
    rewritten = [line for line in rewritten if line is not None]
    if not rewritten:
        return "(no matches)"
    extra = max(0, len(rewritten) - take)
    body = "\n".join(rewritten[:take])
    if extra:
        return f"{body}\n... ({extra} more matches; raise max_matches)"
    return body


def _rewrite_path(workspace: Path, line: str) -> str | None:
    if ":" not in line:
        return line
    raw_path, rest = line.split(":", 1)
    try:
        rel = relative_posix(workspace, Path(raw_path).resolve())
    except (ValueError, WorkspacePathError):
        return None
    return f"{rel}:{rest}"
