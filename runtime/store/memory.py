from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from runtime.tools.fileid import sha256_bytes
from runtime.tools.fs import WorkspacePathError, relative_posix, resolve_in_workspace

SECTIONS = ("engineering", "product", "cicd", "other")
SECTION_CAP = 12
FILE_NOTE_CAP = 40
RENDER_CAP = 8000
NOTE_CLIP = 400
FIELD_CLIP = 200
SHA_SHORT = 7
INGEST_FILE_CAP = 8
INGEST_PROFILES = frozenset({"ask", "coder", "researcher"})
INGEST_STATUSES = frozenset({"ok", "max_turns"})
_FILE_FIELDS = ("purpose", "entry_points", "constraints", "note")
_LABELS = ("what", "paths", "facts", "verdict", "leftover")

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


def _has_note(entry: dict) -> bool:
    return any(str(entry.get(key) or "").strip() for key in _FILE_FIELDS)


def _note_is_fresh(entry, digest: str) -> bool:
    if not isinstance(entry, dict) or not _has_note(entry):
        return False
    note_sha = str(entry.get("note_sha") or "")
    return bool(note_sha) and note_sha == digest


def _prune(data: dict) -> None:
    files = data.get("files") or {}
    noted = []
    for path, entry in files.items():
        if isinstance(entry, dict) and _has_note(entry):
            noted.append((path, entry))
    noted.sort(key=lambda item: item[1].get("updated_at") or "", reverse=True)
    data["files"] = dict(noted[:FILE_NOTE_CAP])
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

    def apply(data: dict) -> bool:
        files = data.setdefault("files", {})
        entry = files.get(rel)
        if not isinstance(entry, dict) or not _has_note(entry):
            return False
        updated = dict(entry)
        updated["seen_sha"] = digest
        updated["action"] = kind
        updated["updated_at"] = _now()
        files[rel] = updated
        return True

    path_obj = _file(workspace)
    with _LOCK:
        data = _load_unlocked(path_obj)
        had_file = path_obj.is_file()
        before = set((data.get("files") or {}).keys())
        changed = apply(data)
        _prune(data)
        after = set((data.get("files") or {}).keys())
        if changed or (had_file and before != after):
            _save_unlocked(path_obj, data)


def remember(
    workspace: Path,
    section: str,
    note: str = "",
    path: str = "",
    purpose: str = "",
    entry_points: str = "",
    constraints: str = "",
) -> str:
    kind = (section or "").strip().lower()
    text = _clip(note)
    purpose = _clip(purpose, FIELD_CLIP)
    entry_points = _clip(entry_points, FIELD_CLIP)
    constraints = _clip(constraints, FIELD_CLIP)
    if kind == "files":
        rel = str(path or "").strip()
        if not rel:
            return "error: files section requires path"
        if not (text or purpose or entry_points or constraints):
            return "error: note is empty"
        try:
            rel = _rel_path(workspace, rel)
        except WorkspacePathError as exc:
            return f"error: {exc}"
        digest = _disk_sha(workspace, rel)
        if digest is None:
            return f"error: missing file: {rel}"

        def apply_file(data: dict) -> None:
            files = data.setdefault("files", {})
            files[rel] = _file_note_entry(
                files.get(rel),
                digest=digest,
                purpose=purpose,
                entry_points=entry_points,
                constraints=constraints,
                note=text,
                merge=True,
            )

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
    if not text:
        return "error: note is empty"

    def apply_section(data: dict) -> None:
        _append_decision(data, kind, text)

    path_obj = _file(workspace)
    with _LOCK:
        data = _load_unlocked(path_obj)
        apply_section(data)
        _prune(data)
        _save_unlocked(path_obj, data)
    return "ok"


