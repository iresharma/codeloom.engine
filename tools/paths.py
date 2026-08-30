from __future__ import annotations

from pathlib import Path


def resolve_workspace_path(workspace: Path, raw: str) -> Path:
    root = workspace.resolve()
    candidate = Path(raw)
    path = candidate.resolve() if candidate.is_absolute() else (root / raw).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path escapes workspace: {raw}") from exc
    return path


def relative_to_workspace(workspace: Path, path: Path) -> str:
    return str(path.resolve().relative_to(workspace.resolve()))
