from __future__ import annotations

from runtime.config import EngineConfig
from llm.openrouter import PLACEHOLDERS


def test_defaults(monkeypatch, tmp_path):
    for key in list(monkeypatch._setitem):
        pass
    monkeypatch.delenv("ENGINE_MAX_TURNS", raising=False)
    monkeypatch.delenv("ENGINE_TURN_SLICE", raising=False)
    monkeypatch.delenv("ENGINE_MAX_CONTINUES", raising=False)
    monkeypatch.delenv("ENGINE_TURN_CONTINUE", raising=False)
    monkeypatch.delenv("ENGINE_MAX_SPAWNS_PER_TURN", raising=False)
    monkeypatch.delenv("ENGINE_EXEC_APPROVAL", raising=False)
    monkeypatch.delenv("ENGINE_JUDGE", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_JEV_API_KEY", raising=False)
    monkeypatch.delenv("ENGINE_PUSHGATEWAY_URL", raising=False)
    monkeypatch.delenv("ENGINE_METRICS_JOB", raising=False)
    monkeypatch.delenv("ENGINE_METRICS_PUSH_INTERVAL_S", raising=False)
    config = EngineConfig.from_env(tmp_path)
    assert config.max_turns == 16
    assert config.turn_slice == 16
    assert config.max_continues == 3
    assert config.turn_continue == "prompt"
    assert config.max_spawns_per_turn == 8
    assert config.exec_approval == "auto"
    assert config.compact_trigger == 0.7
    assert config.keep_full_tools == 3
    assert config.subscriber_bytes == 1 << 20
    assert config.pushgateway_url == ""
    assert config.metrics_job == "engine"
    assert config.metrics_push_interval_s == 2.0
    assert config.warnings == []


def test_reads_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGINE_MAX_TURNS", "4")
    monkeypatch.setenv("ENGINE_TURN_SLICE", "8")
    monkeypatch.setenv("ENGINE_MAX_CONTINUES", "1")
    monkeypatch.setenv("ENGINE_TURN_CONTINUE", "never")
    monkeypatch.setenv("ENGINE_LLM_STREAM", "0")
    config = EngineConfig.from_env(tmp_path)
    assert config.max_turns == 4
    assert config.turn_slice == 8
    assert config.max_continues == 1
    assert config.turn_continue == "never"
    assert config.llm_stream is False


def test_invalid_turn_continue_warns(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGINE_TURN_CONTINUE", "always")
    config = EngineConfig.from_env(tmp_path)
    assert config.turn_continue == "prompt"
    assert any("ENGINE_TURN_CONTINUE" in item for item in config.warnings)


def test_env_sh_loaded_first(monkeypatch, tmp_path):
    monkeypatch.delenv("ENGINE_MAX_TURNS", raising=False)
    (tmp_path / "env.sh").write_text("export ENGINE_MAX_TURNS=7\n")
    config = EngineConfig.from_env(tmp_path)
    assert config.max_turns == 7


def test_malformed_int_warns(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGINE_MAX_TURNS", "sixteen")
    config = EngineConfig.from_env(tmp_path)
    assert config.max_turns == 16
    assert any("ENGINE_MAX_TURNS" in item for item in config.warnings)


def test_invalid_approval_warns(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGINE_EXEC_APPROVAL", "nver")
    config = EngineConfig.from_env(tmp_path)
    assert config.exec_approval == "auto"
    assert any("ENGINE_EXEC_APPROVAL" in item for item in config.warnings)


def test_invalid_approval_still_gets_judged_default_when_usable(monkeypatch, tmp_path):
    # A typo'd ENGINE_EXEC_APPROVAL is not a deliberate opt-out of the smart
    # default -- it should behave like unset (still warn about the typo),
    # not silently downgrade to legacy "auto" just because *something* was
    # present in the environment.
    monkeypatch.setenv("ENGINE_EXEC_APPROVAL", "nver")
    monkeypatch.setenv("TYPESAFE_API_KEY", "sk-real-typesafe-key")
    config = EngineConfig.from_env(tmp_path)
    assert config.exec_approval == "judged"
    assert any("ENGINE_EXEC_APPROVAL" in item for item in config.warnings)


def test_from_env_twice_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "real-key")
    (tmp_path / "env.sh").write_text("export OPENROUTER_API_KEY=from-file\n")
    EngineConfig.from_env(tmp_path)
    EngineConfig.from_env(tmp_path)
    assert monkeypatch._setitem or True
    import os

    assert os.environ["OPENROUTER_API_KEY"] == "real-key"
    assert "real-key" not in PLACEHOLDERS


def test_typesafe_key_placeholders_treated_as_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("TYPESAFE_JEV_API_KEY", raising=False)
    for placeholder in ("", "...", "your-key", "changeme"):
        monkeypatch.setenv("TYPESAFE_API_KEY", placeholder)
        config = EngineConfig.from_env(tmp_path)
        assert config.typesafe_api_key == ""
        assert config.judge_usable is False


def test_typesafe_real_key_is_usable(monkeypatch, tmp_path):
    monkeypatch.setenv("TYPESAFE_API_KEY", "sk-real-typesafe-key")
    config = EngineConfig.from_env(tmp_path)
    assert config.typesafe_api_key == "sk-real-typesafe-key"
    assert config.judge_usable is True


def test_exec_approval_defaults_to_judged_when_judge_usable(monkeypatch, tmp_path):
    monkeypatch.delenv("ENGINE_EXEC_APPROVAL", raising=False)
    monkeypatch.setenv("TYPESAFE_API_KEY", "sk-real-typesafe-key")
    config = EngineConfig.from_env(tmp_path)
    assert config.exec_approval == "judged"


def test_explicit_exec_approval_overrides_judged_default(monkeypatch, tmp_path):
    monkeypatch.setenv("TYPESAFE_API_KEY", "sk-real-typesafe-key")
    monkeypatch.setenv("ENGINE_EXEC_APPROVAL", "always")
    config = EngineConfig.from_env(tmp_path)
    assert config.exec_approval == "always"


def test_engine_judge_off_disables_judged_default(monkeypatch, tmp_path):
    monkeypatch.delenv("ENGINE_EXEC_APPROVAL", raising=False)
    monkeypatch.setenv("TYPESAFE_API_KEY", "sk-real-typesafe-key")
    monkeypatch.setenv("ENGINE_JUDGE", "off")
    config = EngineConfig.from_env(tmp_path)
    assert config.exec_approval == "auto"
    assert config.judge_usable is False


def test_invalid_judge_mode_warns(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGINE_JUDGE", "sometimes")
    config = EngineConfig.from_env(tmp_path)
    assert config.judge_mode == "advisory"
    assert any("ENGINE_JUDGE" in item for item in config.warnings)


def test_judge_mode_for_site_override(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGINE_JUDGE", "advisory")
    monkeypatch.setenv("ENGINE_JUDGE_EXEC", "enforcing")
    config = EngineConfig.from_env(tmp_path)
    assert config.judge_mode_for("exec") == "enforcing"
    assert config.judge_mode_for("tools") == "advisory"


def test_invalid_judge_site_override_is_ignored_with_warning(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGINE_JUDGE_SCREEN", "sometimes")
    config = EngineConfig.from_env(tmp_path)
    assert config.judge_mode_for("screen") == config.judge_mode
    assert any("ENGINE_JUDGE_SCREEN" in item for item in config.warnings)


def test_phase6_judge_site_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGINE_JUDGE", "advisory")
    monkeypatch.setenv("ENGINE_JUDGE_COMPACTION", "enforcing")
    monkeypatch.setenv("ENGINE_JUDGE_DIAGNOSTICS", "off")
    monkeypatch.setenv("ENGINE_JUDGE_LOOP", "enforcing")
    config = EngineConfig.from_env(tmp_path)
    assert config.judge_mode_for("compaction") == "enforcing"
    assert config.judge_mode_for("diagnostics") == "off"
    assert config.judge_mode_for("loop") == "enforcing"


def test_phase5_intent_judge_site_override(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGINE_JUDGE", "advisory")
    monkeypatch.setenv("ENGINE_JUDGE_INTENT", "enforcing")
    config = EngineConfig.from_env(tmp_path)
    assert config.judge_mode_for("intent") == "enforcing"


def test_model_cheap_and_strong_default_empty(monkeypatch, tmp_path):
    monkeypatch.delenv("ENGINE_MODEL_CHEAP", raising=False)
    monkeypatch.delenv("ENGINE_MODEL_STRONG", raising=False)
    config = EngineConfig.from_env(tmp_path)
    assert config.model_cheap == ""
    assert config.model_strong == ""


def test_model_cheap_and_strong_read_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGINE_MODEL_CHEAP", "openai/gpt-4o-mini")
    monkeypatch.setenv("ENGINE_MODEL_STRONG", "anthropic/claude-opus")
    config = EngineConfig.from_env(tmp_path)
    assert config.model_cheap == "openai/gpt-4o-mini"
    assert config.model_strong == "anthropic/claude-opus"


def test_write_gate_never_silently_inherits_global_enforcing(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGINE_JUDGE", "enforcing")
    config = EngineConfig.from_env(tmp_path)
    assert config.judge_mode == "enforcing"
    assert config.judge_mode_for("write") == "advisory"
    assert config.judge_mode_for("exec") == "enforcing"  # other sites unaffected


def test_write_gate_respects_off_and_explicit_override(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGINE_JUDGE", "off")
    off_config = EngineConfig.from_env(tmp_path)
    assert off_config.judge_mode_for("write") == "off"

    monkeypatch.setenv("ENGINE_JUDGE", "enforcing")
    monkeypatch.setenv("ENGINE_JUDGE_WRITE", "enforcing")
    explicit_config = EngineConfig.from_env(tmp_path)
    assert explicit_config.judge_mode_for("write") == "enforcing"


def test_merge_gate_judge_site_override(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGINE_JUDGE", "advisory")
    monkeypatch.setenv("ENGINE_JUDGE_MERGE", "enforcing")
    config = EngineConfig.from_env(tmp_path)
    assert config.judge_mode_for("merge") == "enforcing"


def test_no_typesafe_key_is_byte_identical_to_baseline(monkeypatch, tmp_path):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_JEV_API_KEY", raising=False)
    monkeypatch.delenv("ENGINE_EXEC_APPROVAL", raising=False)
    monkeypatch.delenv("ENGINE_JUDGE", raising=False)
    config = EngineConfig.from_env(tmp_path)
    assert config.exec_approval == "auto"
    assert config.warnings == []


def test_typesafe_jev_alias_is_accepted(monkeypatch, tmp_path):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("TYPESAFE_JEV_API_KEY", "apikey_jev_alias")
    config = EngineConfig.from_env(tmp_path)
    assert config.typesafe_api_key == "apikey_jev_alias"
    assert config.judge_usable is True


def test_typesafe_api_key_wins_over_jev_alias(monkeypatch, tmp_path):
    monkeypatch.setenv("TYPESAFE_API_KEY", "sk-canonical")
    monkeypatch.setenv("TYPESAFE_JEV_API_KEY", "apikey_jev_alias")
    config = EngineConfig.from_env(tmp_path)
    assert config.typesafe_api_key == "sk-canonical"


def test_enforcing_without_key_warns(monkeypatch, tmp_path):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_JEV_API_KEY", raising=False)
    monkeypatch.setenv("ENGINE_JUDGE", "enforcing")
    config = EngineConfig.from_env(tmp_path)
    assert config.judge_usable is False
    assert any("ENGINE_JUDGE=enforcing" in item for item in config.warnings)


def test_pushgateway_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGINE_PUSHGATEWAY_URL", "http://pushgateway:9091")
    monkeypatch.setenv("ENGINE_METRICS_JOB", "engine-bench")
    monkeypatch.setenv("ENGINE_METRICS_PUSH_INTERVAL_S", "0.5")
    config = EngineConfig.from_env(tmp_path)
    assert config.pushgateway_url == "http://pushgateway:9091"
    assert config.metrics_job == "engine-bench"
    assert config.metrics_push_interval_s == 0.5


def test_empty_metrics_job_falls_back(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGINE_METRICS_JOB", "   ")
    config = EngineConfig.from_env(tmp_path)
    assert config.metrics_job == "engine"


def test_negative_push_interval_warns(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGINE_METRICS_PUSH_INTERVAL_S", "-1")
    config = EngineConfig.from_env(tmp_path)
    assert config.metrics_push_interval_s == 2.0
    assert any("ENGINE_METRICS_PUSH_INTERVAL_S" in item for item in config.warnings)
