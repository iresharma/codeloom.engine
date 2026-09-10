from __future__ import annotations

from protocol.commands import (
    ActivateSkill,
    CompleteMcpAuth,
    ReloadIntegrations,
    SetMcpEnabled,
)
from protocol.events import ErrorOccurred
from runtime.commands.register import handles


@handles(ReloadIntegrations)
async def reload_integrations(session, command: ReloadIntegrations) -> None:
    if not session._require_session():
        return
    await session.reload_integrations()


@handles(SetMcpEnabled)
async def set_mcp_enabled(session, command: SetMcpEnabled) -> None:
    if not session._require_session():
        return
    session.set_mcp_enabled(command.name, command.enabled)
    await session.reload_integrations()


@handles(ActivateSkill)
def activate_skill(session, command: ActivateSkill) -> None:
    if not session._require_session():
        return
    if not session.unlock_skill(command.name):
        session._emit(ErrorOccurred(message=f"unknown skill: {command.name}"))


@handles(CompleteMcpAuth)
async def complete_mcp_auth(session, command: CompleteMcpAuth) -> None:
    if not session._require_session():
        return
    await session.complete_mcp_auth(command.server, command.token)
