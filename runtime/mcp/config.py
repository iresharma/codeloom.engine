from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MCP_PROFILES = ("researcher", "debugger")
_ENV = re.compile(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}")
_INTERP = re.compile(r"\$\{(env:[A-Za-z_][A-Za-z0-9_]*|workspaceFolder|userHome)\}")


@dataclass
class McpServerConfig:
    name: str
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    enabled: bool = True
    profiles: list[str] | None = None
    token_env: str | None = None
    safe_tools: list[str] = field(default_factory=list)
    source: str = "engine"
    unresolved: str | None = None
    transport: str = "stdio"


def _interpolate(value: str, workspace: Path, home: Path) -> str:
    def repl(match: re.Match) -> str:
        token = match.group(1)
        if token == "workspaceFolder":
            return str(workspace)
        if token == "userHome":
            return str(home)
        if token.startswith("env:"):
            name = token[4:]
            got = os.environ.get(name)
            if got is None:
                raise KeyError(name)
            return got
        return match.group(0)

    return _INTERP.sub(repl, value)


def _walk_strings(value, workspace: Path, home: Path):
    if isinstance(value, str):
        return _interpolate(value, workspace, home)
    if isinstance(value, list):
        return [_walk_strings(item, workspace, home) for item in value]
    if isinstance(value, dict):
        return {key: _walk_strings(item, workspace, home) for key, item in value.items()}
    return value


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def load_trust(workspace: Path) -> dict:
    path = workspace / ".engine" / "mcp-trust.json"
    data = _read_json(path) if path.is_file() else {}
    cursor = data.get("cursor")
    if isinstance(cursor, list):
        return {"cursor": [str(item) for item in cursor], "refused": list(data.get("refused") or [])}
    return {"cursor": [], "refused": list(data.get("refused") or [])}


def save_trust(workspace: Path, cursor: list[str], refused: list[str] | None = None) -> Path:
    dest = workspace / ".engine" / "mcp-trust.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = {"cursor": list(cursor)}
    if refused:
        payload["refused"] = list(refused)
    dest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return dest


def _entry(name: str, raw: dict, source: str, workspace: Path, home: Path) -> McpServerConfig:
    unresolved = None
    try:
        raw = _walk_strings(raw, workspace, home)
    except KeyError as exc:
        unresolved = str(exc.args[0])
        raw = dict(raw)
    profiles = raw.get("profiles")
    if profiles is not None:
        profiles = [str(item) for item in profiles]
    safe = raw.get("safeTools") or raw.get("safe_tools") or []
    token_env = raw.get("tokenEnv") or raw.get("token_env")
    enabled = raw.get("enabled")
    if enabled is None:
        enabled = True
    return McpServerConfig(
        name=name,
        command=str(raw.get("command") or ""),
        args=[str(item) for item in (raw.get("args") or [])],
        env={str(key): str(value) for key, value in (raw.get("env") or {}).items()},
        cwd=str(raw["cwd"]) if raw.get("cwd") else None,
        enabled=bool(enabled),
        profiles=profiles,
        token_env=str(token_env) if token_env else None,
        safe_tools=[str(item) for item in safe],
        source=source,
        unresolved=unresolved,
        transport=str(raw.get("type") or raw.get("transport") or "stdio"),
    )


def load_mcp_config(
    workspace: Path,
    *,
    home: Path | None = None,
    trust_cursor: list[str] | None = None,
) -> tuple[list[McpServerConfig], list[str], list[str]]:
    """Return (servers, warnings, untrusted_cursor_names)."""
    workspace = Path(workspace).resolve()
    home = Path(home or Path.home())
    engine_path = workspace / ".engine" / "mcp.json"
    cursor_path = workspace / ".cursor" / "mcp.json"
    servers: dict[str, McpServerConfig] = {}
    warnings: list[str] = []
    untrusted: list[str] = []
    trusted = set(trust_cursor) if trust_cursor is not None else set(load_trust(workspace).get("cursor") or [])
    refused = set(load_trust(workspace).get("refused") or [])

    if engine_path.is_file():
        payload = _read_json(engine_path)
        for name, raw in (payload.get("mcpServers") or {}).items():
            if not isinstance(raw, dict):
                continue
            servers[name] = _entry(str(name), raw, "engine", workspace, home)

    if cursor_path.is_file():
        payload = _read_json(cursor_path)
        imported = []
        for name, raw in (payload.get("mcpServers") or {}).items():
            name = str(name)
            if name in servers:
                continue
            if not isinstance(raw, dict):
                continue
            imported.append(name)
            cfg = _entry(name, raw, "cursor", workspace, home)
            if name in refused:
                cfg.enabled = False
                servers[name] = cfg
                continue
            if name not in trusted:
                cfg.enabled = False
                untrusted.append(name)
            servers[name] = cfg
        if imported:
            warnings.append(
                "imported MCP servers from .cursor/mcp.json: " + ", ".join(imported)
            )

    return list(servers.values()), warnings, untrusted
