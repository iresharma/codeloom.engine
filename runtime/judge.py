"""TypeSafe judge integration.

Engine infrastructure, not an LLM-facing tool — the model must never be able
to invoke this directly (see docs/impl-plans/jev-exp-1.md). Modeled on
`runtime.tools.lsp.LSPManager`: one instance owned by the session, built at
bind time, closed on shutdown.

Every failure mode — missing API key, missing `typesafe-sdk` package,
timeout, rate limit, connection error, malformed response — degrades to
`ask()` returning `None`. Callers treat `None` as "no opinion" and fall back
to their pre-existing, judge-less code path. A judge failure must never
propagate into a tool result, a protocol event, or an exception the agent
loop sees (invariant 3 in the plan).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import OrderedDict
from typing import Any

from runtime.config import EngineConfig

try:
    from typesafe_sdk import (
        AsyncTypeSafeClient,
        Choice,
        Noul,
        RetryPolicy,
        Score,
        TypeSafeError,
    )

    _SDK_IMPORT_ERROR: Exception | None = None
except ImportError as exc:  # pragma: no cover - exercised only without the package
    AsyncTypeSafeClient = None  # type: ignore[assignment,misc]
    Choice = None  # type: ignore[assignment,misc]
    Noul = None  # type: ignore[assignment,misc]
    RetryPolicy = None  # type: ignore[assignment,misc]
    Score = None  # type: ignore[assignment,misc]
    TypeSafeError = Exception  # type: ignore[assignment,misc]
    _SDK_IMPORT_ERROR = exc

__all__ = ["Choice", "JudgeManager", "Noul", "Score", "Verdict"]

logger = logging.getLogger("engine.judge")


class Verdict:
    """Thin wrapper over a TypeSafe `SystemOneResponse`.

    Every accessor takes a default so a missing answer (a question that was
    never asked, or dropped by a partial response) never raises.
    """

    __slots__ = ("_response", "cache_hit", "latency_ms")

    def __init__(self, response: Any, *, latency_ms: int, cache_hit: bool = False):
        self._response = response
        self.latency_ms = latency_ms
        self.cache_hit = cache_hit

    def noul(self, key: str, default: float = 0.0) -> float:
        answer = self._response.nouls.get(key)
        return answer.noul if answer is not None else default

    def choice(self, key: str, default: str = "") -> str:
        answer = self._response.choices.get(key)
        return answer.choice if answer is not None else default

    def score(self, key: str, default: float = 0.0) -> float:
        answer = self._response.scores.get(key)
        return answer.score if answer is not None else default

    def confidence(self, key: str, default: float = 0.0) -> float:
        # Noul answers carry no confidence field in the SDK; Choice/Score do.
        answer = self._response.answers.get(key)
        if answer is None:
            return default
        return getattr(answer, "confidence", default)

    def probabilities(self, key: str) -> dict[str, float]:
        """Per-label probabilities from a Choice answer -- the ranking a
        speculative-fan-out Choice-over-candidates question is built on."""
        answer = self._response.choices.get(key)
        if answer is None:
            return {}
        return dict(getattr(answer, "probabilities", {}) or {})

    @property
    def usage(self) -> Any:
        return getattr(self._response, "usage", None)


class JudgeManager:
    def __init__(self, config: EngineConfig, *, on_failure=None, on_request=None):
        self._model = config.judge_model
        self._timeout_s = max(0.05, config.judge_timeout_ms / 1000.0)
        self._cache_size = max(1, config.judge_cache_size)
        self._cache: OrderedDict[str, Any] = OrderedDict()
        self._on_failure = on_failure
        self._on_request = on_request
        self._failure_emitted = False
        self.calls = 0
        self.cache_hits = 0
        self.failures = 0
        self._client: Any = None
        self._api_key = ""
        self._usable = False
        if _SDK_IMPORT_ERROR is not None:
            if config.judge_usable:
                logger.warning(
                    "typesafe-sdk is not installed; the judge is disabled (%s)",
                    _SDK_IMPORT_ERROR,
                )
            return
        if not config.judge_usable:
            return
        # Open the HTTP client on first ask(), not at construction. Tests
        # create many EngineSessions; an eager client per session is how
        # leftover pytest-xdist workers balloon.
        self._api_key = config.typesafe_api_key
        self._usable = True

    @property
    def enabled(self) -> bool:
        return self._usable

    @property
    def stats(self) -> dict[str, int]:
        return {
            "calls": self.calls,
            "cache_hits": self.cache_hits,
            "failures": self.failures,
        }

    async def ask(self, state: Any, questions: dict, *, tag: str) -> Verdict | None:
        """One request, every question in `questions` answered in parallel.

        Returns `None` on any failure (see module docstring); never raises.
        """
        if not self.enabled:
            return None
        self._ensure_client()
        if self._client is None:
            return None
        key = _cache_key(state, questions)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            self.cache_hits += 1
            logger.info("judge tag=%s cache_hit=true", tag)
            verdict = Verdict(cached, latency_ms=0, cache_hit=True)
            self._emit_request(tag, verdict, failed=False)
            return verdict
        self.calls += 1
        started = time.monotonic()
        try:
            response = await self._client.system_one(
                state=state,
                questions=questions,
                model=self._model,
                timeout=self._timeout_s,
            )
        except TypeSafeError as exc:
            self._report_failure(tag, exc)
            self._emit_request(tag, None, failed=True)
            return None
        except Exception as exc:  # noqa: BLE001 - a judge failure must never propagate
            self._report_failure(tag, exc)
            self._emit_request(tag, None, failed=True)
            return None
        latency_ms = int((time.monotonic() - started) * 1000)
        self._cache[key] = response
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        usage = getattr(response, "usage", None)
        logger.info(
            "judge tag=%s cache_hit=false latency_ms=%d input_tokens=%s output_tokens=%s",
            tag,
            latency_ms,
            getattr(usage, "input_tokens", None),
            getattr(usage, "output_tokens", None),
        )
        verdict = Verdict(response, latency_ms=latency_ms, cache_hit=False)
        self._emit_request(tag, verdict, failed=False)
        return verdict

    def _emit_request(self, tag: str, verdict: Verdict | None, failed: bool) -> None:
        if self._on_request is None:
            return
        try:
            self._on_request(tag, verdict, failed)
        except Exception:  # noqa: BLE001 - a callback failure must not escape ask()
            logger.warning("judge on_request callback failed", exc_info=True)

    def _report_failure(self, tag: str, exc: Exception) -> None:
        self.failures += 1
        logger.warning("judge tag=%s failed: %s", tag, exc)
        if self._failure_emitted or self._on_failure is None:
            return
        self._failure_emitted = True
        try:
            self._on_failure(f"judge {tag} failed; falling back to legacy behaviour: {exc}")
        except Exception:  # noqa: BLE001 - a callback failure must not escape ask()
            logger.warning("judge on_failure callback failed", exc_info=True)

    def _ensure_client(self) -> None:
        if self._client is not None or not self._usable:
            return
        assert AsyncTypeSafeClient is not None and RetryPolicy is not None
        self._client = AsyncTypeSafeClient(
            api_key=self._api_key,
            model=self._model,
            retry=RetryPolicy(timeout=self._timeout_s),
            timeout=self._timeout_s,
        )

    async def aclose(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()


def _cache_key(state: Any, questions: dict) -> str:
    payload = {
        "state": state,
        "questions": {name: _question_repr(q) for name, q in questions.items()},
    }
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _question_repr(question: Any) -> Any:
    if isinstance(question, dict):
        return question
    return {
        "type": type(question).__name__,
        "instructions": getattr(question, "instructions", None),
        "criteria": getattr(question, "criteria", None),
    }
