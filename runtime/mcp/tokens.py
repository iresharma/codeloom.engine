from __future__ import annotations

import json
import os
from pathlib import Path


def tokens_path(workspace: Path) -> Path:
    return Path(workspace) / ".engine" / "mcp-tokens.json"


def load_tokens(workspace: Path) -> dict[str, dict]:
    path = tokens_path(workspace)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for name, row in data.items():
        if isinstance(row, dict) and row.get("token"):
            out[str(name)] = {
                "token": str(row["token"]),
                "tokenEnv": str(row.get("tokenEnv") or row.get("token_env") or ""),
            }
    return out


def save_token(workspace: Path, server: str, token: str, token_env: str) -> Path:
    path = tokens_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = load_tokens(workspace)
    data[server] = {"token": token, "tokenEnv": token_env}
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def apply_tokens(configs, tokens: dict[str, dict]) -> None:
    for cfg in configs:
        row = tokens.get(cfg.name)
        if not row:
            continue
        env_name = row.get("tokenEnv") or cfg.token_env
        if not env_name:
            continue
        cfg.env = dict(cfg.env)
        cfg.env[env_name] = row["token"]
        if not cfg.token_env:
            cfg.token_env = env_name
