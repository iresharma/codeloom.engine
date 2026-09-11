from __future__ import annotations

import asyncio
import json
import threading

from agents.compactor import AgentResult
from runtime.store.memory import (
    FILE_NOTE_CAP,
    INGEST_FILE_CAP,
    RENDER_CAP,
    SECTION_CAP,
    ingest_result,
    load,
    remember,
    render_memory,
    touch,
)
from runtime.tools.edits import apply_edit
from runtime.tools.fileid import sha256_bytes
from runtime.tools.tracker import FileTracker
from tools.base import ToolContext
from tools.read_file import read_file
from tools.registry import discover_tools


def test_touch_does_not_clobber_note(tmp_path):
    target = tmp_path / "session.py"
    target.write_text("a = 1\n", encoding="utf-8")
    first = sha256_bytes(target.read_bytes())
    assert remember(tmp_path, "files", "EngineSession binds orch", path="session.py") == "ok"
    target.write_text("a = 2\n", encoding="utf-8")
    second = sha256_bytes(target.read_bytes())
    touch(tmp_path, "session.py", second, "read")
    entry = load(tmp_path)["files"]["session.py"]
    assert entry["note"] == "EngineSession binds orch"
    assert entry["purpose"] == "EngineSession binds orch"
    assert entry["note_sha"] == first
    assert entry["seen_sha"] == second
    assert entry["action"] == "read"


def test_file_note_stale_then_fresh(tmp_path):
    target = tmp_path / "git.py"
    target.write_text("old\n", encoding="utf-8")
    remember(tmp_path, "files", "git helpers", path="git.py")
    text = render_memory(tmp_path)
    assert "[fresh]" in text
    assert "git.py" in text
    target.write_text("new\n", encoding="utf-8")
    stale = render_memory(tmp_path)
    assert "[STALE]" in stale
    assert "git.py" in stale
    remember(tmp_path, "files", "git helpers after rewrite", path="git.py")
    fresh = render_memory(tmp_path)
    assert "[STALE]" not in fresh
    assert "[fresh]" in fresh
    assert "after rewrite" in fresh


def test_decision_section_keeps_newest(tmp_path):
    for index in range(SECTION_CAP + 3):
        remember(tmp_path, "engineering", f"decision-{index}-unique")
    data = load(tmp_path)
    texts = [item["text"] for item in data["engineering"]]
    assert len(texts) == SECTION_CAP
    assert "decision-0-unique" not in texts
    assert "decision-14-unique" in texts
    rendered = render_memory(tmp_path)
    assert "### Engineering" in rendered
    assert "decision-14-unique" in rendered
    assert "decision-0-unique" not in rendered


def test_remember_and_touch_concurrent(tmp_path):
    errors = []
    for index in range(20):
        (tmp_path / f"f{index}.py").write_text(f"v={index}\n", encoding="utf-8")

    def work(n):
        try:
            if n % 2 == 0:
                remember(tmp_path, "files", f"note-{n}-unique", path=f"f{n}.py")
            else:
                digest = sha256_bytes((tmp_path / f"f{n}.py").read_bytes())
                touch(tmp_path, f"f{n}.py", digest, "read")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    files = load(tmp_path)["files"]
    for index in range(0, 20, 2):
        assert f"f{index}.py" in files
        assert files[f"f{index}.py"]["note"] == f"note-{index}-unique"
    for index in range(1, 20, 2):
        assert f"f{index}.py" not in files


def test_render_prefers_decisions_and_caps(tmp_path):
    remember(tmp_path, "product", "ship the memory store")
    remember(tmp_path, "cicd", "pytest on every push")
    for index in range(FILE_NOTE_CAP):
        name = f"big{index}.py"
        (tmp_path / name).write_text(f"x={index}\n", encoding="utf-8")
        remember(tmp_path, "files", ("blob " * 80) + f"file-{index}", path=name)
    text = render_memory(tmp_path, cap=RENDER_CAP)
    assert "ship the memory store" in text
    assert "pytest on every push" in text
    assert len(text) <= RENDER_CAP
    assert text.startswith("## Workspace memory")


