from __future__ import annotations

import fnmatch
from pathlib import Path

from protocol.snapshot import FileTreeNode

ALWAYS_IGNORE = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    ".tox",
}


def load_gitignore(workspace: Path) -> list[str]:
    path = workspace / ".gitignore"
    if not path.is_file():
        return []
    patterns = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        patterns.append(line.rstrip("/"))
    return patterns


def is_ignored(rel: str, name: str, is_dir: bool, patterns: list[str]) -> bool:
    if name in ALWAYS_IGNORE:
        return True
    needle = rel.rstrip("/") + ("/" if is_dir else "")
    for pattern in patterns:
        check = pattern
        if check.endswith("/"):
            if fnmatch.fnmatch(rel, check.rstrip("/")) or fnmatch.fnmatch(name, check.rstrip("/")):
                return True
            continue
        if fnmatch.fnmatch(name, check) or fnmatch.fnmatch(rel, check) or fnmatch.fnmatch(needle, check):
            return True
    return False


def build_file_tree(workspace: Path, max_entries: int = 4000) -> list[FileTreeNode]:
    root = workspace.resolve()
    patterns = load_gitignore(root)
    remaining = max_entries

    def walk(directory: Path) -> list[FileTreeNode]:
        nonlocal remaining
        nodes: list[FileTreeNode] = []
        try:
            entries = sorted(directory.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
        except OSError:
            return nodes
        for entry in entries:
            if remaining <= 0:
                break
            rel = str(entry.relative_to(root))
            if is_ignored(rel, entry.name, entry.is_dir(), patterns):
                continue
            remaining -= 1
            if entry.is_dir():
                nodes.append(
                    FileTreeNode(
                        name=entry.name,
                        path=rel,
                        is_dir=True,
                        children=walk(entry),
                    )
                )
            else:
                nodes.append(FileTreeNode(name=entry.name, path=rel, is_dir=False))
        return nodes

    return walk(root)
