"""Coverage for agents/agent_loop.py"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agents.agent_loop import (
    AgentLoop,
    DEFAULT_SYSTEM,
    compact_params,
    CLOSER_MESSAGE,
    CONTINUE_GRANT,
    TURN_CONTINUE_CHOICES,
    CONTINUE_ALIASES,
    STOP_ALIASES,
)
from llm.provider import LLMResult, Usage, ToolCall
from runtime.config import EngineConfig
from tests.fakes import FakeProvider


def test_agent_loop_init(tmp_path):
    provider = FakeProvider()
    loop = AgentLoop(
        llm=provider,
        workspace=tmp_path,
        system_prompt=DEFAULT_SYSTEM,
    )
    assert loop._llm == provider
    assert loop._workspace == tmp_path


def test_agent_loop_default_system():
    assert "coding assistant" in DEFAULT_SYSTEM


def test_agent_loop_closer_message():
    assert "two tool turns" in CLOSER_MESSAGE


def test_agent_loop_continue_grant():
    assert "Continue from leftover" in CONTINUE_GRANT


def test_agent_loop_turn_continue_choices():
    assert "continue" in TURN_CONTINUE_CHOICES
    assert "stop" in TURN_CONTINUE_CHOICES


def test_agent_loop_continue_aliases():
    assert "continue" in CONTINUE_ALIASES
    assert "yes" in CONTINUE_ALIASES


def test_agent_loop_stop_aliases():
    assert "stop" in STOP_ALIASES


def test_compact_params_defaults():
    config = EngineConfig()
    trigger, keep_full = compact_params(config)
    assert trigger > 0
    assert keep_full > 0


def test_compact_params_custom():
    config = EngineConfig(compact_trigger=0.8, keep_full_tools=200)
    trigger, keep_full = compact_params(config)
    assert trigger == 0.8
    assert keep_full == 200


def test_compact_params_partial():
    config = EngineConfig(compact_trigger=0.7)
    trigger, keep_full = compact_params(config)
    assert trigger == 0.7
    assert keep_full > 0


def test_agent_loop_with_tools(tmp_path):
    from tools.registry import discover_tools
    
    provider = FakeProvider()
    tools = discover_tools()
    loop = AgentLoop(llm=provider, workspace=tmp_path, tools=tools)
    assert loop._tools == tools


def test_agent_loop_custom_on_tool(tmp_path):
    calls = []
    
    def on_tool(name, tool_input, result, status):
        calls.append((name, status))
    
    provider = FakeProvider()
    loop = AgentLoop(llm=provider, workspace=tmp_path, on_tool=on_tool)
    assert loop._on_tool == on_tool


def test_agent_loop_language(tmp_path):
    from runtime.language import LanguageInfo
    
    provider = FakeProvider()
    lang = LanguageInfo(lang="python")
    loop = AgentLoop(llm=provider, workspace=tmp_path, language=lang)
    assert loop._language == lang


def test_agent_loop_lsp(tmp_path):
    provider = FakeProvider()
    lsp_mock = MagicMock()
    loop = AgentLoop(llm=provider, workspace=tmp_path, lsp=lsp_mock)
    assert loop._lsp == lsp_mock


def test_agent_loop_files(tmp_path):
    from runtime.tools.tracker import FileTracker
    
    provider = FakeProvider()
    files = FileTracker()
    loop = AgentLoop(llm=provider, workspace=tmp_path, files=files)
    assert loop._files == files


def test_agent_loop_max_turns(tmp_path):
    provider = FakeProvider()
    loop = AgentLoop(llm=provider, workspace=tmp_path, max_turns=10)
    assert loop._max_turns == 10


def test_agent_loop_max_tools(tmp_path):
    provider = FakeProvider()
    loop = AgentLoop(llm=provider, workspace=tmp_path, max_tools=100)
    assert loop._max_tools == 100


def test_agent_loop_reply_cap(tmp_path):
    provider = FakeProvider()
    loop = AgentLoop(llm=provider, workspace=tmp_path, reply_cap=5000)
    assert loop._reply_cap == 5000


def test_agent_loop_hooks(tmp_path):
    from agents.hooks import AgentHooks
    
    provider = FakeProvider()
    hooks = AgentHooks()
    loop = AgentLoop(llm=provider, workspace=tmp_path, hooks=hooks)
    assert loop._hooks == hooks


def test_agent_loop_registry(tmp_path):
    from tools.registry import ToolRegistry
    
    provider = FakeProvider()
    registry = ToolRegistry()
    loop = AgentLoop(llm=provider, workspace=tmp_path, registry=registry)
    assert loop._registry == registry


async def test_agent_loop_run_simple(tmp_path):
    provider = FakeProvider(results=[LLMResult(text="done", usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15, requests=1))])
    loop = AgentLoop(llm=provider, workspace=tmp_path)
    
    # Mock necessary methods
    loop._emit = MagicMock()
    loop._init_context = MagicMock()
    
    # Just test initialization doesn't crash


def test_agent_loop_context_dump(tmp_path):
    provider = FakeProvider()
    loop = AgentLoop(llm=provider, workspace=tmp_path)
    
    # Mock the context
    with patch.object(loop, "_context", "test context"):
        result = loop.context_dump()
        assert isinstance(result, str)


def test_agent_loop_breakdown_for(tmp_path):
    provider = FakeProvider()
    loop = AgentLoop(llm=provider, workspace=tmp_path)
    
    breakdown = loop.breakdown_for("nonexistent_agent")
    assert breakdown is None


def test_agent_loop_transcript_for(tmp_path):
    provider = FakeProvider()
    loop = AgentLoop(llm=provider, workspace=tmp_path)
    
    transcript = loop.transcript_for("nonexistent_agent")
    assert transcript is None
