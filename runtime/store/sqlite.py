from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from protocol.snapshot import EngineSnapshot, SessionSummary
from runtime.store.edits import ensure_schema as ensure_edits_schema

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  saved_at TEXT NOT NULL
);
"""


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(_SCHEMA)
    return conn


def init(path: Path) -> None:
    conn = _connect(path)
    conn.close()
    ensure_edits_schema(path)


def load(path: Path, session_id: str) -> EngineSnapshot | None:
    if not path.exists():
        return None
    conn = _connect(path)
    try:
        row = conn.execute(
            "SELECT json FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        return EngineSnapshot.from_json(json.loads(row[0]))
    finally:
        conn.close()


def save(path: Path, snapshot: EngineSnapshot) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect(path)
    try:
        conn.execute(
            """
            INSERT INTO sessions (id, json, created_at, saved_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              json = excluded.json,
              saved_at = excluded.saved_at
            """,
            (snapshot.session_id, json.dumps(_persist_payload(snapshot)), now, now),
        )
        conn.commit()
    finally:
        conn.close()


def _persist_payload(snapshot: EngineSnapshot) -> dict:
    data = snapshot.to_json()
    data.pop("file_tree", None)
    data.pop("git", None)
    data.pop("language", None)
    data.pop("language_supported", None)
    return data


def list_sessions(path: Path) -> list[SessionSummary]:
    if not path.exists():
        return []
    conn = _connect(path)
    try:
        rows = conn.execute(
            "SELECT id, json, saved_at FROM sessions ORDER BY saved_at DESC"
        ).fetchall()
    finally:
        conn.close()
    summaries = []
    for session_id, raw_json, saved_at in rows:
        data = json.loads(raw_json)
        summaries.append(
            SessionSummary(
                id=session_id,
                saved_at=saved_at,
                message_count=len(data.get("messages") or []),
                open_files=list(data.get("open_files") or []),
            )
        )
    return summaries
