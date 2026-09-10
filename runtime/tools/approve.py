from __future__ import annotations


async def require_approval(ctx, question: str) -> str:
    """Return an error string if the user denies, else empty.

    `never` skips the prompt. `always`/`auto` ask when ask_user is set.
    No ask_user: `always` denies, `auto` allows.
    """
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
    if approval not in {"always", "auto"}:
        return ""
    answer = await ask(question, kind="confirm")
    if str(answer).strip().lower() not in {"yes", "y"}:
        return "error: user denied"
    return ""
