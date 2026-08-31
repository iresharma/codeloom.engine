from __future__ import annotations

"""Write funnel. _prepare/_commit/_apply_sync must never await.

On a single event loop any stretch without await is atomic. Adding an await
inside those functions silently reintroduces races with no visible symptom
until a file is corrupted.
"""

import asyncio
import difflib
import os
import re
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from runtime.store import edits as journal
from runtime.tools.fileid import (
    FileSource,
    guard_write_path,
    identity_from_sibling,
    read_source,
    render,
    sha256_bytes,
    synthetic_source,
)
from runtime.tools.fs import WorkspacePathError, relative_posix
from runtime.tools.sitter import syntax_gate
from tools.base import ToolContext

DIFF_RESULT_MAX = 4000
HUNK_FUZZ = 5
_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class EditError(ValueError):
    pass


@dataclass
class TextEdit:
    start_line: int
    start_char: int
    end_line: int
    end_char: int
    new_text: str


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    old_lines: list[str]
    new_lines: list[str]
    header: str


@dataclass
class PreparedEdit:
    src: FileSource
    new_text: str
    new_bytes: bytes
    before_bytes: bytes | None
    before_sha: str | None
    after_sha: str
    diff: str
    created_dirs: list[str] = field(default_factory=list)
    is_create: bool = False
    noop: bool = False


@dataclass
class ApplyResult:
    ok: bool
    message: str
    rel: str = ""
    diff: str = ""
    edit_id: int | None = None
    new_text: str = ""
    created: bool = False


def format_unified_diff(old: str, new: str, path: str, context: int = 3) -> str:
    a = old.splitlines(keepends=True)
    b = new.splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    groups = matcher.get_grouped_opcodes(context)
    if not groups:
        return ""
    out = [f"--- a/{path}\n", f"+++ b/{path}\n"]
    for group in groups:
        first, last = group[0], group[-1]
        i1, i2 = first[1], last[2]
        j1, j2 = first[3], last[4]
        out.append(f"@@ -{i1 + 1},{i2 - i1} +{j1 + 1},{j2 - j1} @@\n")
        for tag, ai1, ai2, bj1, bj2 in group:
            if tag == "equal":
                for line in a[ai1:ai2]:
                    out.append(_diff_line(" ", line))
            if tag in ("replace", "delete"):
                for line in a[ai1:ai2]:
                    out.append(_diff_line("-", line))
            if tag in ("replace", "insert"):
                for line in b[bj1:bj2]:
                    out.append(_diff_line("+", line))
    return "".join(out)


def _diff_line(prefix: str, line: str) -> str:
    if line.endswith("\n"):
        return prefix + line
    return prefix + line + "\n"


def str_replace(text: str, old: str, new: str) -> str:
    if not old:
        raise EditError("error: old_string is empty")
    count = text.count(old)
    if count == 1:
        return text.replace(old, new, 1)
    if count > 1:
        lines = []
        start = 0
        while True:
            index = text.find(old, start)
            if index < 0:
                break
            lines.append(str(text.count("\n", 0, index) + 1))
            start = index + 1
        listed = ", ".join(lines[:20])
        extra = f" ({count} total)" if count > 20 else ""
        raise EditError(
            f"error: old_string matched {count} times at lines {listed}{extra}; "
            "include more surrounding context so the match is unique"
        )
    stripped_hit = _whitespace_drift_line(text, old)
    if stripped_hit is not None:
        raise EditError(
            f"error: old_string not found; found at line {stripped_hit} with "
            "whitespace differences; re-issue with the exact text"
        )
    hint = _closest_block_hint(text, old)
    extra = f"\nclosest existing block starts at line {hint}" if hint else ""
    raise EditError(f"error: old_string not found in file{extra}")


def _whitespace_drift_line(text: str, old: str) -> int | None:
    old_lines = [line.rstrip() for line in old.splitlines()]
    if not old_lines:
        return None
    hay = [line.rstrip() for line in text.splitlines()]
    hits: list[int] = []
    span = len(old_lines)
    for index in range(0, len(hay) - span + 1):
        if hay[index : index + span] == old_lines:
            hits.append(index + 1)
            if len(hits) > 1:
                return None
    return hits[0] if len(hits) == 1 else None


