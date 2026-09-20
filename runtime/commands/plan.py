from __future__ import annotations

from protocol.commands import SetPlanMode
from protocol.events import PlanModeChanged
from runtime.commands.register import handles


@handles(SetPlanMode)
def set_plan_mode(session, command: SetPlanMode) -> None:
    if not session._require_session():
        return
    session.plan_mode = bool(command.enabled)
    session._emit(PlanModeChanged(enabled=session.plan_mode))
