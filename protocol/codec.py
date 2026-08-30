from __future__ import annotations

import json
from typing import Any

from protocol.commands import COMMANDS, Command
from protocol.events import EVENTS, Event


class ProtocolError(ValueError):
    pass


def encode(msg: Command | Event) -> bytes:
    return (json.dumps(msg.to_json()) + "\n").encode()


def decode_command(line: str | bytes) -> Command:
    data = _parse(line)
    return _dispatch(COMMANDS, data, kind="command")


def decode_event(line: str | bytes) -> Event:
    data = _parse(line)
    return _dispatch(EVENTS, data, kind="event")


def _parse(line: str | bytes) -> dict[str, Any]:
    if isinstance(line, bytes):
        line = line.decode()
    text = line.strip()
    if not text:
        raise ProtocolError("empty line")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ProtocolError("JSON payload must be an object")
    return data


def _dispatch(registry: dict[str, type], data: dict[str, Any], kind: str):
    type_name = data.get("type")
    if not type_name:
        raise ProtocolError(f"{kind} is missing a type field")
    cls = registry.get(type_name)
    if cls is None:
        raise ProtocolError(f"unknown {kind} type: {type_name}")
    return cls.from_json(data)
