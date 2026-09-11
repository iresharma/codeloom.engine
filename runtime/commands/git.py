from __future__ import annotations

from protocol.commands import RequestGit
from runtime.commands.register import handles


@handles(RequestGit)
def request_git(session, command: RequestGit) -> None:
    if not session._require_session():
        return
    session._emit_git()
