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
  created_at TEXT NOT NULL
);
"""


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
    tag: str,
    subject: str,
    outcome: str,
    signals: dict | None = None,
    enforced: bool = False,
    latency_ms: int = 0,
    agent_id: str = "",
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect(db_path)
    try:
        cursor = conn.execute(
            """
            INSERT INTO judgements (
              session_id, tag, subject, outcome, signals,
              enforced, latency_ms, agent_id, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                   enforced, latency_ms, agent_id, created_at
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
            )
        )
    return out
