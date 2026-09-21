from __future__ import annotations

from runtime.store.judgements import recent, record


def test_record_and_recent_round_trip(tmp_path):
    db = tmp_path / "session.db"
    row_id = record(
        db,
        session_id="s1",
        tag="search_rerank",
        subject="where is retry?",
        outcome="ranked",
        signals={"top_confidence": 0.9},
        enforced=True,
        latency_ms=12,
        agent_id="abc",
    )
    assert row_id >= 1
    rows = recent(db, limit=10)
    assert len(rows) == 1
    item = rows[0]
    assert item.tag == "search_rerank"
    assert item.subject == "where is retry?"
    assert item.outcome == "ranked"
    assert item.signals["top_confidence"] == 0.9
    assert item.enforced is True
    assert item.latency_ms == 12
    assert item.agent_id == "abc"


def test_recent_empty_missing_db(tmp_path):
    assert recent(tmp_path / "missing.db") == []
