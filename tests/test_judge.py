from __future__ import annotations

import asyncio

import pytest

from runtime.config import EngineConfig
from runtime.judge import JudgeManager, Verdict, _cache_key
import runtime.judge as judge_module


def _config(**overrides) -> EngineConfig:
    config = EngineConfig()
    config.typesafe_api_key = overrides.pop("typesafe_api_key", "real-key")
    config.judge_mode = overrides.pop("judge_mode", "advisory")
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def test_disabled_without_api_key():
    manager = JudgeManager(_config(typesafe_api_key=""))
    assert manager.enabled is False
    assert asyncio.run(manager.ask({"x": 1}, {}, tag="t")) is None


def test_disabled_when_judge_off():
    manager = JudgeManager(_config(judge_mode="off"))
    assert manager.enabled is False


def test_enabled_with_key_and_advisory_mode():
    manager = JudgeManager(_config())
    assert manager.enabled is True
    assert manager._client is None  # HTTP client is opened on first ask()


class _FakeAnswer:
    def __init__(self, **fields):
        for key, value in fields.items():
            setattr(self, key, value)


class _FakeUsage:
    def __init__(self, input_tokens=10, output_tokens=5):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeResponse:
    def __init__(self):
        noul_answer = _FakeAnswer(noul=0.9)
        choice_answer = _FakeAnswer(choice="yes", confidence=0.8, probabilities={"yes": 0.8})
        score_answer = _FakeAnswer(score=1.5, confidence=0.7, legend={0: "a"})
        self.nouls = {"is_safe": noul_answer}
        self.choices = {"kind": choice_answer}
        self.scores = {"severity": score_answer}
        self.answers = {
            "is_safe": noul_answer,
            "kind": choice_answer,
            "severity": score_answer,
        }
        self.usage = _FakeUsage()


class _FakeClient:
    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc
        self.calls = 0
        self.closed = False

    async def system_one(self, **kwargs):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return self._response

    async def aclose(self):
        self.closed = True


def _manager_with_fake_client(client) -> JudgeManager:
    manager = JudgeManager(_config())
    manager._client = client
    return manager


def test_verdict_accessors_and_defaults():
    verdict = Verdict(_FakeResponse(), latency_ms=42)
    assert verdict.noul("is_safe") == 0.9
    assert verdict.noul("missing", default=0.25) == 0.25
    assert verdict.choice("kind") == "yes"
    assert verdict.choice("missing", default="n/a") == "n/a"
    assert verdict.score("severity") == 1.5
    assert verdict.score("missing", default=-1.0) == -1.0
    assert verdict.confidence("kind") == 0.8
    assert verdict.confidence("is_safe", default=0.0) == 0.0  # nouls carry no confidence
    assert verdict.confidence("missing", default=0.5) == 0.5
    assert verdict.usage.input_tokens == 10
    assert verdict.latency_ms == 42
    assert verdict.cache_hit is False
    assert verdict.probabilities("kind") == {"yes": 0.8}
    assert verdict.probabilities("missing") == {}


def test_ask_returns_verdict_on_success():
    client = _FakeClient(response=_FakeResponse())
    manager = _manager_with_fake_client(client)

    verdict = asyncio.run(manager.ask({"command": "ls"}, {}, tag="exec_approval"))

    assert isinstance(verdict, Verdict)
    assert verdict.noul("is_safe") == 0.9
    assert client.calls == 1
    assert manager.stats == {"calls": 1, "cache_hits": 0, "failures": 0}


def test_ask_caches_identical_state_and_questions():
    client = _FakeClient(response=_FakeResponse())
    manager = _manager_with_fake_client(client)
    state = {"command": "ls -la"}

    first = asyncio.run(manager.ask(state, {"q": {"type": "noul"}}, tag="exec_approval"))
    second = asyncio.run(manager.ask(state, {"q": {"type": "noul"}}, tag="exec_approval"))

    assert client.calls == 1  # second call served from cache
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert manager.cache_hits == 1


def test_ask_evicts_lru_beyond_cache_size():
    client = _FakeClient(response=_FakeResponse())
    manager = _manager_with_fake_client(client)
    manager._cache_size = 1

    asyncio.run(manager.ask({"a": 1}, {}, tag="t"))
    asyncio.run(manager.ask({"a": 2}, {}, tag="t"))
    # First entry evicted; asking again re-calls the client.
    asyncio.run(manager.ask({"a": 1}, {}, tag="t"))

    assert client.calls == 3


@pytest.mark.parametrize(
    "exc",
    [
        judge_module.TypeSafeError("boom"),
        TimeoutError("slow"),
        RuntimeError("unexpected"),
    ],
)
def test_ask_swallows_every_failure_and_returns_none(exc):
    client = _FakeClient(exc=exc)
    manager = _manager_with_fake_client(client)

    result = asyncio.run(manager.ask({"x": 1}, {}, tag="t"))

    assert result is None
    assert manager.failures == 1


def test_ask_reports_first_failure_only():
    seen: list[str] = []
    client = _FakeClient(exc=RuntimeError("timeout"))
    manager = _manager_with_fake_client(client)
    manager._on_failure = seen.append

    assert asyncio.run(manager.ask({"x": 1}, {}, tag="exec_approval")) is None
    assert asyncio.run(manager.ask({"x": 2}, {}, tag="search_rerank")) is None

    assert manager.failures == 2
    assert len(seen) == 1
    assert "exec_approval" in seen[0]
    assert "timeout" in seen[0]


def test_ask_emits_on_request_for_ok_cache_and_error():
    seen: list[tuple] = []

    def on_request(tag, verdict, failed):
        seen.append((tag, getattr(verdict, "cache_hit", None), failed))

    client = _FakeClient(response=_FakeResponse())
    manager = _manager_with_fake_client(client)
    manager._on_request = on_request
    state = {"command": "ls"}

    asyncio.run(manager.ask(state, {}, tag="exec_approval"))
    asyncio.run(manager.ask(state, {}, tag="exec_approval"))
    manager._client = _FakeClient(exc=RuntimeError("down"))
    asyncio.run(manager.ask({"other": 1}, {}, tag="call_verify"))

    assert seen[0] == ("exec_approval", False, False)
    assert seen[1] == ("exec_approval", True, False)
    assert seen[2] == ("call_verify", None, True)


def test_on_request_exception_does_not_escape_ask():
    client = _FakeClient(response=_FakeResponse())
    manager = _manager_with_fake_client(client)

    def boom(*_args):
        raise RuntimeError("metrics")

    manager._on_request = boom
    verdict = asyncio.run(manager.ask({"x": 1}, {}, tag="t"))
    assert isinstance(verdict, Verdict)


def test_aclose_closes_underlying_client():
    client = _FakeClient(response=_FakeResponse())
    manager = _manager_with_fake_client(client)
    asyncio.run(manager.aclose())
    assert client.closed is True


def test_aclose_is_a_noop_when_disabled():
    manager = JudgeManager(_config(typesafe_api_key=""))
    asyncio.run(manager.aclose())  # must not raise


def test_cache_key_is_stable_and_order_independent():
    key_a = _cache_key({"a": 1, "b": 2}, {"q1": {"type": "noul"}})
    key_b = _cache_key({"b": 2, "a": 1}, {"q1": {"type": "noul"}})
    key_c = _cache_key({"a": 1, "b": 3}, {"q1": {"type": "noul"}})
    assert key_a == key_b
    assert key_a != key_c
