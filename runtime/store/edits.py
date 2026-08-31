from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS edits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  batch_id TEXT,
  path TEXT NOT NULL,
  tool TEXT NOT NULL,
  before BLOB,
  after BLOB,
  before_sha TEXT,
  after_sha TEXT,
  diff TEXT,
  created_dirs TEXT,
  applied_at TEXT NOT NULL
);
"""


@dataclass
class EditRecord:
    id: int
    session_id: str
    batch_id: str | None
    path: str
    tool: str
    before: bytes | None
    after: bytes | None
    before_sha: str | None
    after_sha: str | None
    diff: str
    created_dirs: list[str]
    applied_at: str


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(_SCHEMA)
    return conn


def ensure_schema(path: Path) -> None:
    conn = _connect(path)
    conn.close()


def record(
    db_path: Path,
    *,
    session_id: str,
    batch_id: str | None,
    path: str,
    tool: str,
    before: bytes | None,
    after: bytes | None,
    before_sha: str | None,
    after_sha: str | None,
    diff: str,
    created_dirs: list[str] | None = None,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    dirs = json.dumps(created_dirs or [])
    conn = _connect(db_path)
    try:
        cursor = conn.execute(
            """
            INSERT INTO edits (
              session_id, batch_id, path, tool, before, after,
              before_sha, after_sha, diff, created_dirs, applied_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                batch_id,
                path,
                tool,
                before,
                after,
                before_sha,
                after_sha,
                diff,
                dirs,
                now,
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def recent(db_path: Path, session_id: str, limit: int = 20) -> list[EditRecord]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT id, session_id, batch_id, path, tool, before, after,
                   before_sha, after_sha, diff, created_dirs, applied_at
            FROM edits
            WHERE session_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (session_id, limit),
        ).fetchall()
    finally:
        conn.close()
    return [_row(row) for row in rows]


def last_batch(db_path: Path, session_id: str) -> list[EditRecord]:
    conn = _connect(db_path)
    try:
        head = conn.execute(
            """
            SELECT batch_id, id FROM edits
            WHERE session_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        if head is None:
            return []
        batch_id, edit_id = head
        if batch_id:
            rows = conn.execute(
                """
                SELECT id, session_id, batch_id, path, tool, before, after,
                       before_sha, after_sha, diff, created_dirs, applied_at
                FROM edits
                WHERE session_id = ? AND batch_id = ?
                ORDER BY id ASC
                """,
                (session_id, batch_id),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, session_id, batch_id, path, tool, before, after,
                       before_sha, after_sha, diff, created_dirs, applied_at
                FROM edits
                WHERE id = ?
                """,
                (edit_id,),
            ).fetchall()
    finally:
        conn.close()
    return [_row(row) for row in rows]


def _row(row: tuple) -> EditRecord:
    dirs_raw = row[10]
    try:
        created = json.loads(dirs_raw) if dirs_raw else []
    except json.JSONDecodeError:
        created = []
    if not isinstance(created, list):
        created = []
    return EditRecord(
        id=row[0],
        session_id=row[1],
        batch_id=row[2],
        path=row[3],
        tool=row[4],
        before=row[5],
        after=row[6],
        before_sha=row[7],
        after_sha=row[8],
        diff=row[9] or "",
        created_dirs=[str(item) for item in created],
        applied_at=row[11],
    )
