from __future__ import annotations

from protocol.commands import RequestGit
from protocol.events import GitStateUpdated
from runtime.commands.register import handles
from runtime.tools.git import read_state as read_git


@handles(RequestGit)
def request_git(session, command: RequestGit) -> None:
    if not session._require_session():
        return
    session._emit(GitStateUpdated(git=read_git(session._workspace)))
