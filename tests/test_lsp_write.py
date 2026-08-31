"""Integration tests for the LSP write side against a real language server.

These cover the Phase 4 claim that the server's view of a file is refreshed
after every write. A fake LSP cannot exercise any of it: the version counter,
the publish-generation counter, and the multi-file rename applier only have
meaning against a server that actually republishes diagnostics.

gopls is used because it runs straight off PATH with no network fetch, unlike
the npx-based pyright and typescript-language-server configs.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path

import pytest

from runtime.store import edits as journal
from runtime.store.edits import ensure_schema
from runtime.tools.edits import (
    apply_edit,
    apply_workspace_edit,
    normalize_workspace_edit,
    str_replace,
    undo_last_sync,
)
from runtime.tools.fileid import read_source
from runtime.tools.lsp import LSPManager
from runtime.tools.lsp import rename_symbol as lsp_rename_symbol
from runtime.tools.tracker import FileTracker
from tools.base import ToolContext
from tools.lsp import rename_symbol as rename_symbol_tool

pytestmark = [
    pytest.mark.lsp,
    pytest.mark.skipif(shutil.which("gopls") is None, reason="gopls is not installed"),
]

DIAG_TIMEOUT = 20.0

GO_MOD = "module example.com/probe\n\ngo 1.21\n"

A_CLEAN = """package probe

func Greet(name string) string {
	return "hi " + name
}
"""

A_BROKEN = """package probe

func Greet(name string) string {
	return "hi " + name + missingIdent
}
"""

A_PRE_BROKEN = """package probe

func Greet(name string) string {
	_ = firstBad
	return "hi " + name
}
"""

B_CALLER = """package probe

func Welcome() string {
	return Greet("world")
}
"""

# Same code as A_CLEAN with Greet pushed from line 2 down to line 5 (0-based).
A_SHIFTED = """package probe

// spacer
// spacer

func Greet(name string) string {
	return "hi " + name
}
"""


@pytest.fixture
def go_ws(tmp_path):
    (tmp_path / "go.mod").write_text(GO_MOD)
    (tmp_path / "a.go").write_text(A_CLEAN)
    (tmp_path / "b.go").write_text(B_CALLER)
    manager = LSPManager(tmp_path)
    try:
        yield tmp_path, manager
    finally:
        manager.shutdown_all()


@pytest.fixture
def go_ctx(go_ws):
    root, manager = go_ws
    db = root / ".engine" / "session.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    ensure_schema(db)
    ctx = ToolContext(
        workspace=root,
        lsp=manager,
        files=FileTracker(),
        journal=db,
        session_id="lsp-test",
    )
    return ctx, root, manager, db


def _uri(root: Path, rel: str) -> str:
    return (root / rel).resolve().as_uri()


def _mark_read(ctx: ToolContext, rel: str) -> None:
    src = read_source(ctx.workspace, rel)
    ctx.files.mark(src.rel, src.raw_sha256)


def _messages(diags: list) -> str:
    return " | ".join((item.get("message") or "") for item in diags)


def _symbol_line(symbols: list, name: str) -> int | None:
    """0-based line the server reports for a top-level symbol."""
    for item in symbols or []:
        if item.get("name") != name:
            continue
        if "location" in item:
            rng = item["location"].get("range") or {}
        else:
            rng = item.get("selectionRange") or item.get("range") or {}
        return (rng.get("start") or {}).get("line")
    return None


def test_did_change_refreshes_a_stale_server_view(go_ws):
    """The bug Phase 4 exists to fix: warm_start leaves the server holding
    old content, so diagnostics are stale with no indication of staleness."""
    root, manager = go_ws
    assert manager.open_file_and_get_diagnostics("a.go", wait_timeout=DIAG_TIMEOUT) == []

    (root / "a.go").write_text(A_BROKEN)

    fresh = manager.diagnostics_after_change("a.go", A_BROKEN, timeout=DIAG_TIMEOUT)
    assert "missingIdent" in _messages(fresh)


def test_get_diagnostics_resyncs_after_an_out_of_band_change(go_ws):
    """A file can change outside the edit funnel: the user's editor, a git
    checkout, a build step. The cached entry would otherwise be returned for
    content the server no longer has, with nothing marking it stale."""
    root, manager = go_ws
    assert manager.open_file_and_get_diagnostics("a.go", wait_timeout=DIAG_TIMEOUT) == []

    (root / "a.go").write_text(A_BROKEN)

    diags = manager.open_file_and_get_diagnostics("a.go", wait_timeout=DIAG_TIMEOUT)
    assert "missingIdent" in _messages(diags)


def test_get_diagnostics_does_not_resync_when_content_is_unchanged(go_ws):
    """The resync must be driven by content, not fire on every call, or every
    read pays a republish wait."""
    root, manager = go_ws
    manager.open_file_and_get_diagnostics("a.go", wait_timeout=DIAG_TIMEOUT)
    full = str((root / "a.go").resolve())
    version = manager._versions[full]

    for _ in range(3):
        assert manager.open_file_and_get_diagnostics("a.go") == []

    assert manager._versions[full] == version, "resynced despite identical content"


def test_position_queries_resync_after_an_out_of_band_change(go_ws):
    """Stale content shifts line numbers, so position-based answers are wrong
    rather than merely out of date."""
    root, manager = go_ws
    symbols = manager.ask_document_symbols("a.go")
    assert _symbol_line(symbols, "Greet") == 2

    (root / "a.go").write_text(A_SHIFTED)

    symbols = manager.ask_document_symbols("a.go")
    assert _symbol_line(symbols, "Greet") == 5


def test_did_change_bumps_the_document_version(go_ws):
    root, manager = go_ws
    manager.open_file_and_get_diagnostics("a.go", wait_timeout=DIAG_TIMEOUT)
    full = str((root / "a.go").resolve())
    first = manager._versions[full]

    manager.did_change("a.go", A_BROKEN)
    manager.did_change("a.go", A_CLEAN)

    assert manager._versions[full] == first + 2


def test_publish_generation_counter_reports_cleared_errors(go_ws):
    """An empty diagnostics list and 'not published yet' are identical on the
    wire. Without the generation counter this would block for the full timeout
    and then return [] for the wrong reason."""
    root, manager = go_ws
    manager.open_file_and_get_diagnostics("a.go", wait_timeout=DIAG_TIMEOUT)
    broken = manager.diagnostics_after_change("a.go", A_BROKEN, timeout=DIAG_TIMEOUT)
    assert "missingIdent" in _messages(broken)

    uri = _uri(root, "a.go")
    seq_before = manager._diag_seq[uri]

    started = time.monotonic()
    cleared = manager.diagnostics_after_change("a.go", A_CLEAN, timeout=DIAG_TIMEOUT)
    elapsed = time.monotonic() - started

    assert cleared == []
    assert manager._diag_seq[uri] > seq_before, "no fresh publish was observed"
    assert elapsed < DIAG_TIMEOUT / 2, "returned via timeout, not a real publish"


def test_did_change_opens_a_file_the_server_has_not_seen(go_ws):
    root, manager = go_ws
    (root / "c.go").write_text(
        "package probe\n\nfunc Other() string {\n\treturn lateIdent\n}\n"
    )

    diags = manager.diagnostics_after_change(
        "c.go",
        (root / "c.go").read_text(),
        timeout=DIAG_TIMEOUT,
    )

    assert "lateIdent" in _messages(diags)
    assert str((root / "c.go").resolve()) in manager._opened_files


def test_apply_edit_reports_only_newly_introduced_diagnostics(go_ctx):
    """Pre-existing breakage must not be re-reported as though this edit
    caused it, and the edit's own breakage must not be swallowed."""
    ctx, root, manager, _db = go_ctx
    (root / "a.go").write_text(A_PRE_BROKEN)
    before = manager.open_file_and_get_diagnostics("a.go", wait_timeout=DIAG_TIMEOUT)
    assert "firstBad" in _messages(before)

    _mark_read(ctx, "a.go")

    result = asyncio.run(
        apply_edit(
            ctx,
            "a.go",
            lambda src: str_replace(
                src.text,
                '\treturn "hi " + name',
                '\t_ = secondBad\n\treturn "hi " + name',
            ),
            "str_replace",
        )
    )

    assert result.startswith("ok:")
    # Scope the check to the diagnostics block; the diff legitimately quotes
    # the pre-existing bad line as surrounding context.
    assert "new diagnostics:" in result
    reported = result.split("new diagnostics:", 1)[1]
    assert "secondBad" in reported
    assert "firstBad" not in reported