def ingest_result(
    workspace: Path,
    profile: str,
    result,
    survey_paths: list[str] | None = None,
    store: Path | None = None,
) -> None:
    """Persist a child briefing into memory. No extra model call.

    ``workspace`` is the child's tree (hashes / path resolve). ``store`` is
    where ``memory.json`` lives — the main workspace so orch can render it.
    Fresh file notes (note_sha matches disk in the child tree) are left
    alone. Empty or STALE notes are rewritten from the briefing.
    """
    name = str(profile or "").strip().lower()
    if name not in INGEST_PROFILES:
        return
    status = str(getattr(result, "status", "") or "")
    if status not in INGEST_STATUSES:
        return
    fields = _merge_labels(result)
    if not fields.get("what") and not fields.get("verdict"):
        return
    persist = Path(store or workspace)
    hash_root = Path(workspace)
    leftover = list(getattr(result, "leftover_questions", None) or [])
    constraints = _clip(
        "; ".join(leftover) or fields.get("leftover") or "",
        FIELD_CLIP,
    )
    purpose = _clip(fields.get("what") or "", FIELD_CLIP)
    facts = fields.get("facts") or ""
    verdict = fields.get("verdict") or fields.get("what") or ""
    decision = verdict
    if facts and facts not in decision:
        decision = _clip(f"{verdict} {facts}".strip() if verdict else facts)
    else:
        decision = _clip(decision)
    section = "other" if name == "researcher" else "engineering"
    paths = _ingest_paths(name, result, survey_paths, fields.get("paths") or "")

    path_obj = _file(persist)
    with _LOCK:
        data = _load_unlocked(path_obj)
        if decision:
            _append_decision(data, section, decision)
        files = data.setdefault("files", {})
        for raw in paths:
            rel, digest = _resolve_existing(hash_root, raw)
            if rel is None or digest is None:
                continue
            existing = files.get(rel)
            if _note_is_fresh(existing, digest):
                continue
            files[rel] = _file_note_entry(
                existing,
                digest=digest,
                purpose=purpose,
                entry_points=_entry_for_path(rel, facts),
                constraints=constraints,
            )
        _prune(data)
        _save_unlocked(path_obj, data)


def render_memory(workspace: Path, cap: int = RENDER_CAP) -> str:
    path_obj = _file(workspace)
    with _LOCK:
        data = _load_unlocked(path_obj)
        before = set((data.get("files") or {}).keys())
        missing = _drop_missing(workspace, data)
        _prune(data)
        after = set((data.get("files") or {}).keys())
        if path_obj.is_file() and (missing or before != after):
            _save_unlocked(path_obj, data)
        text = _render(workspace, data, cap)
    return text


def dump_for_client(workspace: Path) -> dict:
    path_obj = _file(workspace)
    with _LOCK:
        data = _load_unlocked(path_obj)
        _drop_missing(workspace, data)
        _prune(data)
        snapshot = {
            "files": dict(data.get("files") or {}),
            **{section: list(data.get(section) or []) for section in SECTIONS},
        }
    files = []
    for rel, entry in snapshot["files"].items():
        if not isinstance(entry, dict) or not _has_note(entry):
            continue
        disk = _disk_sha(workspace, rel)
        note_sha = str(entry.get("note_sha") or "")
        stale = bool(note_sha) and disk is not None and note_sha != disk
        purpose, entry_points, constraints = _file_blurb(entry)
        files.append(
            {
                "path": rel,
                "purpose": purpose,
                "entry_points": entry_points,
                "constraints": constraints,
                "note": str(entry.get("note") or ""),
                "stale": stale,
                "action": str(entry.get("action") or ""),
                "updated_at": str(entry.get("updated_at") or ""),
            }
        )
    out: dict = {"files": files}
    for section in SECTIONS:
        out[section] = [
            {
                "text": str(item.get("text") or ""),
                "updated_at": str(item.get("updated_at") or ""),
            }
            for item in snapshot[section]
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        ]
    return out


def _file_note_entry(
    existing,
    *,
    digest: str,
    purpose: str = "",
    entry_points: str = "",
    constraints: str = "",
    note: str = "",
    action: str = "",
    merge: bool = False,
) -> dict:
    entry = dict(existing) if isinstance(existing, dict) else {}
    purpose = _clip(purpose, FIELD_CLIP)
    entry_points = _clip(entry_points, FIELD_CLIP)
    constraints = _clip(constraints, FIELD_CLIP)
    note = _clip(note)
    if not purpose and note:
        purpose = _clip(note, FIELD_CLIP)
    if merge:
        if purpose:
            entry["purpose"] = purpose
        if entry_points:
            entry["entry_points"] = entry_points
        if constraints:
            entry["constraints"] = constraints
        if note:
            entry["note"] = note
        entry.setdefault("purpose", "")
        entry.setdefault("entry_points", "")
        entry.setdefault("constraints", "")
        entry.setdefault("note", "")
    else:
        entry["purpose"] = purpose
        entry["entry_points"] = entry_points
        entry["constraints"] = constraints
        if note:
            entry["note"] = note
        else:
            entry["note"] = str(entry.get("note") or "")
    entry["note_sha"] = digest
    entry["seen_sha"] = digest
    entry["action"] = action or entry.get("action") or "read"
    entry["updated_at"] = _now()
    return entry


def _append_decision(data: dict, section: str, text: str) -> None:
    text = _clip(text)
    if not text:
        return
    items = list(data.get(section) or [])
    if items:
        last = str(items[-1].get("text") or "").strip().casefold()
        if last == text.casefold():
            return
    items.append({"text": text, "updated_at": _now()})
    data[section] = items


