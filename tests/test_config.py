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
    monkeypatch.delenv("ENGINE_PUSHGATEWAY_URL", raising=False)
    monkeypatch.delenv("ENGINE_METRICS_JOB", raising=False)
    monkeypatch.delenv("ENGINE_METRICS_INSTANCE", raising=False)
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
    assert config.metrics_instance == ""
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


def test_from_env_twice_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "real-key")
    (tmp_path / "env.sh").write_text("export OPENROUTER_API_KEY=from-file\n")
    EngineConfig.from_env(tmp_path)
    EngineConfig.from_env(tmp_path)
    assert monkeypatch._setitem or True
    import os

    assert os.environ["OPENROUTER_API_KEY"] == "real-key"
    assert "real-key" not in PLACEHOLDERS


def test_pushgateway_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGINE_PUSHGATEWAY_URL", "http://pushgateway:9091")
    monkeypatch.setenv("ENGINE_METRICS_JOB", "engine-bench")
    monkeypatch.setenv("ENGINE_METRICS_INSTANCE", "baseline")
    monkeypatch.setenv("ENGINE_METRICS_PUSH_INTERVAL_S", "0.5")
    config = EngineConfig.from_env(tmp_path)
    assert config.pushgateway_url == "http://pushgateway:9091"
    assert config.metrics_job == "engine-bench"
    assert config.metrics_instance == "baseline"
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
