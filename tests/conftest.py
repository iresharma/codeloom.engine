from __future__ import annotations

import pytest

from runtime.config import EngineConfig
from runtime.store.edits import ensure_schema
from runtime.tools.fileid import read_source
from runtime.tools.tracker import FileTracker
from tools.base import ToolContext


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


def seed(ctx: ToolContext, rel: str, text: str, newline: str = "\n") -> None:
    path = ctx.workspace / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    body = text.replace("\n", newline)
    if text.endswith("\n") and newline != "\n":
        body = text.replace("\n", newline)
    path.write_bytes(body.encode("utf-8"))
    src = read_source(ctx.workspace, rel)
    ctx.files.mark(src.rel, src.raw_sha256)
