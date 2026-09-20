from __future__ import annotations

from tools.base import PLAN_MODE_ERROR, plan_mode_active


async def require_approval(ctx, question: str) -> str:
    """Return an error string if the user denies, else empty.

    `never` skips the prompt. `always`/`auto` ask when ask_user is set.
    No ask_user: `always` denies, `auto` allows.

    Mutating calls funnel through here (gh_pr_comment, gh_issue_create,
    gh_pr_create, non-GET/HEAD http_request), so this is also where plan
    mode denies them, mirroring the write funnel in runtime/tools/edits.py.
    """
    if plan_mode_active(ctx):
        return PLAN_MODE_ERROR
    approval = "auto"
    if ctx is not None and getattr(ctx, "config", None) is not None:
        approval = getattr(ctx.config, "exec_approval", "auto") or "auto"
    approval = str(approval).strip().lower()
    if approval == "never":
        return ""
    ask = getattr(ctx, "ask_user", None) if ctx is not None else None
    if ask is None:
        if approval == "always":
            return "error: user denied"
        return ""
    # Unknown values prompt (same as auto). Do not fail open.
    answer = await ask(question, kind="confirm")
    if str(answer).strip().lower() not in {"yes", "y"}:
        return "error: user denied"
    return ""
