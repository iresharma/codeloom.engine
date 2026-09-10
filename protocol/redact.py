from __future__ import annotations

from protocol.commands import CompleteMcpAuth


def redact_command(command) -> dict:
    """Copy of command JSON with secrets stripped for logs."""
    data = command.to_json() if hasattr(command, "to_json") else dict(command)
    if isinstance(command, CompleteMcpAuth) or data.get("type") == "CompleteMcpAuth":
        data = dict(data)
        if "token" in data:
            data["token"] = "<redacted>"
    return data
