from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from llm.openrouter import load_env_sh

EXEC_APPROVALS = ("auto", "always", "never", "judged")
TURN_CONTINUES = ("prompt", "never")
JUDGE_MODES = ("off", "advisory", "calibrated", "enforcing")
# Recommended production profile: enforce the calibrated read/exec sites,
# keep merge advisory. Per-site ENGINE_JUDGE_* still wins. There is no write
# or intent site: the judge does not gate edits or route turns.
CALIBRATED_SITE_MODES = {
    "exec": "enforcing",
    "tools": "advisory",
    "search": "enforcing",
    "screen": "enforcing",
    "compaction": "advisory",
    "diagnostics": "advisory",
    "loop": "advisory",
    "merge": "advisory",
    "brief": "advisory",
}
TYPESAFE_PLACEHOLDERS = {"", "...", "your-key", "changeme"}
JUDGE_SITES = (
    "exec",
    "tools",
    "search",
    "screen",
    "compaction",
    "diagnostics",
    "loop",
    "merge",
    "brief",
)
# 2.0 never fires in-loop; overflow still force-compacts. Must ship with
# github_file windows or surveys hit the 120k fuse.
CHILD_COMPACT_TRIGGER = 2.0
CHILD_KEEP_FULL_TOOLS = 10
# Coders/testers accumulate tool output. Leaving them on the survey trigger
# replays a growing transcript every turn (millions of cached tokens).
WRITER_COMPACT_TRIGGER = 0.7
WRITER_KEEP_FULL_TOOLS = 3