def test_missing_file_is_dropped(tmp_path):
    target = tmp_path / "gone.py"
    target.write_text("x\n", encoding="utf-8")
    remember(tmp_path, "files", "will vanish", path="gone.py")
    target.unlink()
    text = render_memory(tmp_path)
    assert "gone.py" not in text
    assert "gone.py" not in load(tmp_path)["files"]


def test_read_file_does_not_create_empty_entry(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    ctx = ToolContext(workspace=tmp_path, files=FileTracker())
    read_file(ctx, "a.py")
    assert load(tmp_path)["files"] == {}
    assert not (tmp_path / ".engine" / "memory.json").exists()


def test_read_file_updates_seen_sha_on_noted_file(tmp_path):
    target = tmp_path / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")
    remember(tmp_path, "files", "a module", path="a.py")
    first = load(tmp_path)["files"]["a.py"]["seen_sha"]
    target.write_text("x = 2\n", encoding="utf-8")
    ctx = ToolContext(workspace=tmp_path, files=FileTracker())
    read_file(ctx, "a.py")
    entry = load(tmp_path)["files"]["a.py"]
    assert entry["note"] == "a module"
    assert entry["seen_sha"] == sha256_bytes(b"x = 2\n")
    assert entry["seen_sha"] != first
    assert entry["action"] == "read"


def test_apply_edit_updates_noted_file(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    remember(tmp_path, "files", "a module", path="a.py")
    ctx = ToolContext(workspace=tmp_path, files=FileTracker())
    read_file(ctx, "a.py")

    async def run():
        return await apply_edit(ctx, "a.py", lambda _src: "x = 2\n", "str_replace")

    result = asyncio.run(run())
    assert result.startswith("ok:")
    entry = load(tmp_path)["files"]["a.py"]
    assert entry["action"] == "edit"
    assert entry["seen_sha"] == sha256_bytes(b"x = 2\n")
    assert entry["note"] == "a module"


def test_remember_tool_is_discovered():
    registry = discover_tools()
    assert "remember" in registry.names()
    assert not any("remember" in item for item in registry.errors)


def test_invalid_remember(tmp_path):
    assert remember(tmp_path, "files", "no path").startswith("error:")
    assert remember(tmp_path, "nope", "x").startswith("error:")
    assert remember(tmp_path, "engineering", "  ").startswith("error:")
    (tmp_path / "ok.py").write_text("x\n", encoding="utf-8")
    assert remember(tmp_path, "files", "missing", path="nope.py").startswith("error:")
    assert remember(tmp_path, "files", path="ok.py").startswith("error:")


def test_empty_render(tmp_path):
    assert render_memory(tmp_path) == ""
    assert not (tmp_path / ".engine" / "memory.json").exists()


def test_structured_remember_and_render(tmp_path):
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    assert (
        remember(
            tmp_path,
            "files",
            path="a.py",
            purpose="alpha",
            entry_points="foo",
            constraints="no globals",
        )
        == "ok"
    )
    text = render_memory(tmp_path)
    assert "purpose: alpha" in text
    assert "entry: foo" in text
    assert "constraints: no globals" in text
    assert "Recently touched" not in text
    entry = load(tmp_path)["files"]["a.py"]
    assert entry["purpose"] == "alpha"
    assert entry["entry_points"] == "foo"


def test_render_drops_touch_only_entries(tmp_path):
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    digest = sha256_bytes(b"x\n")
    engine = tmp_path / ".engine"
    engine.mkdir()
    (engine / "memory.json").write_text(
        json.dumps(
            {
                "files": {
                    "a.py": {
                        "seen_sha": digest,
                        "note": "",
                        "note_sha": None,
                        "action": "read",
                        "updated_at": "2026-01-01T00:00:00+00:00",
                    }
                },
                "engineering": [],
                "product": [],
                "cicd": [],
                "other": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    text = render_memory(tmp_path)
    assert "Recently touched" not in text
    assert "a.py" not in text
    assert "a.py" not in load(tmp_path)["files"]


def test_ingest_ask_writes_fields_and_engineering(tmp_path):
    (tmp_path / "session.py").write_text("class EngineSession:\n    pass\n", encoding="utf-8")
    result = AgentResult(
        status="ok",
        outcome=(
            "what: EngineSession binds orch\n"
            "paths: session.py\n"
            "facts: EngineSession._bind_loop wires tools\n"
            "verdict: session.py is the session root\n"
        ),
        leftover_questions=["none of the leftover"],
    )
    ingest_result(tmp_path, "ask", result, survey_paths=["session.py"])
    data = load(tmp_path)
    assert data["engineering"]
    assert "session root" in data["engineering"][-1]["text"]
    entry = data["files"]["session.py"]
    assert "EngineSession binds orch" in entry["purpose"]
    assert "session.py" in entry["entry_points"]
    assert "leftover" in entry["constraints"]
    rendered = render_memory(tmp_path)
    assert "purpose:" in rendered
    assert "[fresh]" in rendered
    assert "Recently touched" not in rendered


def test_ingest_duplicate_verdict_not_appended(tmp_path):
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    result = AgentResult(
        status="ok",
        outcome="what: keep\nverdict: ship it\npaths: a.py\n",
    )
    ingest_result(tmp_path, "ask", result)
    ingest_result(tmp_path, "ask", result)
    assert len(load(tmp_path)["engineering"]) == 1


def test_ingest_skips_tester_and_aborted(tmp_path):
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    result = AgentResult(
        status="ok",
        outcome="what: x\nverdict: y\npaths: a.py\n",
    )
    ingest_result(tmp_path, "tester", result)
    ingest_result(
        tmp_path,
        "ask",
        AgentResult(status="aborted", outcome="what: x\nverdict: y\npaths: a.py\n"),
    )
    ingest_result(
        tmp_path,
        "ask",
        AgentResult(status="failed", outcome="what: x\nverdict: y\npaths: a.py\n"),
    )
    ingest_result(
        tmp_path,
        "reviewer",
        result,
    )
    data = load(tmp_path)
    assert data["engineering"] == []
    assert data["files"] == {}


def test_ingest_coder_uses_files_touched(tmp_path):
    (tmp_path / "edited.py").write_text("x\n", encoding="utf-8")
    (tmp_path / "other.py").write_text("y\n", encoding="utf-8")
    result = AgentResult(
        status="ok",
        outcome="what: edited the writer\nverdict: done\npaths: other.py\n",
        files_touched=["edited.py"],
    )
    ingest_result(tmp_path, "coder", result, survey_paths=["other.py"])
    files = load(tmp_path)["files"]
    assert "edited.py" in files
    assert "other.py" not in files


def test_ingest_coder_dedupes_files_touched(tmp_path):
    (tmp_path / "edited.py").write_text("x\n", encoding="utf-8")
    extras = []
    for index in range(INGEST_FILE_CAP):
        name = f"f{index}.py"
        (tmp_path / name).write_text("x\n", encoding="utf-8")
        extras.append(name)
    result = AgentResult(
        status="ok",
        outcome="what: edited\nverdict: done\n",
        files_touched=["edited.py", "edited.py", *extras],
    )
    ingest_result(tmp_path, "coder", result)
    files = load(tmp_path)["files"]
    assert len(files) == INGEST_FILE_CAP
    assert "edited.py" in files
    assert extras[INGEST_FILE_CAP - 2] in files


def test_ingest_fact_keeps_dotted_filename(tmp_path):
    (tmp_path / "session.py").write_text("x\n", encoding="utf-8")
    result = AgentResult(
        status="ok",
        outcome=(
            "what: session root\n"
            "paths: session.py\n"
            "facts: retry lives in session.py near _bind_loop.\n"
            "verdict: ok\n"
        ),
    )
    ingest_result(tmp_path, "ask", result)
    entry = load(tmp_path)["files"]["session.py"]
    assert "retry lives in session.py" in entry["entry_points"]


def test_ingest_ask_caps_files(tmp_path):
    paths = []
    for index in range(INGEST_FILE_CAP + 3):
        name = f"f{index}.py"
        (tmp_path / name).write_text("x\n", encoding="utf-8")
        paths.append(name)
    result = AgentResult(
        status="ok",
        outcome="what: survey\nverdict: many files\npaths: " + ", ".join(paths) + "\n",
    )
    ingest_result(tmp_path, "ask", result, survey_paths=paths)
    assert len(load(tmp_path)["files"]) == INGEST_FILE_CAP


def test_ingest_researcher_goes_to_other(tmp_path):
    result = AgentResult(
        status="ok",
        outcome="what: lib X\nverdict: skip Graphify\n",
    )
    ingest_result(tmp_path, "researcher", result)
    data = load(tmp_path)
    assert data["engineering"] == []
    assert "skip Graphify" in data["other"][-1]["text"]
    assert data["files"] == {}


def test_ingest_max_turns_still_writes(tmp_path):
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    result = AgentResult(
        status="max_turns",
        outcome="what: partial\nverdict: still useful\npaths: a.py\n",
    )
    ingest_result(tmp_path, "ask", result)
    assert load(tmp_path)["engineering"]
    assert "a.py" in load(tmp_path)["files"]


def test_ingest_stopped_skips(tmp_path):
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    result = AgentResult(
        status="stopped",
        outcome="what: partial\nverdict: still useful\npaths: a.py\n",
    )
    ingest_result(tmp_path, "ask", result)
    data = load(tmp_path)
    assert data["engineering"] == []
    assert data["files"] == {}


def test_ingest_skips_fresh_note(tmp_path):
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    remember(tmp_path, "files", path="a.py", purpose="hand-written")
    result = AgentResult(
        status="ok",
        outcome="what: generic survey blurb\nverdict: v\npaths: a.py\n",
    )
    ingest_result(tmp_path, "ask", result)
    assert load(tmp_path)["files"]["a.py"]["purpose"] == "hand-written"


def test_ingest_refreshes_stale_note(tmp_path):
    target = tmp_path / "a.py"
    target.write_text("old\n", encoding="utf-8")
    remember(
        tmp_path,
        "files",
        path="a.py",
        purpose="hand-written",
        entry_points="old_fn",
        constraints="keep x",
    )
    target.write_text("new\n", encoding="utf-8")
    assert "[STALE]" in render_memory(tmp_path)
    result = AgentResult(
        status="ok",
        outcome=(
            "what: rewritten after edit\n"
            "paths: a.py\n"
            "facts: a.py now exports new_fn\n"
            "verdict: caught up\n"
        ),
        leftover_questions=["check callers"],
    )
    ingest_result(tmp_path, "ask", result)
    entry = load(tmp_path)["files"]["a.py"]
    assert "rewritten after edit" in entry["purpose"]
    assert entry["entry_points"] != "old_fn"
    assert "a.py" in entry["entry_points"]
    assert "callers" in entry["constraints"]
    assert entry["note_sha"] == sha256_bytes(b"new\n")
    assert "[STALE]" not in render_memory(tmp_path)
    assert "[fresh]" in render_memory(tmp_path)


def test_remember_merges_partial_fields(tmp_path):
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    remember(
        tmp_path,
        "files",
        path="a.py",
        purpose="alpha",
        entry_points="foo",
        constraints="no globals",
    )
    assert remember(tmp_path, "files", path="a.py", purpose="beta") == "ok"
    entry = load(tmp_path)["files"]["a.py"]
    assert entry["purpose"] == "beta"
    assert entry["entry_points"] == "foo"
    assert entry["constraints"] == "no globals"


def test_ingest_hashes_child_tree_stores_on_main(tmp_path):
    main = tmp_path / "main"
    tree = tmp_path / "tree"
    main.mkdir()
    tree.mkdir()
    (main / "a.py").write_text("old\n", encoding="utf-8")
    (tree / "a.py").write_text("new\n", encoding="utf-8")
    result = AgentResult(
        status="ok",
        outcome="what: changed a\nverdict: edited\n",
        files_touched=["a.py"],
    )
    ingest_result(tree, "coder", result, store=main)
    entry = load(main)["files"]["a.py"]
    assert entry["note_sha"] == sha256_bytes(b"new\n")
    assert "[STALE]" in render_memory(main)


def test_file_tracker_paths():
    tracker = FileTracker()
    tracker.mark("a.py", "aaa")
    tracker.mark("b.py", "bbb")
    assert tracker.paths() == ["a.py", "b.py"]
    assert tracker.get("a.py") == "aaa"
