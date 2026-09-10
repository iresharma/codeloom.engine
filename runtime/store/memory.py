from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from runtime.tools.fileid import sha256_bytes
from runtime.tools.fs import WorkspacePathError, relative_posix, resolve_in_workspace

SECTIONS = ("engineering", "product", "cicd", "other")
SECTION_CAP = 12
FILE_NOTE_CAP = 40
TOUCH_ONLY_KEEP = 80
RENDER_TOUCH_ONLY = 10
RENDER_CAP = 8000
NOTE_CLIP = 400
SHA_SHORT = 7

_LOCK = threading.Lock()
_SECTION_HEADINGS = {
    "engineering": "Engineering",
    "product": "Product",
    "cicd": "CI/CD",
    "other": "Other",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _file(workspace: Path) -> Path:
    return Path(workspace) / ".engine" / "memory.json"


def _empty() -> dict:
    return {
        "files": {},
        "engineering": [],
        "product": [],
        "cicd": [],
        "other": [],
    }


def _load_unlocked(path: Path) -> dict:
    data = _empty()
    if not path.is_file():
        return data
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return data
    if not isinstance(raw, dict):
        return data
    files = raw.get("files")
    if isinstance(files, dict):
        cleaned = {}
        for key, value in files.items():
            if isinstance(key, str) and isinstance(value, dict):
                cleaned[key] = dict(value)
        data["files"] = cleaned
    for section in SECTIONS:
        items = raw.get(section)
        if not isinstance(items, list):
            continue
        kept = []
        for item in items:
            if isinstance(item, dict) and str(item.get("text") or "").strip():
                kept.append(
                    {
                        "text": str(item.get("text") or ""),
                        "updated_at": str(item.get("updated_at") or ""),
                    }
                )
        data[section] = kept
    return data


def _save_unlocked(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _clip(text: str, limit: int = NOTE_CLIP) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _prune(data: dict) -> None:
    files = data.get("files") or {}
    noted = []
    touches = []
    for path, entry in files.items():
        if not isinstance(entry, dict):
            continue
        if str(entry.get("note") or "").strip():
            noted.append((path, entry))
        else:
            touches.append((path, entry))
    noted.sort(key=lambda item: item[1].get("updated_at") or "", reverse=True)
    touches.sort(key=lambda item: item[1].get("updated_at") or "", reverse=True)
    kept = dict(noted[:FILE_NOTE_CAP])
    for path, entry in touches[:TOUCH_ONLY_KEEP]:
        kept[path] = entry
    data["files"] = kept
    for section in SECTIONS:
        items = list(data.get(section) or [])
        data[section] = items[-SECTION_CAP:]


def _rel_path(workspace: Path, path: str) -> str:
    resolved = resolve_in_workspace(workspace, path)
    return relative_posix(workspace, resolved)


def _disk_sha(workspace: Path, rel: str) -> str | None:
    try:
        resolved = resolve_in_workspace(workspace, rel)
    except WorkspacePathError:
        return None
    if not resolved.is_file():
        return None
    try:
        return sha256_bytes(resolved.read_bytes())
    except OSError:
        return None


def _drop_missing(workspace: Path, data: dict) -> bool:
    files = data.get("files") or {}
    kept = {}
    dropped = False
    for rel, entry in files.items():
        if _disk_sha(workspace, rel) is None:
            dropped = True
            continue
        kept[rel] = entry
    data["files"] = kept
    return dropped


def load(workspace: Path) -> dict:
    path = _file(workspace)
    with _LOCK:
        return _load_unlocked(path)


def touch(workspace: Path, path: str, sha: str, action: str) -> None:
    rel = str(path or "").strip()
    digest = str(sha or "").strip()
    if not rel or not digest:
        return
    kind = "edit" if action == "edit" else "read"

    def apply(data: dict) -> None:
        files = data.setdefault("files", {})
        entry = dict(files.get(rel) or {})
        entry["seen_sha"] = digest
        entry["action"] = kind
        entry["updated_at"] = _now()
        if "note" not in entry:
            entry["note"] = ""
        if "note_sha" not in entry:
            entry["note_sha"] = None
        files[rel] = entry

    path_obj = _file(workspace)
    with _LOCK:
        data = _load_unlocked(path_obj)
        apply(data)
        _prune(data)
        _save_unlocked(path_obj, data)


def remember(workspace: Path, section: str, note: str, path: str = "") -> str:
    kind = (section or "").strip().lower()
    text = _clip(note)
    if not text:
        return "error: note is empty"
    if kind == "files":
        rel = str(path or "").strip()
        if not rel:
            return "error: files section requires path"
        try:
            rel = _rel_path(workspace, rel)
        except WorkspacePathError as exc:
            return f"error: {exc}"
        digest = _disk_sha(workspace, rel)
        if digest is None:
            return f"error: missing file: {rel}"

        def apply_file(data: dict) -> None:
            files = data.setdefault("files", {})
            entry = dict(files.get(rel) or {})
            entry["note"] = text
            entry["note_sha"] = digest
            entry["seen_sha"] = digest
            entry["action"] = entry.get("action") or "read"
            entry["updated_at"] = _now()
            files[rel] = entry

        path_obj = _file(workspace)
        with _LOCK:
            data = _load_unlocked(path_obj)
            apply_file(data)
            _prune(data)
            _save_unlocked(path_obj, data)
        return "ok"
    if kind not in SECTIONS:
        return (
            "error: section must be files, engineering, product, cicd, or other"
        )

    def apply_section(data: dict) -> None:
        items = list(data.get(kind) or [])
        items.append({"text": text, "updated_at": _now()})
        data[kind] = items

    path_obj = _file(workspace)
    with _LOCK:
        data = _load_unlocked(path_obj)
        apply_section(data)
        _prune(data)
        _save_unlocked(path_obj, data)
    return "ok"


def render_memory(workspace: Path, cap: int = RENDER_CAP) -> str:
    path_obj = _file(workspace)
    with _LOCK:
        data = _load_unlocked(path_obj)
        if _drop_missing(workspace, data):
            _prune(data)
            _save_unlocked(path_obj, data)
        text = _render(workspace, data, cap)
    return text


def _short(sha: str) -> str:
    return (sha or "")[:SHA_SHORT]


def _render(workspace: Path, data: dict, cap: int) -> str:
    blocks = []
    for section in SECTIONS:
        items = list(data.get(section) or [])
        if not items:
            continue
        lines = [f"### {_SECTION_HEADINGS[section]}"]
        for item in reversed(items):
            text = _clip(str(item.get("text") or ""))
            if text:
                lines.append(f"- {text}")
        if len(lines) > 1:
            blocks.append("\n".join(lines))

    files = data.get("files") or {}
    noted = []
    touches = []
    for rel, entry in files.items():
        if not isinstance(entry, dict):
            continue
        note = str(entry.get("note") or "").strip()
        disk = _disk_sha(workspace, rel)
        if disk is None:
            continue
        stamp = str(entry.get("updated_at") or "")
        if note:
            noted.append((stamp, rel, entry, disk, note))
        else:
            touches.append((stamp, rel, entry, disk))
    noted.sort(key=lambda item: item[0], reverse=True)
    touches.sort(key=lambda item: item[0], reverse=True)

    file_lines = []
    for _stamp, rel, entry, disk, note in noted:
        note_sha = str(entry.get("note_sha") or "")
        stale = bool(note_sha) and note_sha != disk
        flag = "STALE" if stale else "fresh"
        action = entry.get("action") or "read"
        sha_bit = _short(note_sha or disk)
        if stale:
            sha_bit = f"{_short(note_sha)}≠disk"
        file_lines.append(f"- {rel} [{flag}] sha={sha_bit} {action}  {_clip(note)}")
    if file_lines:
        blocks.append("### Files\n" + "\n".join(file_lines))

    touch_lines = []
    for _stamp, rel, entry, disk in touches[:RENDER_TOUCH_ONLY]:
        action = entry.get("action") or "read"
        sha_bit = _short(str(entry.get("seen_sha") or disk))
        touch_lines.append(f"- {rel} [seen] sha={sha_bit} {action}")
    if touch_lines:
        blocks.append("### Recently touched\n" + "\n".join(touch_lines))

    if not blocks:
        return ""
    text = "## Workspace memory\n" + "\n".join(blocks)
    if len(text) <= cap:
        return text
    trimmed = text[: cap - 16].rstrip()
    return trimmed + "\n... (truncated)"
