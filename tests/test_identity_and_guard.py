from __future__ import annotations

from runtime.tools.fileid import guard_write_path, read_source, render
from runtime.tools.fs import WorkspacePathError
from tests.conftest import seed


def test_crlf_round_trip(ctx):
    raw = b"hello\r\nworld\r\n"
    path = ctx.workspace / "a.py"
    path.write_bytes(raw)
    src = read_source(ctx.workspace, "a.py")
    assert src.newline == "\r\n"
    assert src.trailing_newline is True
    assert src.text == "hello\nworld\n"
    assert render(src, src.text) == raw


def test_no_trailing_newline_round_trip(ctx):
    raw = b"hello\nworld"
    (ctx.workspace / "a.py").write_bytes(raw)
    src = read_source(ctx.workspace, "a.py")
    assert src.trailing_newline is False
    assert render(src, src.text) == raw


def test_bom_preserved(ctx):
    raw = b"\xef\xbb\xbfprint(1)\n"
    (ctx.workspace / "a.py").write_bytes(raw)
    src = read_source(ctx.workspace, "a.py")
    assert src.encoding == "utf-8-sig"
    assert src.text == "print(1)\n"
    assert render(src, src.text) == raw


def test_guard_denylist(ctx):
    (ctx.workspace / ".git").mkdir()
    (ctx.workspace / ".git" / "HEAD").write_text("ref\n")
    (ctx.workspace / ".engine").mkdir()
    (ctx.workspace / ".engine" / "x").write_text("x\n")
    (ctx.workspace / "env.sh").write_text("KEY=1\n")
    (ctx.workspace / "package-lock.json").write_text("{}\n")
    (ctx.workspace / "node_modules").mkdir()
    (ctx.workspace / "node_modules" / "pkg.js").write_text("1\n")
    cases = [
        ".git/HEAD",
        ".engine/x",
        "env.sh",
        "package-lock.json",
        "node_modules/pkg.js",
        ".env.local",
    ]
    (ctx.workspace / ".env.local").write_text("x=1\n")
    for path in cases:
        try:
            guard_write_path(ctx.workspace, path)
            raise AssertionError(f"expected deny for {path}")
        except WorkspacePathError:
            pass


def test_guard_symlink_inside_workspace(ctx):
    target = ctx.workspace / "real.py"
    target.write_text("print(1)\n")
    link = ctx.workspace / "alias.py"
    link.symlink_to(target)
    try:
        guard_write_path(ctx.workspace, "alias.py")
        raise AssertionError("expected symlink refusal")
    except WorkspacePathError as exc:
        assert "symlink" in str(exc)
    assert target.read_text() == "print(1)\n"


def test_create_file_identity_from_crlf_sibling(ctx):
    seed(ctx, "mod.py", "print(1)\n", newline="\r\n")
    from runtime.tools.edits import apply_edit
    import asyncio

    async def run():
        return await apply_edit(
            ctx, "other.py", lambda src: "print(2)\n", "create_file", creating=True
        )

    result = asyncio.run(run())
    assert result.startswith("ok:")
    data = (ctx.workspace / "other.py").read_bytes()
    assert data == b"print(2)\r\n"
