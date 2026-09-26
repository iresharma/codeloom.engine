from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS judgements (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT,
  tag TEXT NOT NULL,
  subject TEXT,
  outcome TEXT,
  signals TEXT,
  enforced INTEGER,
  latency_ms INTEGER,
  agent_id TEXT,
  created_at TEXT NOT NULL,
  edit_id INTEGER
);
"""
# `edit_id` arrived after the first schema shipped, so an existing database
# needs it added rather than recreated.
_MIGRATIONS = (("edit_id", "ALTER TABLE judgements ADD COLUMN edit_id INTEGER"),)


@dataclass
class JudgementRecord:
    id: int
    session_id: str
    tag: str
    subject: str
    outcome: str
    signals: dict
    enforced: bool
    latency_ms: int
    agent_id: str
    created_at: str
    # The edits journal row this judgement was about (write_gate only), so a
    # flag can be joined with what the edit actually turned out to be.
    edit_id: int | None = None


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(_SCHEMA)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(judgements)")}
    for column, statement in _MIGRATIONS:
        if column not in existing:
            conn.execute(statement)
            conn.commit()
    return conn


def ensure_schema(path: Path) -> None:
    conn = _connect(path)
    conn.close()


def record(
    db_path: Path,
    *,
    session_id: str,
    tag: str,
    subject: str,
    outcome: str,
    signals: dict | None = None,
    enforced: bool = False,
    latency_ms: int = 0,
    agent_id: str = "",
    edit_id: int | None = None,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect(db_path)
    try:
        cursor = conn.execute(
            """
            INSERT INTO judgements (
              session_id, tag, subject, outcome, signals,
              enforced, latency_ms, agent_id, created_at, edit_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                tag,
                subject,
                outcome,
                json.dumps(signals or {}),
                1 if enforced else 0,
                int(latency_ms or 0),
                agent_id or "",
                now,
                int(edit_id) if edit_id is not None else None,
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def recent(db_path: Path, *, limit: int = 50) -> list[JudgementRecord]:
    if not Path(db_path).is_file():
        return []
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT id, session_id, tag, subject, outcome, signals,
                   enforced, latency_ms, agent_id, created_at, edit_id
            FROM judgements
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for row in rows:
        try:
            signals = json.loads(row[5] or "{}")
        except json.JSONDecodeError:
            signals = {}
        if not isinstance(signals, dict):
            signals = {}
        out.append(
            JudgementRecord(
                id=row[0],
                session_id=row[1] or "",
                tag=row[2] or "",
                subject=row[3] or "",
                outcome=row[4] or "",
                signals=signals,
                enforced=bool(row[6]),
                latency_ms=int(row[7] or 0),
                agent_id=row[8] or "",
                created_at=row[9] or "",
                edit_id=row[10],
            )
        )
    return out


def all_for_session(db_path: Path, session_id: str = "") -> list[JudgementRecord]:
    """Every judgement, oldest first, optionally for one session.

    The precision report (scripts/judge_precision.py) reads this; `recent`
    is capped and newest-first, which is wrong for a per-run tally.
    """
    if not Path(db_path).is_file():
        return []
    conn = _connect(db_path)
    try:
        sql = """
            SELECT id, session_id, tag, subject, outcome, signals,
                   enforced, latency_ms, agent_id, created_at, edit_id
            FROM judgements
        """
        params: tuple = ()
        if session_id:
            sql += " WHERE session_id = ?"
            params = (session_id,)
        sql += " ORDER BY id ASC"
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    out = []
    for row in rows:
        try:
            signals = json.loads(row[5] or "{}")
        except json.JSONDecodeError:
            signals = {}
        if not isinstance(signals, dict):
            signals = {}
        out.append(
            JudgementRecord(
                id=row[0],
                session_id=row[1] or "",
                tag=row[2] or "",
                subject=row[3] or "",
                outcome=row[4] or "",
                signals=signals,
                enforced=bool(row[6]),
                latency_ms=int(row[7] or 0),
                agent_id=row[8] or "",
                created_at=row[9] or "",
                edit_id=row[10],
            )
        )
    return out
