from __future__ import annotations


class FileTracker:
    """Maps workspace-relative paths to the sha256 of bytes last read or written."""

    def __init__(self) -> None:
        self._shas: dict[str, str] = {}

    def mark(self, rel: str, sha: str) -> None:
        self._shas[rel] = sha

    def get(self, rel: str) -> str | None:
        return self._shas.get(rel)
