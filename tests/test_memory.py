from __future__ import annotations

import asyncio
import threading

from runtime.store.memory import (
    FILE_NOTE_CAP,
    RENDER_CAP,
    SECTION_CAP,
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
    for index in range(20):
        assert f"f{index}.py" in files
    for index in range(0, 20, 2):
        assert files[f"f{index}.py"]["note"] == f"note-{index}-unique"


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


def test_read_file_touches_memory(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    ctx = ToolContext(workspace=tmp_path, files=FileTracker())
    read_file(ctx, "a.py")
    entry = load(tmp_path)["files"]["a.py"]
    assert entry["action"] == "read"
    assert entry["seen_sha"] == sha256_bytes(b"x = 1\n")
    assert not entry.get("note")


def test_apply_edit_touches_memory(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    ctx = ToolContext(workspace=tmp_path, files=FileTracker())
    read_file(ctx, "a.py")

    async def run():
        return await apply_edit(ctx, "a.py", lambda _src: "x = 2\n", "str_replace")

    result = asyncio.run(run())
    assert result.startswith("ok:")
    entry = load(tmp_path)["files"]["a.py"]
    assert entry["action"] == "edit"
    assert entry["seen_sha"] == sha256_bytes(b"x = 2\n")


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


def test_empty_render(tmp_path):
    assert render_memory(tmp_path) == ""
    assert not (tmp_path / ".engine" / "memory.json").exists()