def _closest_block_hint(text: str, old: str) -> int | None:
    needle = old.splitlines()
    if not needle:
        return None
    hay = text.splitlines()
    span = len(needle)
    if span > len(hay) or not hay:
        return None
    best_ratio = 0.0
    best_line = None
    step = 1 if len(hay) < 2000 else max(1, span // 2)
    for index in range(0, len(hay) - span + 1, step):
        window = hay[index : index + span]
        ratio = difflib.SequenceMatcher(
            a=needle, b=window, autojunk=False
        ).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_line = index + 1
    if best_ratio < 0.4:
        return None
    return best_line


def replace_lines(text: str, start: int, end: int, new_text: str) -> str:
    lines = text.splitlines(keepends=True)
    total = len(lines)
    if start < 1 or end < start:
        raise EditError(f"error: invalid line range {start}-{end}")
    if total == 0:
        if start != 1 or end != 1:
            raise EditError("error: file is empty; use insert_at_line or create_file")
        replacement = new_text
        if replacement and not replacement.endswith("\n"):
            replacement += "\n"
        return replacement
    if start > total or end > total:
        raise EditError(
            f"error: line range {start}-{end} is past end of file ({total} lines)"
        )
    replacement = new_text
    if replacement and not replacement.endswith("\n") and end < total:
        replacement += "\n"
    return "".join(lines[: start - 1]) + replacement + "".join(lines[end:])


def insert_at_line(text: str, line: int, new_text: str) -> str:
    lines = text.splitlines(keepends=True)
    total = len(lines)
    if line < 1 or line > total + 1:
        raise EditError(
            f"error: insert line {line} is out of range (1-{total + 1})"
        )
    insertion = new_text
    if insertion and not insertion.endswith("\n"):
        insertion += "\n"
    if line == total + 1:
        if lines and not lines[-1].endswith("\n"):
            return "".join(lines) + "\n" + insertion
        return "".join(lines) + insertion
    return "".join(lines[: line - 1]) + insertion + "".join(lines[line - 1 :])


def parse_unified_diff(patch: str) -> list[Hunk]:
    hunks: list[Hunk] = []
    lines = patch.splitlines()
    index = 0
    while index < len(lines):
        match = _HUNK_HEADER.match(lines[index])
        if not match:
            index += 1
            continue
        old_start = int(match.group(1))
        old_count = int(match.group(2) or "1")
        new_start = int(match.group(3))
        new_count = int(match.group(4) or "1")
        header = lines[index]
        index += 1
        old_lines: list[str] = []
        new_lines: list[str] = []
        while index < len(lines) and not lines[index].startswith("@@"):
            raw = lines[index]
            if raw.startswith("--- ") or raw.startswith("+++ "):
                break
            if raw.startswith("\\"):
                index += 1
                continue
            if raw.startswith("-"):
                old_lines.append(raw[1:])
            elif raw.startswith("+"):
                new_lines.append(raw[1:])
            elif raw.startswith(" "):
                old_lines.append(raw[1:])
                new_lines.append(raw[1:])
            elif raw == "":
                old_lines.append("")
                new_lines.append("")
            else:
                old_lines.append(raw)
                new_lines.append(raw)
            index += 1
        hunks.append(
            Hunk(
                old_start=old_start,
                old_count=old_count,
                new_start=new_start,
                new_count=new_count,
                old_lines=old_lines,
                new_lines=new_lines,
                header=header,
            )
        )
    if not hunks:
        raise EditError("error: patch contains no hunks")
    return hunks


def hunks_to_text(text: str, hunks: list[Hunk], fuzz: int = HUNK_FUZZ) -> str:
    lines = text.splitlines()
    located: list[tuple[int, Hunk]] = []
    for hunk in hunks:
        at = _locate_hunk(lines, hunk, fuzz)
        located.append((at, hunk))
    located.sort(key=lambda item: item[0], reverse=True)
    for at, hunk in located:
        end = at + len(hunk.old_lines)
        lines[at:end] = hunk.new_lines
    joined = "\n".join(lines)
    if text.endswith("\n") and (joined or text):
        if joined:
            joined += "\n"
        elif text == "\n":
            joined = "\n"
    return joined


def _locate_hunk(lines: list[str], hunk: Hunk, fuzz: int) -> int:
    target = hunk.old_lines
    if not target:
        expected = max(0, hunk.old_start)
        if expected > len(lines):
            expected = len(lines)
        return expected

    def matches(idx: int) -> bool:
        if idx < 0 or idx + len(target) > len(lines):
            return False
        return lines[idx : idx + len(target)] == target

    expected = max(0, hunk.old_start - 1)
    if matches(expected):
        return expected
    for delta in range(1, fuzz + 1):
        if matches(expected - delta):
            return expected - delta
        if matches(expected + delta):
            return expected + delta
    raise EditError(
        f"error: hunk did not match near line {hunk.old_start}: {hunk.header}"
    )


def apply_patch_text(text: str, patch: str) -> str:
    return hunks_to_text(text, parse_unified_diff(patch))


def apply_text_edits(text: str, edits: list[TextEdit]) -> str:
    ordered = sorted(
        edits, key=lambda item: (item.start_line, item.start_char), reverse=True
    )
    current = text
    for edit in ordered:
        start = _lsp_offset(current, edit.start_line, edit.start_char)
        end = _lsp_offset(current, edit.end_line, edit.end_char)
        current = current[:start] + edit.new_text + current[end:]
    return current


def _lsp_offset(text: str, line: int, character: int) -> int:
    lines = text.splitlines(keepends=True)
    if line < 0:
        return 0
    if line >= len(lines):
        return len(text)
    prefix = sum(len(item) for item in lines[:line])
    encoded = lines[line].encode("utf-16-le")
    byte_pos = min(max(character, 0) * 2, len(encoded))
    return prefix + len(encoded[:byte_pos].decode("utf-16-le"))


def _atomic_replace(target: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    tmp_path = Path(tmp)
    try:
        os.write(fd, data)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        if target.exists():
            os.chmod(tmp_path, stat.S_IMODE(target.stat().st_mode))
        os.replace(tmp_path, target)
    except Exception:
        if fd >= 0:
            os.close(fd)
        tmp_path.unlink(missing_ok=True)
        raise


def _atomic_create(target: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    tmp_path = Path(tmp)
    try:
        os.write(fd, data)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.chmod(tmp_path, 0o644)
        os.link(tmp_path, target)
    except FileExistsError:
        if fd >= 0:
            os.close(fd)
        tmp_path.unlink(missing_ok=True)
        raise EditError(f"error: file already exists: {target.name}")
    except Exception:
        if fd >= 0:
            os.close(fd)
        tmp_path.unlink(missing_ok=True)
        raise
    else:
        tmp_path.unlink(missing_ok=True)


def _ensure_parents(workspace: Path, resolved: Path) -> list[str]:
    workspace = workspace.resolve()
    created: list[str] = []
    missing: list[Path] = []
    current = resolved.parent
    while current != workspace and not current.exists():
        missing.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    for directory in reversed(missing):
        rel = relative_posix(workspace, directory)
        guard_write_path(workspace, rel)
        directory.mkdir(exist_ok=True)
        created.append(rel)
    return created


def _session_id(ctx: ToolContext) -> str:
    return ctx.session_id or "none"


def _tracker_check(ctx: ToolContext, src: FileSource) -> None:
    if ctx.files is None:
        return
    recorded = ctx.files.get(src.rel)
    if recorded is None:
        raise EditError(
            f"error: read {src.rel} before editing it"
        )
    if recorded != src.raw_sha256:
        current_lines = src.text.count("\n") + (0 if src.text.endswith("\n") or not src.text else 1)
        raise EditError(
            f"error: {src.rel} changed on disk since you read it "
            f"(now {current_lines} lines); read_file again before editing"
        )


def _prepare(
    ctx: ToolContext,
    path: str,
    mutate: Callable[[FileSource], str],
    *,
    creating: bool = False,
    check_stale: bool = True,
) -> PreparedEdit:
    resolved = guard_write_path(ctx.workspace, path)
    created_dirs: list[str] = []
    if creating:
        if resolved.exists():
            raise EditError(f"error: file already exists: {relative_posix(ctx.workspace, resolved)}")
        created_dirs = _ensure_parents(ctx.workspace, resolved)
        newline, trailing, encoding, indent = identity_from_sibling(
            resolved.parent, resolved.suffix
        )
        src = synthetic_source(
            ctx.workspace, resolved, "", newline, trailing, encoding, indent
        )
        try:
            new_text = mutate(src)
        except ValueError as exc:
            raise EditError(
                str(exc) if str(exc).startswith("error:") else f"error: {exc}"
            ) from exc
        gate = syntax_gate(src.rel, new_text, None)
        if gate:
            for directory in reversed(created_dirs):
                _rmdir_if_empty(ctx.workspace / directory)
            raise EditError(gate)
        new_bytes = render(src, new_text)
        diff = format_unified_diff("", new_text, src.rel)
        return PreparedEdit(
            src=src,
            new_text=new_text,
            new_bytes=new_bytes,
            before_bytes=None,
            before_sha=None,
            after_sha=sha256_bytes(new_bytes),
            diff=diff,
            created_dirs=created_dirs,
            is_create=True,
        )

    src = read_source(ctx.workspace, path)
    if check_stale:
        _tracker_check(ctx, src)
    try:
        new_text = mutate(src)
    except ValueError as exc:
        raise EditError(
            str(exc) if str(exc).startswith("error:") else f"error: {exc}"
        ) from exc
    if new_text == src.text:
        return PreparedEdit(
            src=src,
            new_text=new_text,
            new_bytes=render(src, new_text),
            before_bytes=src.resolved.read_bytes(),
            before_sha=src.raw_sha256,
            after_sha=src.raw_sha256,
            diff="",
            noop=True,
        )
    gate = syntax_gate(src.rel, new_text, src.text)
    if gate:
        raise EditError(gate)
    new_bytes = render(src, new_text)
    before_bytes = src.resolved.read_bytes()
    diff = format_unified_diff(src.text, new_text, src.rel)
    return PreparedEdit(
        src=src,
        new_text=new_text,
        new_bytes=new_bytes,
        before_bytes=before_bytes,
        before_sha=src.raw_sha256,
        after_sha=sha256_bytes(new_bytes),
        diff=diff,
    )


def _commit(
    ctx: ToolContext,
    prepared: PreparedEdit,
    tool_name: str,
    batch_id: str,
) -> ApplyResult:
    if prepared.noop:
        return ApplyResult(
            ok=True,
            message=f"{prepared.src.rel}: no changes",
            rel=prepared.src.rel,
            diff="",
            new_text=prepared.new_text,
        )
    target = prepared.src.resolved
    if prepared.is_create:
        _atomic_create(target, prepared.new_bytes)
    else:
        _atomic_replace(target, prepared.new_bytes)
    edit_id = None
    if ctx.journal is not None:
        edit_id = journal.record(
            Path(ctx.journal),
            session_id=_session_id(ctx),
            batch_id=batch_id,
            path=prepared.src.rel,
            tool=tool_name,
            before=prepared.before_bytes,
            after=prepared.new_bytes,
            before_sha=prepared.before_sha,
            after_sha=prepared.after_sha,
            diff=prepared.diff,
            created_dirs=prepared.created_dirs,
        )
    if ctx.files is not None:
        ctx.files.mark(prepared.src.rel, prepared.after_sha)
    return ApplyResult(
        ok=True,
        message="",
        rel=prepared.src.rel,
        diff=prepared.diff,
        edit_id=edit_id,
        new_text=prepared.new_text,
        created=prepared.is_create,
    )


def _apply_sync(
    ctx: ToolContext,
    path: str,
    mutate: Callable[[FileSource], str],
    tool_name: str,
    *,
    creating: bool = False,
    batch_id: str | None = None,
) -> ApplyResult:
    """Await-free write core. Must never await."""
    try:
        prepared = _prepare(ctx, path, mutate, creating=creating)
        return _commit(ctx, prepared, tool_name, batch_id or uuid4().hex)
    except (EditError, WorkspacePathError, FileNotFoundError, OSError) as exc:
        return ApplyResult(ok=False, message=str(exc) if str(exc).startswith("error:") else f"error: {exc}")


def _format_success(result: ApplyResult, extra: str = "") -> str:
    header = f"ok: {'created' if result.created else 'edited'} {result.rel}"
    diff = result.diff
    if len(diff) > DIFF_RESULT_MAX:
        diff = diff[:DIFF_RESULT_MAX] + "\n...[diff truncated]"
    parts = [header]
    if diff:
        parts.append(diff)
    else:
        parts.append("(no textual diff)")
    if extra:
        parts.append(extra)
    return "\n".join(parts)


def _new_diagnostics(before: list, after: list) -> list:
    def key(item: dict) -> tuple:
        start = item.get("range", {}).get("start", {})
        return (
            start.get("line"),
            start.get("character"),
            item.get("message"),
            item.get("severity"),
        )

    seen = {key(item) for item in before}
    return [item for item in after if key(item) not in seen]


def _format_new_diags(rel: str, diags: list) -> str:
    if not diags:
        return "no new diagnostics"
    names = {1: "Error", 2: "Warning", 3: "Info", 4: "Hint"}
    lines = ["new diagnostics:"]
    for item in diags:
        start = item.get("range", {}).get("start", {})
        line_no = start.get("line", -1) + 1
        col_no = start.get("character", -1) + 1
        sev = names.get(item.get("severity"), "Diagnostic")
        msg = (item.get("message") or "").strip()
        source = item.get("source")
        prefix = f"[{source}] " if source else ""
        lines.append(f"{rel}:{line_no}:{col_no} {sev}: {prefix}{msg}")
    return "\n".join(lines)


def _lsp_after(ctx: ToolContext, result: ApplyResult, before: list) -> str:
    lsp = ctx.lsp
    if lsp is None or not result.ok:
        return ""
    try:
        after = lsp.diagnostics_after_change(result.rel, result.new_text)
    except Exception as exc:  # noqa: BLE001
        return f"lsp diagnostics: error: {exc}"
    return _format_new_diags(result.rel, _new_diagnostics(before, after))


async def apply_edit(
    ctx: ToolContext,
    path: str,
    mutate: Callable[[FileSource], str],
    tool_name: str,
    *,
    creating: bool = False,
) -> str:
    before: list = []
    if ctx.lsp is not None and not creating:
        try:
            before = ctx.lsp.cached_diagnostics(path)
        except Exception:  # noqa: BLE001
            before = []
    result = _apply_sync(ctx, path, mutate, tool_name, creating=creating)
    if not result.ok:
        return result.message
    extra = ""
    if ctx.lsp is not None and result.diff:
        extra = await asyncio.to_thread(_lsp_after, ctx, result, before)
    if ctx.on_edit is not None and result.diff:
        ctx.on_edit(result.rel, result.diff, tool_name, result.edit_id)
    return _format_success(result, extra)


def _apply_workspace_sync(
    ctx: ToolContext,
    edits_by_path: list[tuple[str, list[TextEdit]]],
    tool_name: str,
) -> tuple[list[ApplyResult], str | None]:
    """Await-free: prepare every file, then commit all, or write nothing."""
    batch_id = uuid4().hex
    prepared: list[tuple[str, PreparedEdit]] = []
    try:
        for path, edits in edits_by_path:
            item = _prepare(
                ctx,
                path,
                lambda src, captured=edits: apply_text_edits(src.text, captured),
                check_stale=False,
            )
            prepared.append((path, item))
    except (EditError, WorkspacePathError, FileNotFoundError, OSError) as exc:
        msg = str(exc) if str(exc).startswith("error:") else f"error: {exc}"
        return [], msg
    results = []
    committed: list[ApplyResult] = []
    try:
        for path, item in prepared:
            result = _commit(ctx, item, tool_name, batch_id)
            committed.append(result)
            results.append(result)
    except (EditError, OSError) as exc:
        _rollback_committed(ctx, committed)
        msg = str(exc) if str(exc).startswith("error:") else f"error: {exc}"
        return [], msg
    return results, None


def _rollback_committed(ctx: ToolContext, committed: list[ApplyResult]) -> None:
    db = Path(ctx.journal) if ctx.journal is not None else None
    for result in reversed(committed):
        if not result.ok or result.rel == "":
            continue
        records = []
        if db is not None and result.edit_id is not None:
            records = [
                item
                for item in journal.recent(db, _session_id(ctx), limit=50)
                if item.id == result.edit_id
            ]
        if not records:
            continue
        rec = records[0]
        target = ctx.workspace / rec.path
        try:
            if rec.before is None:
                target.unlink(missing_ok=True)
            else:
                _atomic_replace(target, rec.before)
        except OSError:
            pass


async def apply_workspace_edit(
    ctx: ToolContext,
    edits_by_path: list[tuple[str, list[TextEdit]]],
    tool_name: str,
) -> str:
    results, err = _apply_workspace_sync(ctx, edits_by_path, tool_name)
    if err:
        return err
    extras: list[str] = []
    for result in results:
        extra = ""
        if ctx.lsp is not None and result.diff:
            extra = await asyncio.to_thread(_lsp_after, ctx, result, [])
        if ctx.on_edit is not None and result.diff:
            ctx.on_edit(result.rel, result.diff, tool_name, result.edit_id)
        extras.append(_format_success(result, extra))
    if not extras:
        return "error: rename produced no edits"
    return "\n\n".join(extras)


def normalize_workspace_edit(workspace: Path, payload: dict) -> list[tuple[str, list[TextEdit]]]:
    grouped: dict[str, list[TextEdit]] = {}

    def add(uri: str, edits: list) -> None:
        rel = _uri_to_rel(workspace, uri)
        grouped.setdefault(rel, []).extend(_parse_text_edits(edits))

    changes = payload.get("changes") or {}
    for uri, edits in changes.items():
        add(uri, edits or [])
    for item in payload.get("documentChanges") or []:
        if not isinstance(item, dict):
            continue
        if "edits" in item:
            uri = (item.get("textDocument") or {}).get("uri") or ""
            add(uri, item.get("edits") or [])
    return list(grouped.items())


def _uri_to_rel(workspace: Path, uri: str) -> str:
    from urllib.parse import unquote, urlparse

    parsed = urlparse(uri)
    full = Path(unquote(parsed.path))
    guard_write_path(workspace, str(full))
    return relative_posix(workspace, full.resolve())


def _parse_text_edits(edits: list) -> list[TextEdit]:
    out: list[TextEdit] = []
    for item in edits:
        rng = item.get("range") or {}
        start = rng.get("start") or {}
        end = rng.get("end") or {}
        out.append(
            TextEdit(
                start_line=int(start.get("line", 0)),
                start_char=int(start.get("character", 0)),
                end_line=int(end.get("line", 0)),
                end_char=int(end.get("character", 0)),
                new_text=item.get("newText") or "",
            )
        )
    return out


def _rmdir_if_empty(path: Path) -> None:
    try:
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    except OSError:
        pass


def undo_last_sync(ctx: ToolContext) -> ApplyResult:
    """Await-free undo of the most recent journal batch."""
    if ctx.journal is None:
        return ApplyResult(ok=False, message="error: edit journal is not available")
    db = Path(ctx.journal)
    batch = journal.last_batch(db, _session_id(ctx))
    if not batch:
        return ApplyResult(ok=False, message="error: no edits to undo")
    for rec in reversed(batch):
        target = ctx.workspace / rec.path
        current_sha = sha256_bytes(target.read_bytes()) if target.is_file() else None
        if rec.after is None:
            if target.exists():
                return ApplyResult(
                    ok=False,
                    message=f"error: {rec.path} exists; expected it to be deleted",
                )
        else:
            if current_sha != rec.after_sha:
                return ApplyResult(
                    ok=False,
                    message=(
                        f"error: {rec.path} changed on disk since the edit; "
                        "refusing to clobber"
                    ),
                )
    undo_batch = uuid4().hex
    diffs: list[str] = []
    last_id = None
    last_rel = ""
    for rec in reversed(batch):
        target = ctx.workspace / rec.path
        if rec.before is None:
            before_now = target.read_bytes() if target.is_file() else None
            before_sha = sha256_bytes(before_now) if before_now is not None else None
            target.unlink()
            for directory in reversed(rec.created_dirs):
                _rmdir_if_empty(ctx.workspace / directory)
            after_bytes = None
            after_sha = None
            old_text = before_now.decode("utf-8", errors="replace") if before_now else ""
            new_text = ""
        elif rec.after is None:
            _ensure_parents(ctx.workspace, target)
            _atomic_create(target, rec.before)
            before_now = None
            before_sha = None
            after_bytes = rec.before
            after_sha = rec.before_sha
            old_text = ""
            new_text = rec.before.decode("utf-8", errors="replace")
        else:
            before_now = target.read_bytes()
            _atomic_replace(target, rec.before)
            before_sha = rec.after_sha
            after_bytes = rec.before
            after_sha = rec.before_sha
            old_text = rec.after.decode("utf-8", errors="replace")
            new_text = rec.before.decode("utf-8", errors="replace")
        diff = format_unified_diff(
            old_text.replace("\r\n", "\n").replace("\r", "\n"),
            new_text.replace("\r\n", "\n").replace("\r", "\n"),
            rec.path,
        )
        last_id = journal.record(
            db,
            session_id=_session_id(ctx),
            batch_id=undo_batch,
            path=rec.path,
            tool="undo_edit",
            before=before_now if rec.before is None or rec.after is not None else None,
            after=after_bytes,
            before_sha=before_sha,
            after_sha=after_sha,
            diff=diff,
            created_dirs=[],
        )
        if ctx.files is not None and after_sha:
            ctx.files.mark(rec.path, after_sha)
        elif ctx.files is not None and rec.before is None:
            ctx.files.mark(rec.path, "")
        diffs.append(diff or f"{rec.path}: undone")
        last_rel = rec.path
    return ApplyResult(
        ok=True,
        message="",
        rel=last_rel,
        diff="\n".join(diffs),
        edit_id=last_id,
        new_text="",
    )


async def undo_last(ctx: ToolContext) -> str:
    result = undo_last_sync(ctx)
    if not result.ok:
        return result.message
    extra = ""
    if ctx.lsp is not None and result.rel:
        try:
            src = read_source(ctx.workspace, result.rel)
            result.new_text = src.text
            extra = await asyncio.to_thread(_lsp_after, ctx, result, [])
        except (FileNotFoundError, WorkspacePathError, OSError):
            extra = ""
    if ctx.on_edit is not None:
        ctx.on_edit(result.rel, result.diff, "undo_edit", result.edit_id)
    header = f"ok: undone {result.rel}"
    body = result.diff
    if len(body) > DIFF_RESULT_MAX:
        body = body[:DIFF_RESULT_MAX] + "\n...[diff truncated]"
    parts = [header, body]
    if extra:
        parts.append(extra)
    return "\n".join(parts)


def list_edits_text(ctx: ToolContext, limit: int = 20) -> str:
    if ctx.journal is None:
        return "error: edit journal is not available"
    rows = journal.recent(Path(ctx.journal), _session_id(ctx), limit=limit)
    if not rows:
        return "(no edits this session)"
    lines = []
    for rec in rows:
        kind = "create" if rec.before is None else ("delete" if rec.after is None else "edit")
        lines.append(f"#{rec.id}  {kind}  {rec.path}  {rec.tool}  {rec.applied_at}")
        if rec.diff:
            snippet = rec.diff if len(rec.diff) <= 500 else rec.diff[:500] + "\n...[truncated]"
            lines.append(snippet)
            lines.append("")
    return "\n".join(lines).rstrip()
