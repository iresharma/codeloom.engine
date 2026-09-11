from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from llm.openrouter import load_env_sh

EXEC_APPROVALS = ("auto", "always", "never")
TURN_CONTINUES = ("prompt", "never")
# 2.0 never fires in-loop; overflow still force-compacts. Must ship with
# github_file windows or surveys hit the 120k fuse.
CHILD_COMPACT_TRIGGER = 2.0
CHILD_KEEP_FULL_TOOLS = 10


@dataclass
class EngineConfig:
    llm_stream: bool = True
    llm_timeout_s: float = 600.0
    stream_idle_s: float = 90.0
    max_turns: int = 16
    turn_slice: int = 16
    max_continues: int = 3
    turn_continue: str = "prompt"
    exec_approval: str = "auto"
    exec_timeout_s: int = 120
    exec_file_limit_mb: int = 2048
    context_budget: int = 120_000
    compact_trigger: float = 0.7
    keep_full_tools: int = 3
    child_model: str = ""
    max_spawns_per_turn: int = 8
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
        config.turn_slice = _env_int("ENGINE_TURN_SLICE", config.turn_slice, warnings)
        config.max_continues = _env_int(
            "ENGINE_MAX_CONTINUES", config.max_continues, warnings
        )
        config.exec_timeout_s = _env_int(
            "ENGINE_EXEC_TIMEOUT_S", config.exec_timeout_s, warnings
        )
        config.exec_file_limit_mb = _env_int(
            "ENGINE_EXEC_FILE_LIMIT_MB", config.exec_file_limit_mb, warnings
        )
        config.context_budget = _env_int(
            "ENGINE_CONTEXT_BUDGET", config.context_budget, warnings
        )
        config.child_model = (
            os.environ.get("OPENROUTER_CHILD_MODEL") or ""
        ).strip()
        config.max_spawns_per_turn = _env_int(
            "ENGINE_MAX_SPAWNS_PER_TURN", config.max_spawns_per_turn, warnings
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
        raw_continue = os.environ.get("ENGINE_TURN_CONTINUE", config.turn_continue)
        continue_mode = (raw_continue or "").strip().lower()
        if continue_mode not in TURN_CONTINUES:
            warnings.append(
                f"ENGINE_TURN_CONTINUE={raw_continue!r} is not prompt|never; "
                "using prompt"
            )
            continue_mode = "prompt"
        config.turn_continue = continue_mode
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