def _labeled_fields(text: str) -> dict[str, str]:
    chunks: dict[str, list[str]] = {key: [] for key in _LABELS}
    current = None
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        key = None
        rest = stripped
        if ":" in stripped:
            raw_key, rest = stripped.split(":", 1)
            normalized = raw_key.strip().lower().replace(" ", "")
            if normalized in {"leftoverquestions", "leftover_questions"}:
                normalized = "leftover"
            if normalized in chunks:
                key = normalized
        if key:
            current = key
            rest = rest.strip()
            if rest:
                chunks[current].append(rest)
        elif current:
            chunks[current].append(stripped)
    return {key: " ".join(parts).strip() for key, parts in chunks.items() if parts}


def _merge_labels(result) -> dict[str, str]:
    fields = _labeled_fields(str(getattr(result, "outcome", "") or ""))
    extra = _labeled_fields(str(getattr(result, "summary", "") or ""))
    for key, value in extra.items():
        if key not in fields:
            fields[key] = value
    return fields


def _split_paths(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for raw in re.split(r"[,;\n]", text or ""):
        item = raw.strip().strip("`").strip()
        if not item:
            continue
        token = item.split()[0].rstrip(")")
        if token in seen:
            continue
        seen.add(token)
        found.append(token)
    return found


def _ingest_paths(
    profile: str,
    result,
    survey_paths: list[str] | None,
    briefing_paths: str,
) -> list[str]:
    if profile == "coder":
        raw_items = [str(item) for item in (getattr(result, "files_touched", None) or [])]
    else:
        raw_items = _split_paths(briefing_paths) + list(survey_paths or [])
    ordered: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        ordered.append(text)
    return ordered[:INGEST_FILE_CAP]


def _resolve_existing(workspace: Path, path: str) -> tuple[str | None, str | None]:
    raw = str(path or "").strip()
    if not raw:
        return None, None
    try:
        rel = _rel_path(workspace, raw)
    except WorkspacePathError:
        return None, None
    digest = _disk_sha(workspace, rel)
    if digest is None:
        return None, None
    return rel, digest


def _entry_for_path(rel: str, facts: str) -> str:
    bits = [rel]
    name = Path(rel).name
    for part in re.split(r"[;\n]|\.\s+", facts or ""):
        fragment = part.strip()
        if fragment and name in fragment:
            bits.append(_clip(fragment, FIELD_CLIP))
            break
    return _clip(", ".join(bits), FIELD_CLIP)


def _short(sha: str) -> str:
    return (sha or "")[:SHA_SHORT]


def _file_blurb(entry: dict) -> tuple[str, str, str]:
    purpose = str(entry.get("purpose") or "").strip()
    entry_points = str(entry.get("entry_points") or "").strip()
    constraints = str(entry.get("constraints") or "").strip()
    note = str(entry.get("note") or "").strip()
    if not purpose and note:
        purpose = note
    return purpose, entry_points, constraints


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
    for rel, entry in files.items():
        if not isinstance(entry, dict) or not _has_note(entry):
            continue
        disk = _disk_sha(workspace, rel)
        if disk is None:
            continue
        stamp = str(entry.get("updated_at") or "")
        noted.append((stamp, rel, entry, disk))
    noted.sort(key=lambda item: item[0], reverse=True)

    file_lines = []
    for _stamp, rel, entry, disk in noted:
        note_sha = str(entry.get("note_sha") or "")
        stale = bool(note_sha) and note_sha != disk
        flag = "STALE" if stale else "fresh"
        action = entry.get("action") or "read"
        sha_bit = _short(note_sha or disk)
        if stale:
            sha_bit = f"{_short(note_sha)}≠disk"
        file_lines.append(f"- {rel} [{flag}] sha={sha_bit} {action}")
        purpose, entry_points, constraints = _file_blurb(entry)
        if purpose:
            file_lines.append(f"  purpose: {_clip(purpose, FIELD_CLIP)}")
        if entry_points:
            file_lines.append(f"  entry: {_clip(entry_points, FIELD_CLIP)}")
        if constraints:
            file_lines.append(f"  constraints: {_clip(constraints, FIELD_CLIP)}")
    if file_lines:
        blocks.append("### Files\n" + "\n".join(file_lines))

    if not blocks:
        return ""
    text = "## Workspace memory\n" + "\n".join(blocks)
    if len(text) <= cap:
        return text
    trimmed = text[: cap - 16].rstrip()
    return trimmed + "\n... (truncated)"
