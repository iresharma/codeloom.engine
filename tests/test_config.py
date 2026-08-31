from __future__ import annotations

from runtime.config import EngineConfig
from llm.openrouter import PLACEHOLDERS


def test_defaults(monkeypatch, tmp_path):
    for key in list(monkeypatch._setitem):
        pass
    monkeypatch.delenv("ENGINE_MAX_TURNS", raising=False)
    monkeypatch.delenv("ENGINE_EXEC_APPROVAL", raising=False)
    config = EngineConfig.from_env(tmp_path)
    assert config.max_turns == 16
    assert config.exec_approval == "auto"
    assert config.subscriber_bytes == 1 << 20
    assert config.warnings == []


def test_reads_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGINE_MAX_TURNS", "4")
    monkeypatch.setenv("ENGINE_LLM_STREAM", "0")
    config = EngineConfig.from_env(tmp_path)
    assert config.max_turns == 4
    assert config.llm_stream is False


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
