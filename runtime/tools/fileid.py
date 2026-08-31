from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from runtime.tools.fs import SKIP_NAMES, WorkspacePathError, relative_posix, resolve_in_workspace

LOCKFILE_NAMES = {
    "package-lock.json",
    "uv.lock",
    "poetry.lock",
    "Cargo.lock",
    "go.sum",
}

_ENV_NAMES = {"env.sh", ".env"}
_BOM = b"\xef\xbb\xbf"


@dataclass
class FileSource:
    rel: str
    resolved: Path
    text: str
    raw_sha256: str
    newline: str
    trailing_newline: bool
    encoding: str
    indent: str


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def detect_newline(data: bytes) -> str:
    crlf = data.count(b"\r\n")
    # Count CR and LF that are not part of CRLF.
    stripped = data.replace(b"\r\n", b"")
    cr = stripped.count(b"\r")
    lf = stripped.count(b"\n")
    if crlf >= cr and crlf >= lf and crlf > 0:
        return "\r\n"
    if cr > lf:
        return "\r"
    return "\n"


def detect_indent(text: str) -> str:
    tabs = 0
    space_widths: list[int] = []
    for line in text.splitlines():
        if not line or line.lstrip() == line:
            continue
        if line.startswith("\t"):
            tabs += 1
            continue
        width = 0
        for char in line:
            if char != " ":
                break
            width += 1
        if width:
            space_widths.append(width)
    if tabs and tabs >= len(space_widths):
        return "\t"
    if space_widths:
        for size in (4, 2, 8, 3):
            if all(width % size == 0 for width in space_widths):
                return " " * size
        return " " * space_widths[0]
    return "    "


def decode_source(data: bytes) -> tuple[str, str, str, bool]:
    encoding = "utf-8-sig" if data.startswith(_BOM) else "utf-8"
    newline = detect_newline(data)
    trailing = data.endswith((b"\n", b"\r"))
    body = data[len(_BOM) :] if encoding == "utf-8-sig" else data
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkspacePathError("file is not valid UTF-8") from exc
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text, newline, encoding, trailing


def read_source(workspace: Path, path: str) -> FileSource:
    resolved = resolve_in_workspace(workspace, path)
    if not resolved.exists():
        raise FileNotFoundError(path)
    if not resolved.is_file():
        raise WorkspacePathError("not a file")
    try:
        data = resolved.read_bytes()
    except OSError as exc:
        raise WorkspacePathError(str(exc)) from exc
    text, newline, encoding, trailing = decode_source(data)
    return FileSource(
        rel=relative_posix(workspace, resolved),
        resolved=resolved,
        text=text,
        raw_sha256=sha256_bytes(data),
        newline=newline,
        trailing_newline=trailing,
        encoding=encoding,
        indent=detect_indent(text),
    )


def render(src: FileSource, new_text: str) -> bytes:
    text = new_text.replace("\r\n", "\n").replace("\r", "\n")
    if src.trailing_newline:
        if text and not text.endswith("\n"):
            text += "\n"
        elif not text:
            text = "\n"
    elif text.endswith("\n"):
        text = text[:-1]
    body = text.replace("\n", src.newline).encode("utf-8")
    if src.encoding == "utf-8-sig":
        return _BOM + body
    return body


def identity_from_sibling(directory: Path, suffix: str) -> tuple[str, bool, str, str]:
    """Return newline, trailing_newline, encoding, indent from a sibling file."""
    if directory.is_dir():
        try:
            entries = sorted(directory.iterdir(), key=lambda item: item.name.lower())
        except OSError:
            entries = []
        for entry in entries:
            if not entry.is_file() or entry.is_symlink():
                continue
            if entry.suffix != suffix:
                continue
            try:
                data = entry.read_bytes()
            except OSError:
                continue
            try:
                text, newline, encoding, trailing = decode_source(data)
            except WorkspacePathError:
                continue
            return newline, trailing, encoding, detect_indent(text)
    return "\n", True, "utf-8", "    "


def synthetic_source(
    workspace: Path,
    resolved: Path,
    text: str,
    newline: str,
    trailing_newline: bool,
    encoding: str,
    indent: str,
) -> FileSource:
    return FileSource(
        rel=relative_posix(workspace, resolved),
        resolved=resolved,
        text=text,
        raw_sha256="",
        newline=newline,
        trailing_newline=trailing_newline,
        encoding=encoding,
        indent=indent,
    )


def guard_write_path(workspace: Path, path: str) -> Path:
    """Refuse writes outside the workspace, through symlinks, or into denylisted paths."""
    workspace = workspace.resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = workspace / candidate
    if os.path.lexists(candidate) and os.path.islink(candidate):
        raise WorkspacePathError("refusing to write through a symlink")
    resolved = candidate.resolve()
    try:
        rel = resolved.relative_to(workspace)
    except ValueError:
        raise WorkspacePathError("path is outside the workspace")
    for part in rel.parts:
        if part in SKIP_NAMES:
            raise WorkspacePathError(f"writes to {part}/ are not allowed")
    name = resolved.name
    if name in LOCKFILE_NAMES:
        raise WorkspacePathError(f"writes to lockfile '{name}' are not allowed")
    if name in _ENV_NAMES or name.startswith(".env."):
        raise WorkspacePathError(f"writes to '{name}' are not allowed")
    return resolved
