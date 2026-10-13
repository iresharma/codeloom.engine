from protocol.commands import RequestRunState
from protocol.events import RunStateReady
from runtime.commands.register import handles


@handles(RequestRunState)
async def request_run_state(session, command):
    loop = session._loop
    children = sum(not task.done() for task in getattr(loop, "_child_tasks", {}).values())
    settlements = sum(not task.done() for task in getattr(loop, "_settle_tasks", {}).values())
    queued = len(session._pending_user) + len(session._inbox)
    prompt = session._prompts.pending() is not None
    turn = session._turn_task is not None and not session._turn_task.done()
    session._emit(RunStateReady(
        idle=not (turn or children or settlements or queued or prompt),
        live_children=children, live_settlements=settlements,
        queued_messages=queued, pending_prompt=prompt,
    ))
