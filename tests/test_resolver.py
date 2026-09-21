"""Phase 5 (docs/impl-plans/jev-exp-1.md): intent routing and the
read-path resolver, exercised directly against agents/resolver.py."""

from __future__ import annotations

import asyncio
import shutil

import pytest

from agents.resolver import (
    ClassifiedTurn,
    Resolution,
    classify_turn,
    extract_keywords,
    memory_keywords,
    resolve_locate,
)
from tests.conftest import FakeJudge, FakeVerdict

# resolve_locate shells out to real ripgrep (unlike the rest of the offline
# suite, which mocks search). rg is a documented hard requirement (README),
# so CI installs it -- this is defense in depth for any environment that
# doesn't, mirroring the existing gopls skip pattern in test_lsp_write.py.
needs_rg = pytest.mark.skipif(shutil.which("rg") is None, reason="rg (ripgrep) is not installed")

# ---------------------------------------------------------------------
# extract_keywords: pure, deterministic -- TypeSafe never generates the
# search query, so this must never call the judge.
# ---------------------------------------------------------------------


def test_extract_keywords_drops_stopwords_and_short_tokens():
    keywords = extract_keywords("where is the retry logic in this codebase?")
    assert "retry" in keywords
    assert "logic" in keywords
    assert "where" not in keywords
    assert "the" not in keywords
    assert "is" not in keywords


def test_extract_keywords_deduplicates_and_caps_length():
    message = " ".join(f"unique{i}" for i in range(20))
    keywords = extract_keywords(message)
    assert len(keywords) <= 8


def test_extract_keywords_empty_message():
    assert extract_keywords("") == []
    assert extract_keywords("the is a") == []


def test_extract_keywords_unions_extra_aliases():
    keywords = extract_keywords("how does auth work?", extra=["authenticate", "login"])
    assert "auth" in keywords
    assert "authenticate" in keywords


def test_memory_keywords_unions_overlapping_file_notes(tmp_path):
    from runtime.store.memory import remember

    (tmp_path / "auth.py").write_text("def authenticate():\n    pass\n")
    remember(
        tmp_path,
        "files",
        path="auth.py",
        purpose="login authenticate helper",
        entry_points="authenticate",
    )
    extras = memory_keywords(tmp_path, "how does auth work?")
    assert "authenticate" in extras


# ---------------------------------------------------------------------
# classify_turn
# ---------------------------------------------------------------------


def test_classify_turn_disabled_judge_returns_none():
    result = asyncio.run(classify_turn(None, "fix the bug"))
    assert result is None


def test_classify_turn_none_verdict_returns_none():
    judge = FakeJudge()  # no scripted response
    result = asyncio.run(classify_turn(judge, "fix the bug"))
    assert result is None
    assert judge.calls


def test_classify_turn_ambiguous():
    judge = FakeJudge()
    judge.responses["intent_route"] = FakeVerdict(nouls={"is_ambiguous": 0.9})
    result = asyncio.run(classify_turn(judge, "do the thing"))
    assert isinstance(result, ClassifiedTurn)
    assert result.route == "ambiguous"


def test_classify_turn_locate():
    judge = FakeJudge()
    judge.responses["intent_route"] = FakeVerdict(
        choices={"intent": "locate"}, confidences={"intent": 0.9}
    )
    result = asyncio.run(classify_turn(judge, "where is the retry logic?"))
    assert result.route == "locate"


def test_classify_turn_locate_below_confidence_floor_falls_to_default():
    judge = FakeJudge()
    judge.responses["intent_route"] = FakeVerdict(
        choices={"intent": "locate"}, confidences={"intent": 0.5}
    )
    result = asyncio.run(classify_turn(judge, "where is the retry logic?"))
    assert result.route == "default"


def test_classify_turn_edit_multi_file():
    judge = FakeJudge()
    judge.responses["intent_route"] = FakeVerdict(
        choices={"intent": "edit"},
        confidences={"intent": 0.9},
        nouls={"is_multi_file": 0.8},
    )
    result = asyncio.run(classify_turn(judge, "rename this across the repo"))
    assert result.route == "edit_multi_file"


def test_classify_turn_meta_asks_second_question_for_action():
    judge = FakeJudge()
    judge.responses["intent_route"] = FakeVerdict(
        choices={"intent": "meta", "meta_action": "undo"},
        confidences={"intent": 0.95, "meta_action": 0.9},
    )
    result = asyncio.run(classify_turn(judge, "undo that"))
    assert result.route == "meta"
    assert result.meta_action == "undo"
    assert judge.calls[0]["tag"] == "intent_route"
    assert judge.calls[1]["tag"] == "intent_route"  # meta_action sub-call


