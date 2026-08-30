from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from runtime.tools.fs import SKIP_NAMES
from runtime.tools.git import tracked_paths

SUPPORTED = ("python", "go", "javascript")
_LABELS = {
    "python": "python",
    "go": "go",
    "javascript": "javascript/typescript",
}

_MARKERS = {
    "go.mod": "go",
    "go.sum": "go",
    "pyproject.toml": "python",
    "setup.py": "python",
    "setup.cfg": "python",
    "requirements.txt": "python",
    "Pipfile": "python",
    "poetry.lock": "python",
    "tsconfig.json": "javascript",
    "jsconfig.json": "javascript",
    "package.json": "javascript",
    "package-lock.json": "javascript",
    "pnpm-lock.yaml": "javascript",
    "yarn.lock": "javascript",
    "bun.lock": "javascript",
    "bun.lockb": "javascript",
}

_EXTENSIONS = {
    ".py": "python",
    ".pyi": "python",
    ".go": "go",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "javascript",
    ".tsx": "javascript",
    ".mts": "javascript",
    ".cts": "javascript",
}

_OTHER_EXTENSIONS = {
    ".rs": "rust",
    ".rb": "ruby",
    ".java": "java",
    ".kt": "kotlin",
    ".swift": "swift",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cs": "csharp",
    ".php": "php",
    ".scala": "scala",
    ".hs": "haskell",
    ".ex": "elixir",
    ".exs": "elixir",
    ".lua": "lua",
    ".zig": "zig",
}

MAX_FILES = 20_000


@dataclass
class LanguageInfo:
    name: str | None
    supported: bool
    file_counts: dict[str, int]
    warning: str | None

    @property
    def label(self) -> str:
        if self.name is None:
            return "unknown"
        return _LABELS.get(self.name, self.name)


def detect(workspace: Path) -> LanguageInfo:
    workspace = workspace.resolve()
    paths = tracked_paths(workspace)
    if paths is None:
        paths = _walk(workspace)
    counts: Counter[str] = Counter()
    for rel in paths:
        lang = _language_for(rel)
        if lang:
            counts[lang] += 1
    markers = _root_markers(workspace)
    name = _pick(counts, markers)
    supported = name in SUPPORTED
    warning = None
    if name is None:
        warning = (
            "could not detect a project language; "
            "tree-sitter and LSP are not available. "
            "supported: python, go, javascript/typescript"
        )
    elif not supported:
        label = _LABELS.get(name, name)
        warning = (
            f"tree-sitter and LSP are not available for this project "
            f"(detected: {label}). "
            "supported: python, go, javascript/typescript"
        )
    return LanguageInfo(
        name=name,
        supported=supported,
        file_counts=dict(counts),
        warning=warning,
    )


def _pick(counts: Counter[str], markers: set[str]) -> str | None:
    if len(markers) == 1:
        marked = next(iter(markers))
        if counts:
            top, n = counts.most_common(1)[0]
            marked_n = counts.get(marked, 0)
            if top != marked and n >= 5 and n >= 2 * max(marked_n, 1):
                return top
        return marked
    if counts:
        return counts.most_common(1)[0][0]
    if markers:
        return sorted(markers)[0]
    return None


def _root_markers(workspace: Path) -> set[str]:
    found: set[str] = set()
    try:
        names = {entry.name for entry in workspace.iterdir()}
    except OSError:
        return found
    for name, language in _MARKERS.items():
        if name in names:
            found.add(language)
    return found


def _language_for(rel: str) -> str | None:
    suffix = Path(rel).suffix.lower()
    return _EXTENSIONS.get(suffix) or _OTHER_EXTENSIONS.get(suffix)


def _walk(workspace: Path) -> list[str]:
    paths: list[str] = []
    for entry in workspace.rglob("*"):
        if len(paths) >= MAX_FILES:
            break
        if not entry.is_file():
            continue
        if any(part in SKIP_NAMES for part in entry.relative_to(workspace).parts):
            continue
        paths.append(entry.relative_to(workspace).as_posix())
    return paths
