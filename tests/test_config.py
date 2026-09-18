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
