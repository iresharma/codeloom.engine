"""Calibration fixture for the read-path resolver.

Hits the real TypeSafe API — skipped unless TYPESAFE_API_KEY is set.
Run with `pytest -m judge tests/live/test_locate_live.py` when tuning
RESOLVER_* floors in runtime/judge_decisions.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.resolver import resolve_locate
from llm.openrouter import load_env_sh
from runtime.config import EngineConfig, typesafe_api_key_from_env
from runtime.judge import JudgeManager

load_env_sh(Path(__file__).resolve().parents[2] / "env.sh")
_KEY = typesafe_api_key_from_env()

pytestmark = [
    pytest.mark.judge,
    pytest.mark.skipif(
        not _KEY,
        reason="TYPESAFE_API_KEY / TYPESAFE_JEV_API_KEY is not set",
    ),
    pytest.mark.skipif(
        __import__("shutil").which("rg") is None,
        reason="rg (ripgrep) is not installed",
    ),
]


def _plant(workspace: Path) -> None:
    (workspace / "retry.py").write_text(
        "\n".join(f"# line {i}" for i in range(20))
        + "\ndef retry_logic():\n    return backoff()\n",
        encoding="utf-8",
    )
    (workspace / "auth.py").write_text(
        "def authenticate(user, password):\n    return token_for(user)\n",
        encoding="utf-8",
    )
    (workspace / "notes.md").write_text(
        "retry backoff retry backoff retry backoff retry\n" * 4,
        encoding="utf-8",
    )


CASES = [
    "where is the retry logic?",
    "find retry_logic in this codebase",
    "where is authenticate defined?",
    "how does retry backoff work?",
    "locate the authenticate function",
    "where is token_for used?",
    "find the retry helper",
    "where does authenticate live?",
    "show me retry_logic",
    "where is the auth entry point?",
]


@pytest.mark.asyncio
async def test_locate_resolution_rate(tmp_path):
    _plant(tmp_path)
    config = EngineConfig(typesafe_api_key=_KEY, judge_mode="enforcing")
    judge = JudgeManager(config)
    assert judge.enabled

    resolved = 0
    complete = 0
    for message in CASES:
        result = await resolve_locate(tmp_path, judge, message)
        if result is not None:
            resolved += 1
            if result.complete:
                complete += 1

    await judge.aclose()

    # Conservative: the resolver is allowed to fall through often.
    # A total miss on every planted locate question is calibration drift.
    assert resolved >= 3, f"resolved {resolved}/{len(CASES)} (complete={complete})"
