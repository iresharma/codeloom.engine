from __future__ import annotations

import asyncio
from contextlib import suppress

import pytest

from runtime.config import EngineConfig
from runtime.store.edits import ensure_schema
from runtime.tools.fileid import read_source
from runtime.tools.tracker import FileTracker
from tools.base import ToolContext

_SECRET_ENV = (
    "TYPESAFE_API_KEY",
    "TYPESAFE_JEV_API_KEY",
    "OPENROUTER_API_KEY",
    "OPENROUTER_CHILD_MODEL",
)


@pytest.fixture(autouse=True)
def _isolate_secrets_and_close_sessions(request, monkeypatch):
    """Keep developer env.sh keys out of the suite, and tear down sessions.

    EngineSession.__init__ used to open a TypeSafe HTTP client whenever a
    key was in the environment. Combined with pytest-xdist `-n auto` that
    left worker processes holding sockets, LSP children, and gigabytes of
    RSS after the run (or after Ctrl-C).
    """
    if request.node.get_closest_marker("judge") is None:
        for name in _SECRET_ENV:
            monkeypatch.delenv(name, raising=False)

    from runtime.session import EngineSession

    created: list = []
    original_init = EngineSession.__init__

    def tracking_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        created.append(self)

    monkeypatch.setattr(EngineSession, "__init__", tracking_init)
    yield
    for sess in created:
        _force_close_session(sess)


def _force_close_session(sess) -> None:
    with suppress(Exception):
        sess._kill_live_procs()
    with suppress(Exception):
        sess._stop_lsp()
    _close_async_resource(getattr(sess, "_judge", None))
    sess._judge = None
    mcp = getattr(sess, "_mcp", None)
    sess._mcp = None
    _close_async_resource(mcp)
    with suppress(Exception):
        sess.close_session()


def _close_async_resource(resource) -> None:
    if resource is None:
        return
    close = getattr(resource, "aclose", None)
    if close is None:
        return
    with suppress(Exception):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(close())
        else:
            # Already inside pytest-asyncio's loop; drop the client so the
            # process can exit even if we cannot await here.
            if getattr(resource, "_client", None) is not None:
                resource._client = None


@pytest.fixture
def ctx(tmp_path):
    db = tmp_path / "session.db"
    ensure_schema(db)
    return ToolContext(
        workspace=tmp_path,
        files=FileTracker(),
        journal=db,
        session_id="test-session",
        config=EngineConfig(),
    )


class FakeVerdict:
    """Stands in for runtime.judge.Verdict without a real SystemOneResponse."""

    def __init__(
        self, nouls=None, choices=None, scores=None, confidences=None, probabilities=None
    ):
        self._nouls = nouls or {}
        self._choices = choices or {}
        self._scores = scores or {}
        self._confidences = confidences or {}
        self._probabilities = probabilities or {}
        self.latency_ms = 0
        self.cache_hit = False

    def noul(self, key, default=0.0):
        return self._nouls.get(key, default)

    def choice(self, key, default=""):
        return self._choices.get(key, default)

    def score(self, key, default=0.0):
        return self._scores.get(key, default)

    def confidence(self, key, default=0.0):
        return self._confidences.get(key, default)

    def probabilities(self, key):
        return dict(self._probabilities.get(key, {}))

    @property
    def usage(self):
        return None


class FakeJudge:
    """Scripted stand-in for runtime.judge.JudgeManager; no network.

    Set `responses[tag]` to a FakeVerdict (or leave unset / None to make
    `ask()` return None, reproducing "judge unavailable" behaviour) before
    exercising code that calls `ctx.judge.ask(...)`. Every call is recorded
    in `calls` for assertions.
    """

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.responses: dict[str, FakeVerdict | None] = {}
        self.calls: list[dict] = []

    async def ask(self, state, questions, *, tag: str):
        self.calls.append({"state": state, "questions": questions, "tag": tag})
        if not self.enabled:
            return None
        return self.responses.get(tag)


@pytest.fixture
def judge():
    return FakeJudge()


def seed(ctx: ToolContext, rel: str, text: str, newline: str = "\n") -> None:
    path = ctx.workspace / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    body = text.replace("\n", newline)
    if text.endswith("\n") and newline != "\n":
        body = text.replace("\n", newline)
    path.write_bytes(body.encode("utf-8"))
    src = read_source(ctx.workspace, rel)
    ctx.files.mark(src.rel, src.raw_sha256)
