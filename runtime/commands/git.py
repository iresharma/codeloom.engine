from __future__ import annotations

from protocol.commands import RequestGit
from protocol.events import GitStateUpdated, WarningOccurred
from runtime.commands.register import handles
from runtime.subscriber import EVENT_SOFT_LIMIT, clip_text
from runtime.tools.git import read_state as read_git


@handles(RequestGit)
def request_git(session, command: RequestGit) -> None:
    if not session._require_session():
        return
    git = read_git(session._workspace)
    limit = EVENT_SOFT_LIMIT // 2
    staged, omitted_s = clip_text(git.staged_diff, limit)
    unstaged, omitted_u = clip_text(git.unstaged_diff, limit)
    git.staged_diff = staged
    git.unstaged_diff = unstaged
    session._emit(GitStateUpdated(git=git))
    omitted = omitted_s + omitted_u
    if omitted:
        session._emit(
            WarningOccurred(message=f"git diffs truncated; {omitted} bytes omitted")
        )
