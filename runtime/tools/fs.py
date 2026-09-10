from __future__ import annotations

from pathlib import Path

from protocol.snapshot import FileTreeNode

SKIP_NAMES = {
    ".git",
    ".engine",
    ".cursor",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    ".ruff_cache",
    "ruff_cache",
    ".mypy_cache",
    ".pytest_cache",
    ".hypothesis",
    ".tox",
    ".nox",
    ".cache",
    ".eggs",
    ".ipynb_checkpoints",
    ".parcel-cache",
    ".next",
    ".nuxt",
    ".turbo",
    ".nyc_output",
    "htmlcov",
    "coverage",
    "site-packages",
    "dist",
    "build",
}

_SKIP_SUFFIXES = (".egg-info", ".dist-info")


def should_skip_name(name: str) -> bool:
    """True for caches, venvs, and other dirs that are not project source."""
    if name in SKIP_NAMES:
        return True
    if name.endswith(_SKIP_SUFFIXES):
        return True
    return bool(name.startswith(".") and "cache" in name.lower())


class WorkspacePathError(ValueError):
    pass


def resolve_in_workspace(workspace: Path, path: str) -> Path:
    workspace = workspace.resolve()
    candidate = Path(path)
    resolved = candidate.resolve() if candidate.is_absolute() else (workspace / candidate).resolve()
    try:
        resolved.relative_to(workspace)
    except ValueError:
        raise WorkspacePathError("path is outside the workspace")
    return resolved


def relative_posix(workspace: Path, resolved: Path) -> str:
    return resolved.relative_to(workspace.resolve()).as_posix()


def list_tree(workspace: Path) -> list[FileTreeNode]:
    workspace = workspace.resolve()
    if not workspace.is_dir():
        return []
    return _list_dir(workspace, workspace)


def read_text(workspace: Path, path: str) -> tuple[str, str]:
    from runtime.tools.fileid import read_source

    src = read_source(workspace, path)
    return src.rel, src.text


DEFAULT_READ_LIMIT = 200
MAX_READ_LIMIT = 400


def format_window(rel: str, content: str, offset: int, limit: int) -> str:
    lines = content.splitlines()
    total = len(lines)
    if total == 0:
        return f"{rel}  empty"
    start = max(1, offset)
    take = min(max(1, limit), MAX_READ_LIMIT)
    if start > total:
        return f"{rel}  offset {start} is past end ({total} lines)"
    end = min(total, start + take - 1)
    width = len(str(total))
    body = "\n".join(
        f"{index:>{width}}|{lines[index - 1]}"
        for index in range(start, end + 1)
    )
    header = f"{rel}  lines {start}-{end} of {total}"
    if end < total:
        header += f"  (next offset={end + 1})"
    return f"{header}\n{body}"


def read_window(
    workspace: Path,
    path: str,
    offset: int = 1,
    limit: int = DEFAULT_READ_LIMIT,
) -> str:
    from runtime.tools.fileid import read_source

    src = read_source(workspace, path)
    return format_window(src.rel, src.text, offset, limit)


def _list_dir(workspace: Path, directory: Path) -> list[FileTreeNode]:
    nodes = []
    try:
        entries = sorted(directory.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
    except OSError:
        return []
    for entry in entries:
        if should_skip_name(entry.name):
            continue
        rel = relative_posix(workspace, entry)
        if entry.is_dir():
            nodes.append(
                FileTreeNode(
                    name=entry.name,
                    path=rel,
                    is_dir=True,
                    children=_list_dir(workspace, entry),
                )
            )
        elif entry.is_file():
            nodes.append(
                FileTreeNode(name=entry.name, path=rel, is_dir=False, children=None)
            )
    return nodes
