from __future__ import annotations

from protocol.commands import CloseFile, OpenFile
from protocol.events import ErrorOccurred, FileClosed, FileContent
from runtime.commands.register import handles
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
        rel, content = read_text(session._workspace, command.path)
    except FileNotFoundError:
        session._emit(ErrorOccurred(message=f"file not found: {command.path}"))
        return
    except WorkspacePathError as exc:
        session._emit(ErrorOccurred(message=str(exc)))
        return
    if rel not in session._state.open_files:
        session._state.open_files.append(rel)
    session._persist()
    session._emit(FileContent(path=rel, content=content))


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