def test_classify_turn_meta_low_confidence_action_falls_to_default():
    judge = FakeJudge()
    judge.responses["intent_route"] = FakeVerdict(
        choices={"intent": "meta", "meta_action": "undo"},
        confidences={"intent": 0.95, "meta_action": 0.1},
    )
    result = asyncio.run(classify_turn(judge, "undo that"))
    assert result.route == "default"


def test_classify_turn_unremarkable_message_is_default():
    judge = FakeJudge()
    judge.responses["intent_route"] = FakeVerdict(
        choices={"intent": "execute"}, confidences={"intent": 0.9}
    )
    result = asyncio.run(classify_turn(judge, "run the tests"))
    assert result.route == "default"


# ---------------------------------------------------------------------
# resolve_locate
# ---------------------------------------------------------------------


def _seed_workspace(tmp_path):
    (tmp_path / "retry.py").write_text(
        "\n".join(f"# line {i}" for i in range(30))
        + "\ndef retry_logic():\n    return backoff()\n"
    )
    (tmp_path / "unrelated.py").write_text("def totally_unrelated():\n    pass\n")
    (tmp_path / "notes.md").write_text(
        "retry backoff retry backoff retry backoff retry\n" * 5
    )


def test_resolve_locate_disabled_judge_returns_none(tmp_path):
    _seed_workspace(tmp_path)
    result = asyncio.run(resolve_locate(tmp_path, None, "where is the retry logic?"))
    assert result is None


def test_resolve_locate_no_keywords_returns_none(tmp_path):
    judge = FakeJudge()
    result = asyncio.run(resolve_locate(tmp_path, judge, "the is a"))
    assert result is None
    assert judge.calls == []  # never even asked


@needs_rg
def test_resolve_locate_below_candidate_floor_returns_none(tmp_path):
    (tmp_path / "onlyone.py").write_text("def zzzzuniquepattern(): pass\n")
    judge = FakeJudge()
    result = asyncio.run(resolve_locate(tmp_path, judge, "find zzzzuniquepattern"))
    assert result is None
    assert judge.calls == []  # fewer than RESOLVER_CANDIDATE_FLOOR hits


@needs_rg
def test_resolve_locate_none_rerank_verdict_returns_none(tmp_path):
    _seed_workspace(tmp_path)
    judge = FakeJudge()  # no "search_rerank" response scripted
    result = asyncio.run(resolve_locate(tmp_path, judge, "retry backoff logic"))
    assert result is None


@needs_rg
def test_resolve_locate_low_rank_confidence_returns_none(tmp_path):
    _seed_workspace(tmp_path)
    judge = FakeJudge()
    judge.responses["search_rerank"] = FakeVerdict(
        probabilities={"best_match": {"1": 0.5}}, confidences={"best_match": 0.1}
    )
    result = asyncio.run(resolve_locate(tmp_path, judge, "retry backoff logic"))
    assert result is None


@needs_rg
def test_resolve_locate_answers_gate_soft_seeds_insufficient_context(tmp_path):
    _seed_workspace(tmp_path)
    judge = FakeJudge()
    judge.responses["search_rerank"] = FakeVerdict(
        probabilities={"best_match": {"1": 0.9}}, confidences={"best_match": 0.9}
    )
    judge.responses["intent_route"] = FakeVerdict(nouls={"answers_message": 0.1})
    result = asyncio.run(resolve_locate(tmp_path, judge, "retry backoff logic"))
    assert isinstance(result, Resolution)
    assert result.complete is False
    assert result.context
    assert result.trace


@needs_rg
def test_resolve_locate_success_returns_resolution_with_trace(tmp_path):
    _seed_workspace(tmp_path)
    judge = FakeJudge()
    judge.responses["search_rerank"] = FakeVerdict(
        probabilities={"best_match": {"1": 0.9}}, confidences={"best_match": 0.9}
    )
    judge.responses["intent_route"] = FakeVerdict(nouls={"answers_message": 0.9})

    result = asyncio.run(resolve_locate(tmp_path, judge, "retry backoff logic"))

    assert isinstance(result, Resolution)
    assert result.complete is True
    assert result.context
    assert result.trace
    assert result.trace[0].name == "search"
    assert any(call.name == "read_file" for call in result.trace)
