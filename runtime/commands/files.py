from __future__ import annotations

from protocol.commands import (
    CloseFile,
    CreatePath,
    DeletePath,
    OpenFile,
    RenamePath,
    UndoLastEdit,
)
from protocol.events import ErrorOccurred, FileClosed, PathChanged
from runtime.commands.register import handles
from runtime.tools.edits import (
    apply_edit,
    delete_path,
    mkdir_path,
    rename_path,
    undo_last,
)
from runtime.tools.fs import (
    WorkspacePathError,
    read_text,
    relative_posix,
    resolve_in_workspace,
)


@handles(OpenFile)
def open_file(session, command: OpenFile) -> None:
    if not session._require_session():
        return
    try:
        rel, _content = read_text(session._workspace, command.path)
    except FileNotFoundError:
        session._emit(ErrorOccurred(message=f"file not found: {command.path}"))
        return
    except WorkspacePathError as exc:
        session._emit(ErrorOccurred(message=str(exc)))
        return
    if rel not in session._state.open_files:
        session._state.open_files.append(rel)
    session._persist()
    session._emit_file_content(rel, full=True)


@handles(CloseFile)
def close_file(session, command: CloseFile) -> None:
    if not session._require_session():
        return
    try:
        rel = relative_posix(
            session._workspace,
            resolve_in_workspace(session._workspace, command.path),
        )
    except WorkspacePathError as exc:
        session._emit(ErrorOccurred(message=str(exc)))
        return
    if rel not in session._state.open_files:
        session._emit(ErrorOccurred(message=f"file is not open: {command.path}"))
        return
    session._state.open_files.remove(rel)
    session._persist()
    session._emit(FileClosed(path=rel))


@handles(UndoLastEdit)
async def undo_last_edit(session, command: UndoLastEdit) -> None:
    if not session._require_session():
        return
    ctx = session.tool_context()
    text = await undo_last(ctx)
    if text.startswith("error:"):
        session._emit(ErrorOccurred(message=text))


@handles(CreatePath)
async def create_path(session, command: CreatePath) -> None:
    if not session._require_session():
        return
    ctx = session.tool_context()
    path = (command.path or "").strip()
    if not path:
        session._emit(ErrorOccurred(message="path is required"))
        return
    if command.is_dir:
        text = await mkdir_path(ctx, path)
        if text.startswith("error:"):
            session._emit(ErrorOccurred(message=text))
            return
        session._emit(PathChanged(path=path, action="mkdir"))
        session._emit_tree()
        session._emit_git()
        return

    async def run():
        return await apply_edit(
            ctx, path, lambda src: command.content or "", "create_path", creating=True
        )

    text = await run()
    if text.startswith("error:"):
        session._emit(ErrorOccurred(message=text))


@handles(RenamePath)
async def rename_path_cmd(session, command: RenamePath) -> None:
    if not session._require_session():
        return
    ctx = session.tool_context()
    src = (command.src or "").strip()
    dest = (command.dest or "").strip()
    if not src or not dest:
        session._emit(ErrorOccurred(message="src and dest are required"))
        return
    text = await rename_path(ctx, src, dest)
    if text.startswith("error:"):
        session._emit(ErrorOccurred(message=text))
        return
    if src in session._state.open_files:
        session._state.open_files.remove(src)
        if dest not in session._state.open_files:
            session._state.open_files.append(dest)
        session._persist()
    session._emit(PathChanged(path=src, action="renamed", dest=dest))
    session._emit_tree()
    session._emit_git()


@handles(DeletePath)
async def delete_path_cmd(session, command: DeletePath) -> None:
    if not session._require_session():
        return
    ctx = session.tool_context()
    path = (command.path or "").strip()
    if not path:
        session._emit(ErrorOccurred(message="path is required"))
        return
    text = await delete_path(ctx, path)
    if text.startswith("error:"):
        session._emit(ErrorOccurred(message=text))
        return
    if path in session._state.open_files:
        session._state.open_files.remove(path)
        session._persist()
        session._emit(FileClosed(path=path))
    session._emit(PathChanged(path=path, action="deleted"))
    session._emit_tree()
    session._emit_git()
