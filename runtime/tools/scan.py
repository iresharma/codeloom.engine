from __future__ import annotations

import re
from pathlib import Path

from runtime.tools.fs import SKIP_NAMES, WorkspacePathError, relative_posix, resolve_in_workspace

MARKERS = ("TODO", "FIXME", "XXX", "HACK")
DEFAULT_LIMIT = 80
_DEFAULT = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b")


def todo_scan(
    workspace: Path, *, path: str = "", pattern: str = "", limit: int = DEFAULT_LIMIT
) -> str:
    workspace = Path(workspace).resolve()
    take = max(1, min(int(limit or DEFAULT_LIMIT), 200))
    extra = (pattern or "").strip()
    if extra:
        try:
            matcher = re.compile(extra)
        except re.error as exc:
            return f"error: bad pattern: {exc}"
    else:
        matcher = _DEFAULT
    root = workspace
    if path.strip():
        try:
            root = resolve_in_workspace(workspace, path.strip())
        except WorkspacePathError as exc:
            return f"error: {exc}"
        if not root.exists():
            return f"error: {path} not found"
    hits: list[str] = []
    for file_path in _iter_files(workspace, root):
        try:
            text = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = relative_posix(workspace, file_path)
        for index, line in enumerate(text.splitlines(), start=1):
            if matcher.search(line):
                hits.append(f"{rel}:{index}:{line.rstrip()}")
                if len(hits) >= take:
                    hits.append("...[truncated]")
                    return "\n".join(hits)
    return "\n".join(hits) if hits else "(none)"


def _iter_files(workspace: Path, root: Path):
    if root.is_file():
        yield root
        return
    if not root.is_dir():
        return
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.name in SKIP_NAMES:
                continue
            if entry.is_dir():
                stack.append(entry)
            elif entry.is_file():
                yield entry
