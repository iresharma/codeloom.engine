"""Optional full-fidelity JSONL trace of judge and tool calls.

Off by default (`ENGINE_TRACE_CALLS=1` to enable, wired up by
`scripts/bench_ab.py` for both sides of an A/B run). Written for offline
analysis (`scripts/bench_flow.py`) -- the engine itself never reads this
file back, so a write failure here must never affect a run.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

_FIELD_CAP = 4000  # per string field, so one huge tool result/state blob
_LINE_CAP = 16_000  # whole-record safety net regardless of shape


def _capped(value: Any) -> Any:
    if isinstance(value, str):
        if len(value) > _FIELD_CAP:
            return value[:_FIELD_CAP] + f"…[{len(value) - _FIELD_CAP} more chars]"
        return value
    if isinstance(value, dict):
        return {k: _capped(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_capped(v) for v in value]
    return value


class TraceWriter:
    def __init__(self, path: Path):
        self._path = path

    def write(self, kind: str, **fields: Any) -> None:
        record = {"ts": time.time(), "kind": kind}
        record.update({k: _capped(v) for k, v in fields.items()})
        try:
            line = json.dumps(record, default=str)
        except (TypeError, ValueError):
            return
        if len(line) > _LINE_CAP:
            line = json.dumps(
                {
                    "ts": record["ts"],
                    "kind": kind,
                    "truncated": True,
                    "original_chars": len(line),
                }
            )
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