@dataclass
class EngineConfig:
    llm_stream: bool = True
    llm_timeout_s: float = 600.0
    stream_idle_s: float = 90.0
    max_turns: int = 16
    turn_slice: int = 16
    max_continues: int = 1
    # Safety fuse on executed tools (ask_user does not count). LLM rounds
    # are max_turns; this stops a single round from spraying unbounded calls.
    max_tool_calls: int = 50
    turn_continue: str = "prompt"
    exec_approval: str = "auto"
    exec_timeout_s: int = 120
    exec_file_limit_mb: int = 2048
    context_budget: int = 120_000
    compact_trigger: float = 0.7
    keep_full_tools: int = 3
    child_model: str = ""
    model_cheap: str = ""
    model_strong: str = ""
    max_spawns_per_turn: int = 8
    subscriber_capacity: int = 4096
    subscriber_bytes: int = 1 << 20
    pushgateway_url: str = ""
    metrics_job: str = "engine"
    metrics_instance: str = ""
    metrics_push_interval_s: float = 2.0
    trace_calls: bool = False
    warnings: list[str] = field(default_factory=list)
    typesafe_api_key: str = ""
    judge_mode: str = "advisory"
    judge_model: str = "jev-latest"
    judge_timeout_ms: int = 800
    judge_cache_size: int = 512
    loop_control_interval: int = 3
    judge_mode_exec: str = ""
    judge_mode_tools: str = ""
    judge_mode_search: str = ""
    judge_mode_screen: str = ""
    judge_mode_compaction: str = ""
    judge_mode_diagnostics: str = ""
    judge_mode_loop: str = ""
    judge_mode_merge: str = ""
    judge_mode_brief: str = ""
    # Opt-in pre-turn prompter. Independent of ENGINE_JUDGE: on means the
    # brief site may act even when the global judge is advisory.
    interview: bool = False

    def judge_mode_for(self, site: str) -> str:
        """Effective judge mode for a call site: its own override, or the
        global default."""
        if site not in JUDGE_SITES:
            return "off"
        override = getattr(self, f"judge_mode_{site}", "")
        if override:
            return override
        if self.judge_mode == "calibrated":
            return CALIBRATED_SITE_MODES.get(site, "advisory")
        return self.judge_mode

    @property
    def judge_usable(self) -> bool:
        return bool(self.typesafe_api_key) and self.judge_mode != "off"

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
        config.max_tool_calls = _env_int(
            "ENGINE_MAX_TOOL_CALLS", config.max_tool_calls, warnings
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
        config.model_cheap = (os.environ.get("ENGINE_MODEL_CHEAP") or "").strip()
        config.model_strong = (os.environ.get("ENGINE_MODEL_STRONG") or "").strip()
        config.max_spawns_per_turn = _env_int(
            "ENGINE_MAX_SPAWNS_PER_TURN", config.max_spawns_per_turn, warnings
        )
        config.subscriber_capacity = _env_int(
            "ENGINE_SUBSCRIBER_CAPACITY", config.subscriber_capacity, warnings
        )
        config.subscriber_bytes = _env_int(
            "ENGINE_SUBSCRIBER_BYTES", config.subscriber_bytes, warnings
        )
        config.pushgateway_url = (
            os.environ.get("ENGINE_PUSHGATEWAY_URL") or ""
        ).strip()
        job = (os.environ.get("ENGINE_METRICS_JOB") or config.metrics_job).strip()
        config.metrics_job = job or "engine"
        config.metrics_instance = (
            os.environ.get("ENGINE_METRICS_INSTANCE") or ""
        ).strip()
        config.metrics_push_interval_s = _env_float(
            "ENGINE_METRICS_PUSH_INTERVAL_S",
            config.metrics_push_interval_s,
            warnings,
        )
        if config.metrics_push_interval_s < 0:
            warnings.append(
                f"ENGINE_METRICS_PUSH_INTERVAL_S={config.metrics_push_interval_s!r} "
                "is negative; using 2"
            )
            config.metrics_push_interval_s = 2.0
        config.typesafe_api_key = typesafe_api_key_from_env()
        raw_judge_mode = os.environ.get("ENGINE_JUDGE", config.judge_mode)
        judge_mode = (raw_judge_mode or "").strip().lower()
        if judge_mode not in JUDGE_MODES:
            warnings.append(
                f"ENGINE_JUDGE={raw_judge_mode!r} is not "
                "off|advisory|calibrated|enforcing; using advisory"
            )
            judge_mode = "advisory"
        config.judge_mode = judge_mode
        if judge_mode in {"enforcing", "calibrated"} and not config.typesafe_api_key:
            warnings.append(
                f"ENGINE_JUDGE={judge_mode} but no TypeSafe API key is set "
                "(TYPESAFE_API_KEY or TYPESAFE_JEV_API_KEY); the judge is off"
            )
        config.judge_model = (
            os.environ.get("ENGINE_JUDGE_MODEL") or config.judge_model
        ).strip()
        config.judge_timeout_ms = _env_int(
            "ENGINE_JUDGE_TIMEOUT_MS", config.judge_timeout_ms, warnings
        )
        config.judge_cache_size = _env_int(
            "ENGINE_JUDGE_CACHE_SIZE", config.judge_cache_size, warnings
        )
        config.loop_control_interval = _env_int(
            "ENGINE_JUDGE_LOOP_INTERVAL", config.loop_control_interval, warnings
        )
        for site in JUDGE_SITES:
            setattr(
                config,
                f"judge_mode_{site}",
                _env_judge_site(f"ENGINE_JUDGE_{site.upper()}", warnings),
            )
        config.trace_calls = _env_bool("ENGINE_TRACE_CALLS", config.trace_calls, warnings)
        raw_interview = os.environ.get("ENGINE_INTERVIEW", "off")
        interview = (raw_interview or "").strip().lower()
        if interview in {"1", "true", "yes", "on"}:
            config.interview = True
        elif interview in {"0", "false", "no", "off", ""}:
            config.interview = False
        else:
            warnings.append(
                f"ENGINE_INTERVIEW={raw_interview!r} is not off|on; using off"
            )
            config.interview = False

        # An explicit-but-invalid value (a typo, a stale config) is not a
        # deliberate opt-out of the judged default -- it's treated the same
        # as unset, so it still gets the smart default when the judge is
        # usable, after a warning about the typo. Only a *valid* explicit
        # choice (auto/always/never/judged) counts as opting out.
        approval_set = "ENGINE_EXEC_APPROVAL" in os.environ
        raw_approval = os.environ.get("ENGINE_EXEC_APPROVAL", config.exec_approval)
        approval = (raw_approval or "").strip().lower()
        if approval not in EXEC_APPROVALS:
            warnings.append(
                f"ENGINE_EXEC_APPROVAL={raw_approval!r} is not "
                "auto|always|never|judged; using auto"
            )
            approval = "auto"
            approval_set = False
        if not approval_set and config.judge_usable and config.judge_mode_for("exec") != "off":
            approval = "judged"
        elif (
            approval_set
            and approval != "judged"
            and config.judge_usable
            and config.judge_mode_for("exec") != "off"
        ):
            # A dual-knob footgun: ENGINE_JUDGE(_EXEC) turns the exec judge
            # on, but run_command only ever consults it when exec_approval
            # is literally "judged" (tools/shell.py). An explicit
            # auto/always/never here silently means no exec judge at all,
            # despite the judge config suggesting otherwise.
            warnings.append(
                f"exec judge is enabled (mode={config.judge_mode_for('exec')!r}) but "
                f"ENGINE_EXEC_APPROVAL={approval!r} is explicit, so run_command never "
                "consults it; unset ENGINE_EXEC_APPROVAL or set it to 'judged' to use it"
            )
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


def typesafe_api_key_from_env() -> str:
    """TYPESAFE_API_KEY wins; TYPESAFE_JEV_API_KEY is the dashboard alias."""
    for name in ("TYPESAFE_API_KEY", "TYPESAFE_JEV_API_KEY"):
        raw = os.environ.get(name, "").strip()
        if raw and raw not in TYPESAFE_PLACEHOLDERS:
            return raw
    return ""


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


def _env_judge_site(name: str, warnings: list[str]) -> str:
    """Per-site judge mode override: empty string means 'inherit the global mode'."""
    raw = os.environ.get(name, "")
    value = (raw or "").strip().lower()
    if value == "":
        return ""
    if value not in ("off", "advisory", "enforcing"):
        warnings.append(
            f"{name}={raw!r} is not off|advisory|enforcing; ignoring override"
        )
        return ""
    return value


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
