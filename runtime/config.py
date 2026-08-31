from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from llm.openrouter import load_env_sh

EXEC_APPROVALS = ("auto", "always", "never")


@dataclass
class EngineConfig:
    llm_stream: bool = True
    llm_timeout_s: float = 600.0
    stream_idle_s: float = 90.0
    max_turns: int = 16
    exec_approval: str = "auto"
    exec_timeout_s: int = 120
    exec_file_limit_mb: int = 2048
    context_budget: int = 120_000
    subscriber_capacity: int = 4096
    subscriber_bytes: int = 1 << 20
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls, workspace: Path) -> EngineConfig:
        load_env_sh(workspace / "env.sh")
        config = cls()
        warnings: list[str] = []
        config.llm_stream = _env_bool("ENGINE_LLM_STREAM", config.llm_stream, warnings)
        config.llm_timeout_s = _env_float(
            "ENGINE_LLM_TIMEOUT_S", config.llm_timeout_s, warnings
        )
        config.stream_idle_s = _env_float(
            "ENGINE_LLM_IDLE_S", config.stream_idle_s, warnings
        )
        config.max_turns = _env_int("ENGINE_MAX_TURNS", config.max_turns, warnings)
        config.exec_timeout_s = _env_int(
            "ENGINE_EXEC_TIMEOUT_S", config.exec_timeout_s, warnings
        )
        config.exec_file_limit_mb = _env_int(
            "ENGINE_EXEC_FILE_LIMIT_MB", config.exec_file_limit_mb, warnings
        )
        config.context_budget = _env_int(
            "ENGINE_CONTEXT_BUDGET", config.context_budget, warnings
        )
        config.subscriber_capacity = _env_int(
            "ENGINE_SUBSCRIBER_CAPACITY", config.subscriber_capacity, warnings
        )
        config.subscriber_bytes = _env_int(
            "ENGINE_SUBSCRIBER_BYTES", config.subscriber_bytes, warnings
        )
        raw_approval = os.environ.get("ENGINE_EXEC_APPROVAL", config.exec_approval)
        approval = (raw_approval or "").strip().lower()
        if approval not in EXEC_APPROVALS:
            warnings.append(
                f"ENGINE_EXEC_APPROVAL={raw_approval!r} is not auto|always|never; "
                "using auto"
            )
            approval = "auto"
        config.exec_approval = approval
        config.warnings = warnings
        return config


def _env_int(name: str, default: int, warnings: list[str]) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        warnings.append(f"{name}={raw!r} is not an integer; using {default}")
        return default


def _env_float(name: str, default: float, warnings: list[str]) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.strip())
    except ValueError:
        warnings.append(f"{name}={raw!r} is not a number; using {default}")
        return default


def _env_bool(name: str, default: bool, warnings: list[str]) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    warnings.append(f"{name}={raw!r} is not a boolean; using {default}")
    return default