def test_rename_symbol_applies_across_files_as_one_batch(go_ctx):
    ctx, root, manager, db = go_ctx
    manager.open_file_and_get_diagnostics("a.go", wait_timeout=DIAG_TIMEOUT)
    manager.open_file_and_get_diagnostics("b.go", wait_timeout=DIAG_TIMEOUT)

    payload = lsp_rename_symbol(root, manager, "a.go", 3, 6, "Salute")
    assert not isinstance(payload, str), payload

    grouped = normalize_workspace_edit(root, payload)
    assert {rel for rel, _ in grouped} == {"a.go", "b.go"}

    output = asyncio.run(apply_workspace_edit(ctx, grouped, "rename_symbol"))
    assert "error:" not in output

    assert "func Salute(" in (root / "a.go").read_text()
    assert "Salute(\"world\")" in (root / "b.go").read_text()
    assert "Greet" not in (root / "a.go").read_text()
    assert "Greet" not in (root / "b.go").read_text()

    rows = journal.recent(db, "lsp-test", limit=10)
    assert len(rows) == 2
    assert len({row.batch_id for row in rows}) == 1, "rename must share one batch_id"


def test_undo_reverts_an_entire_rename_batch(go_ctx):
    ctx, root, manager, _db = go_ctx
    manager.open_file_and_get_diagnostics("a.go", wait_timeout=DIAG_TIMEOUT)
    manager.open_file_and_get_diagnostics("b.go", wait_timeout=DIAG_TIMEOUT)

    payload = lsp_rename_symbol(root, manager, "a.go", 3, 6, "Salute")
    grouped = normalize_workspace_edit(root, payload)
    asyncio.run(apply_workspace_edit(ctx, grouped, "rename_symbol"))
    assert "Salute" in (root / "b.go").read_text()

    result = undo_last_sync(ctx)

    assert result.ok, result.message
    assert (root / "a.go").read_text() == A_CLEAN
    assert (root / "b.go").read_text() == B_CALLER


def test_rename_symbol_tool_end_to_end(go_ctx):
    ctx, root, manager, _db = go_ctx
    manager.open_file_and_get_diagnostics("a.go", wait_timeout=DIAG_TIMEOUT)
    manager.open_file_and_get_diagnostics("b.go", wait_timeout=DIAG_TIMEOUT)

    output = asyncio.run(
        rename_symbol_tool._engine_tool.execute(
            ctx,
            {"path": "a.go", "line": 3, "character": 6, "new_name": "Salute"},
        )
    )

    assert "error:" not in output
    assert "a.go" in output and "b.go" in output
    assert "func Salute(" in (root / "a.go").read_text()
    assert "Salute(\"world\")" in (root / "b.go").read_text()
