#!/usr/bin/env python3
"""Report write-gate flag counts per heuristic, per run.

Four coders in the PR trials collected 10, 11, 14 and 16 advisory write-gate
flags. None was acted on and the independent reviews rated them false
positives -- but nothing recorded *which* heuristic fired, so "mostly false
positives" stayed an impression instead of a number. Every judgement is now
journalled with its per-signal scores and, for a write gate, the `edit_id` of
the edit it judged, so a flag can be joined with what that edit turned out to
be.

Usage:
    python3 scripts/judge_precision.py [--db PATH] [--session ID] [--tag TAG]
    python3 scripts/judge_precision.py --json

The default database is `.engine/session.db` under the current workspace.
`--edits` additionally lists each write-gate flag with the path and tool of
the edit it was about, which is the join a human needs to label precision.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runtime.judge_decisions import (  # noqa: E402
    WRITE_DELETES_UNRELATED_FLAG,
    WRITE_DISABLES_CHECK_FLAG,
    WRITE_MATCHES_INTENT_FLAG,
    WRITE_SCOPE_CREEP_FLAG,
    WRITE_SECRET_BLOCK,
)
from runtime.store import edits as edit_journal  # noqa: E402
from runtime.store.judgements import all_for_session  # noqa: E402

DEFAULT_DB = Path(".engine") / "session.db"

# Which signal tripped, using the same thresholds classify_write applies, so
# the report cannot drift away from what the gate actually did.
_HEURISTICS = (
    ("introduces_hardcoded_secret", lambda v: v > WRITE_SECRET_BLOCK),
    ("disables_a_test_or_check", lambda v: v > WRITE_DISABLES_CHECK_FLAG),
    ("deletes_unrelated_code", lambda v: v > WRITE_DELETES_UNRELATED_FLAG),
    ("scope_creep", lambda v: v >= WRITE_SCOPE_CREEP_FLAG),
    ("matches_stated_intent", lambda v: v < WRITE_MATCHES_INTENT_FLAG),
)


def tripped(signals: dict) -> list[str]:
    """Heuristics whose recorded score crosses its own flag threshold."""
    out = []
    for name, over in _HEURISTICS:
        value = signals.get(name)
        if value is None:
            continue
        try:
            if over(float(value)):
                out.append(name)
        except (TypeError, ValueError):
            continue
    return out


def report(db: Path, *, session: str = "", tag: str = "write_gate") -> dict:
    rows = [item for item in all_for_session(db, session) if not tag or item.tag == tag]
    per_run: dict[str, Counter] = defaultdict(Counter)
    per_run_totals: Counter = Counter()
    per_agent: Counter = Counter()
    outcomes: Counter = Counter()
    unattributed = 0
    for item in rows:
        run = item.session_id or "(no session)"
        per_run_totals[run] += 1
        outcomes[item.outcome] += 1
        if item.agent_id:
            per_agent[item.agent_id] += 1
        names = tripped(item.signals)
        if not names:
            unattributed += 1
        for name in names:
            per_run[run][name] += 1
    return {
        "database": str(db),
        "tag": tag or "(all)",
        "total": len(rows),
        "with_edit_id": sum(1 for item in rows if item.edit_id is not None),
        "outcomes": dict(outcomes),
        "flags_per_run": {run: dict(counts) for run, counts in per_run.items()},
        "decisions_per_run": dict(per_run_totals),
        "decisions_per_agent": dict(per_agent),
        "no_heuristic_over_threshold": unattributed,
    }


def edit_rows(db: Path, *, session: str = "", tag: str = "write_gate") -> list[dict]:
    """Each flag joined to the edit it judged, for hand-labelling precision."""
    judgements = [
        item for item in all_for_session(db, session) if not tag or item.tag == tag
    ]
    wanted = {item.edit_id for item in judgements if item.edit_id is not None}
    by_id: dict[int, object] = {}
    if wanted:
        sessions = {item.session_id for item in judgements}
        for sid in sessions:
            for rec in edit_journal.recent(db, sid, limit=10_000):
                if rec.id in wanted:
                    by_id[rec.id] = rec
    out = []
    for item in judgements:
        rec = by_id.get(item.edit_id) if item.edit_id is not None else None
        out.append(
            {
                "judgement_id": item.id,
                "session_id": item.session_id,
                "agent_id": item.agent_id,
                "outcome": item.outcome,
                "enforced": item.enforced,
                "heuristics": tripped(item.signals),
                "signals": item.signals,
                "edit_id": item.edit_id,
                "edit_path": getattr(rec, "path", None),
                "edit_tool": getattr(rec, "tool", None),
            }
        )
    return out


def _print(summary: dict, rows: list[dict] | None) -> None:
    print(f"database: {summary['database']}")
    print(f"tag: {summary['tag']}")
    print(f"decisions: {summary['total']}  (with edit_id: {summary['with_edit_id']})")
    if summary["outcomes"]:
        joined = ", ".join(f"{k}={v}" for k, v in sorted(summary["outcomes"].items()))
        print(f"outcomes: {joined}")
    print()
    print("flags per heuristic per run:")
    if not summary["flags_per_run"]:
        print("  (none)")
    for run, counts in summary["flags_per_run"].items():
        total = summary["decisions_per_run"].get(run, 0)
        print(f"  {run}  ({total} decision(s))")
        for name, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"    {name:32s} {count}")
    if summary["no_heuristic_over_threshold"]:
        print(
            f"\n{summary['no_heuristic_over_threshold']} decision(s) recorded no "
            "signal over its threshold (older rows, or a signal set that changed)"
        )
    if summary["decisions_per_agent"]:
        print("\ndecisions per agent:")
        for agent, count in sorted(
            summary["decisions_per_agent"].items(), key=lambda kv: -kv[1]
        ):
            print(f"  {agent[:8]}  {count}")
    if rows is not None:
        print("\nflagged edits:")
        for row in rows:
            names = ",".join(row["heuristics"]) or "-"
            path = row["edit_path"] or "(no edit recorded)"
            print(
                f"  #{row['judgement_id']:<5} {row['outcome']:<6} {names:<40} "
                f"edit={row['edit_id']} {path}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB), help="session database path")
    parser.add_argument("--session", default="", help="limit to one session id")
    parser.add_argument(
        "--tag", default="write_gate", help="judge call site (empty for all)"
    )
    parser.add_argument(
        "--edits", action="store_true", help="also list each flag's edit"
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    db = Path(args.db)
    if not db.is_file():
        print(f"no such database: {db}", file=sys.stderr)
        return 1
    summary = report(db, session=args.session, tag=args.tag)
    rows = edit_rows(db, session=args.session, tag=args.tag) if args.edits else None
    if args.json:
        payload = dict(summary)
        if rows is not None:
            payload["flagged_edits"] = rows
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    _print(summary, rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
