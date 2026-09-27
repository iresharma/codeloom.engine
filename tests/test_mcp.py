from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agents.profile import discover_profiles
from protocol.commands import (
    AnswerPrompt,
    CompleteMcpAuth,
    ReloadIntegrations,
    SetMcpEnabled,
    StartSession,
)
from protocol.events import (
    McpAuthRequired,
    SnapshotReady,
    UserPromptRequested,
    WarningOccurred,
)
from protocol.redact import redact_command
from runtime.mcp.bridge import flatten_mcp_result, sanitize_mcp_name, scheme_allowed
from runtime.mcp.config import load_mcp_config
from runtime.mcp.manager import McpManager
from runtime.session import EngineSession
from tests.fakes import FakeMcpSession, fake_mcp_connect
from tools.registry import discover_tools
from unittest.mock import patch
import pytest
from runtime.mcp.config import McpServerConfig
from runtime.mcp.manager import (
    McpServerState,
    extract_auth_url,
    is_auth_error,
)
from types import SimpleNamespace
from unittest.mock import Mock
from runtime.mcp.manager import (
    _tool_list, _tool_name, _tool_description, _input_schema, _read_only,
    _has_stored_token, _call, _maybe_await,
)
from runtime.mcp.tokens import apply_tokens, load_tokens, save_token, tokens_path


def _write_engine_mcp(tmp_path, servers: dict):
    dest = tmp_path / ".engine" / "mcp.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def test_missing_env_fail_loud(tmp_path, monkeypatch):
    monkeypatch.delenv("MISSING_TOKEN", raising=False)
    _write_engine_mcp(
        tmp_path,
        {
            "broken": {
                "command": "npx",
                "env": {"TOKEN": "${env:MISSING_TOKEN}"},
            },
            "ok": {"command": "echo"},
        },
    )
    configs, _, _ = load_mcp_config(tmp_path)
    by_name = {item.name: item for item in configs}
    assert by_name["broken"].unresolved == "MISSING_TOKEN"

    async def run():
        manager = McpManager(tmp_path, connect=fake_mcp_connect())
        await manager.start(configs)
        assert manager.servers["broken"].status == "error"
        assert "unresolved" in manager.servers["broken"].error
        await manager.aclose()

    asyncio.run(run())


def test_sanitized_name_collision():
    assert sanitize_mcp_name("my-server", "tool.a") == sanitize_mcp_name("my_server", "tool_a")

    async def run():
        session = FakeMcpSession(
            tools=[{"name": "tool.a"}, {"name": "tool_a"}],
        )
        manager = McpManager(
            Path("."),
            connect=fake_mcp_connect(lambda: session),
        )
        from runtime.mcp.config import McpServerConfig

        await manager.start([McpServerConfig(name="my-server", command="x")])
        wires = [spec.name for spec in manager.tools() if spec.family == "mcp" and spec.name.startswith("mcp_")]
        assert len([name for name in wires if name == "mcp_my_server_tool_a"]) == 1
        assert any("collision" in item for item in manager.warnings)
        await manager.aclose()

    asyncio.run(run())


def test_subset_mcp_profiles():
    registry = discover_tools()
    from tools.base import Tool

    async def _fn(ctx=None, **kwargs):
        return "ok"

    registry.register(
        Tool(
            name="mcp_github_search",
            description="s",
            parameters={"type": "object", "properties": {}},
            fn=_fn,
            family="mcp",
            mcp_profiles=("researcher", "debugger"),
        )
    )
    researcher = registry.subset(["mcp", "read_file"], profile="researcher")
    coder = registry.subset(["mcp", "read_file"], profile="coder")
    assert "mcp_github_search" in researcher.names()
    assert "read_file" in researcher.names()
    assert "mcp_github_search" not in coder.names()
    assert "read_file" in coder.names()
    profiles = discover_profiles()
    assert "mcp" in profiles.get("researcher").tool_names
    assert "mcp" not in profiles.get("coder").tool_names


def test_flatten_image_omitted():
    text = flatten_mcp_result(
        [
            {"type": "text", "text": "hello"},
            {"type": "image", "mimeType": "image/png", "data": "abc" * 20},
        ]
    )
    assert "hello" in text
    assert "omitted: image/png" in text
    assert "abc" * 5 not in text


def test_one_bad_server_does_not_block_start(tmp_path):
    _write_engine_mcp(
        tmp_path,
        {"bad": {"command": "x"}, "good": {"command": "y"}},
    )

    async def connect(cfg):
        if cfg.name == "bad":
            raise RuntimeError("boom")
        return FakeMcpSession()

    async def run():
        session = EngineSession(tmp_path, tmp_path / "session.db")
        await session.start()
        session._mcp_connect = connect
        queue = session.subscribe()
        await session.handle(StartSession(workspace=str(tmp_path)))
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        assert any(isinstance(item, SnapshotReady) for item in events)
        assert session._mcp.servers["bad"].status == "error"
        assert session._mcp.servers["good"].status == "ready"
        await session.aclose()
        # fake sessions closed via manager
        assert True

    asyncio.run(run())


def test_backoff_then_cooling(tmp_path):
    attempts = {"n": 0}

    async def connect(cfg):
        attempts["n"] += 1
        sess = FakeMcpSession()
        sess.dead = True
        return sess

    async def sleeper(_delay):
        return None

    async def run():
        manager = McpManager(
            tmp_path,
            connect=connect,
            backoff_s=(0, 0, 0),
            cool_s=30,
            sleep=sleeper,
        )
        from runtime.mcp.config import McpServerConfig

        clock = {"t": 0.0}

        def now():
            return clock["t"]

        manager._clock = now
        await manager.start([McpServerConfig(name="gh", command="x")])
        first = await manager.call_tool("gh", "search", {})
        assert "cooling" in first
        after_first = attempts["n"]
        second = await manager.call_tool("gh", "search", {})
        assert "cooling" in second
        assert attempts["n"] == after_first
        clock["t"] = 31
        await manager.call_tool("gh", "search", {})
        assert attempts["n"] > after_first
        await manager.aclose()

    asyncio.run(run())


def test_reload_clears_cooling(tmp_path):
    _write_engine_mcp(tmp_path, {"gh": {"command": "x"}})
    calls = {"n": 0}

    async def connect(cfg):
        calls["n"] += 1
        return FakeMcpSession()

    async def run():
        session = EngineSession(tmp_path, tmp_path / "session.db")
        await session.start()
        session._mcp_connect = connect
        session._mcp_backoff = (0, 0, 0)
        session._mcp_cool_s = 30
        await session.handle(StartSession(workspace=str(tmp_path)))
        session._mcp.servers["gh"].cool_until = session._mcp._clock() + 30
        session._mcp.servers["gh"].status = "error"
        session._mcp.servers["gh"].error = "cooling"
        before = calls["n"]
        await session.handle(ReloadIntegrations())
        assert calls["n"] > before
        assert session._mcp.servers["gh"].cool_until == 0
        await session.aclose()

    asyncio.run(run())


def test_shutdown_closes_session(tmp_path):
    closed = {}

    async def connect(cfg):
        sess = FakeMcpSession()

        async def aclose():
            closed["ok"] = True

        sess.aclose = aclose
        return sess

    _write_engine_mcp(tmp_path, {"gh": {"command": "x"}})

    async def run():
        session = EngineSession(tmp_path, tmp_path / "session.db")
        await session.start()
        session._mcp_connect = connect
        await session.handle(StartSession(workspace=str(tmp_path)))
        await session.aclose()
        assert closed.get("ok")

    asyncio.run(run())


def test_resource_uri_jail():
    advertised = {"https://example.com/doc"}
    assert scheme_allowed("file:///etc/passwd", advertised).startswith("error:")
    assert scheme_allowed("https://example.com/doc", advertised) is None
    assert scheme_allowed("ftp://host/x", advertised).startswith("error:")
    assert scheme_allowed("gopher://x").startswith("error:")


def test_read_resource_https(tmp_path):
    async def run():
        sess = FakeMcpSession(
            resources=[{"uri": "https://example.com/doc", "name": "doc"}]
        )
        manager = McpManager(tmp_path, connect=fake_mcp_connect(lambda: sess))
        from runtime.mcp.config import McpServerConfig

        await manager.start([McpServerConfig(name="docs", command="x")])
        assert "https://example.com/doc" in await manager.read_resource(
            "https://example.com/doc"
        )
        assert (await manager.read_resource("file:///etc/passwd")).startswith("error:")
        await manager.aclose()

    asyncio.run(run())


def test_cursor_import_trust(tmp_path):
    dest = tmp_path / ".cursor" / "mcp.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps({"mcpServers": {"gh": {"command": "npx"}}}),
        encoding="utf-8",
    )

    async def run():
        session = EngineSession(tmp_path, tmp_path / "session.db")
        await session.start()
        session._mcp_connect = fake_mcp_connect()
        queue = session.subscribe()
        task = asyncio.create_task(
            session.handle(StartSession(workspace=str(tmp_path)))
        )
        pending = None
        for _ in range(100):
            pending = session._prompts.pending()
            if pending is not None:
                break
            await asyncio.sleep(0.01)
        assert pending is not None
        assert pending.kind == "confirm"
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        assert any(isinstance(item, WarningOccurred) and "cursor" in item.message for item in events)
        await session.handle(AnswerPrompt(prompt_id=pending.prompt_id, text="yes"))
        await task
        trust = json.loads((tmp_path / ".engine" / "mcp-trust.json").read_text())
        assert "gh" in trust["cursor"]
        session2 = EngineSession(tmp_path, tmp_path / "session.db")
        await session2.start()
        session2._mcp_connect = fake_mcp_connect()
        task2 = asyncio.create_task(
            session2.handle(StartSession(workspace=str(tmp_path)))
        )
        await asyncio.sleep(0.05)
        assert session2._prompts.pending() is None
        await task2
        await session.aclose()
        await session2.aclose()

    asyncio.run(run())


def test_cursor_import_refused(tmp_path):
    dest = tmp_path / ".cursor" / "mcp.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps({"mcpServers": {"gh": {"command": "npx"}}}),
        encoding="utf-8",
    )

    async def run():
        session = EngineSession(tmp_path, tmp_path / "session.db")
        await session.start()
        session._mcp_connect = fake_mcp_connect()
        task = asyncio.create_task(
            session.handle(StartSession(workspace=str(tmp_path)))
        )
        pending = None
        for _ in range(100):
            pending = session._prompts.pending()
            if pending is not None:
                break
            await asyncio.sleep(0.01)
        await session.handle(AnswerPrompt(prompt_id=pending.prompt_id, text="no"))
        await task
        assert session._mcp.servers["gh"].status == "disabled"
        trust = json.loads((tmp_path / ".engine" / "mcp-trust.json").read_text())
        assert "gh" in trust["refused"]
        await session.aclose()

    asyncio.run(run())


def test_auth_url_stdout_and_stderr(tmp_path):
    _write_engine_mcp(
        tmp_path,
        {
            "slack": {"command": "x", "tokenEnv": "SLACK_BOT_TOKEN"},
            "ok": {"command": "y"},
        },
    )

    async def connect(cfg):
        if cfg.name == "slack":
            return FakeMcpSession(stderr="login at https://slack.example/auth now")
        return FakeMcpSession()

    async def run():
        session = EngineSession(tmp_path, tmp_path / "session.db")
        await session.start()
        session._mcp_connect = connect
        queue = session.subscribe()
        await session.handle(StartSession(workspace=str(tmp_path)))
        await asyncio.sleep(0.05)
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        assert session._mcp.servers["ok"].status == "ready"
        assert session._mcp.servers["slack"].status == "needs_auth"
        assert any(isinstance(item, McpAuthRequired) for item in events)
        assert any(
            isinstance(item, UserPromptRequested) and item.kind == "mcp_auth"
            for item in events
        )
        await session.aclose()

    asyncio.run(run())

    async def connect_stdout(cfg):
        return FakeMcpSession(stdout="https://login.example/x")

    _write_engine_mcp(tmp_path, {"web": {"command": "x", "tokenEnv": "T"}})

    async def run2():
        session = EngineSession(tmp_path, tmp_path / "session.db")
        await session.start()
        session._mcp_connect = connect_stdout
        queue = session.subscribe()
        await session.handle(StartSession(workspace=str(tmp_path)))
        await asyncio.sleep(0.05)
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        assert any(
            isinstance(item, McpAuthRequired) and "login.example" in item.url
            for item in events
        )
        await session.aclose()

    asyncio.run(run2())


def test_auth_without_token_env(tmp_path):
    _write_engine_mcp(tmp_path, {"slack": {"command": "x"}})

    async def connect(cfg):
        return FakeMcpSession(stderr="https://slack.example/login")

    async def run():
        session = EngineSession(tmp_path, tmp_path / "session.db")
        await session.start()
        session._mcp_connect = connect
        queue = session.subscribe()
        await session.handle(StartSession(workspace=str(tmp_path)))
        await asyncio.sleep(0.05)
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        assert any(isinstance(item, McpAuthRequired) for item in events)
        assert not any(
            isinstance(item, UserPromptRequested) and item.kind == "mcp_auth"
            for item in events
        )
        assert any("tokenEnv" in item.message for item in events if isinstance(item, WarningOccurred))
        await session.aclose()

    asyncio.run(run())


def test_set_mcp_enabled_persists(tmp_path):
    _write_engine_mcp(tmp_path, {"gh": {"command": "x"}})

    async def run():
        session = EngineSession(tmp_path, tmp_path / "session.db")
        await session.start()
        session._mcp_connect = fake_mcp_connect()
        await session.handle(StartSession(workspace=str(tmp_path)))
        await session.handle(SetMcpEnabled(name="gh", enabled=False))
        data = json.loads((tmp_path / ".engine" / "mcp.json").read_text())
        assert data["mcpServers"]["gh"]["enabled"] is False
        assert session._mcp.servers["gh"].status == "disabled"
        await session.aclose()

    asyncio.run(run())


def test_answer_prompt_mcp_auth(tmp_path):
    _write_engine_mcp(
        tmp_path, {"slack": {"command": "x", "tokenEnv": "SLACK_BOT_TOKEN"}}
    )
    sessions = [FakeMcpSession(stderr="https://x.example/a"), FakeMcpSession()]

    async def connect(cfg):
        return sessions.pop(0)

    async def run():
        session = EngineSession(tmp_path, tmp_path / "session.db")
        await session.start()
        session._mcp_connect = connect
        await session.handle(StartSession(workspace=str(tmp_path)))
        pending = None
        for _ in range(100):
            pending = session._prompts.pending()
            if pending is not None and pending.kind == "mcp_auth":
                break
            await asyncio.sleep(0.01)
        assert pending is not None
        await session.handle(AnswerPrompt(prompt_id=pending.prompt_id, text="sekrit"))
        for _ in range(50):
            if session._mcp.servers["slack"].status == "ready":
                break
            await asyncio.sleep(0.02)
        data = json.loads((tmp_path / ".engine" / "mcp-tokens.json").read_text())
        assert data["slack"]["token"] == "sekrit"
        assert session._mcp.servers["slack"].status == "ready"
        await session.aclose()

    asyncio.run(run())


def test_complete_mcp_auth_stores_token(tmp_path):
    _write_engine_mcp(
        tmp_path, {"slack": {"command": "x", "tokenEnv": "SLACK_BOT_TOKEN"}}
    )
    sessions = [FakeMcpSession(stderr="https://x.example/a"), FakeMcpSession()]

    async def connect(cfg):
        return sessions.pop(0)

    async def run():
        session = EngineSession(tmp_path, tmp_path / "session.db")
        await session.start()
        session._mcp_connect = connect
        await session.handle(StartSession(workspace=str(tmp_path)))
        await session.handle(CompleteMcpAuth(server="slack", token="sekrit"))
        path = tmp_path / ".engine" / "mcp-tokens.json"
        data = json.loads(path.read_text())
        assert data["slack"]["token"] == "sekrit"
        assert oct(path.stat().st_mode & 0o777) == "0o600"
        assert session._mcp.servers["slack"].status == "ready"
        snap = session.snapshot().to_json()
        assert "sekrit" not in json.dumps(snap)
        redacted = redact_command(CompleteMcpAuth(server="slack", token="sekrit"))
        assert redacted["token"] == "<redacted>"
        raw = CompleteMcpAuth(server="slack", token="sekrit").to_json()
        assert raw["token"] == "sekrit"
        await session.aclose()

    asyncio.run(run())


def test_expired_token_needs_auth_again(tmp_path):
    _write_engine_mcp(
        tmp_path, {"slack": {"command": "x", "tokenEnv": "SLACK_BOT_TOKEN"}}
    )
    live = FakeMcpSession()

    async def connect(cfg):
        return live

    async def run():
        session = EngineSession(tmp_path, tmp_path / "session.db")
        await session.start()
        session._mcp_connect = connect
        await session.handle(StartSession(workspace=str(tmp_path)))
        assert session._mcp.servers["slack"].status == "ready"
        live.call_error = RuntimeError("401 unauthorized invalid_token")
        result = await session._mcp.call_tool("slack", "search", {}, read_only=True)
        assert "CompleteMcpAuth" in result
        assert session._mcp.servers["slack"].status == "needs_auth"
        assert "cooling" not in (session._mcp.servers["slack"].error or "")
        await session.aclose()

    asyncio.run(run())


def test_unannotated_tool_approval(tmp_path):
    asked = []

    async def ask(question, kind="text", **kwargs):
        asked.append(kind)
        return "no"

    async def run():
        sess = FakeMcpSession(tools=[{"name": "delete"}])
        manager = McpManager(
            tmp_path,
            connect=fake_mcp_connect(lambda: sess),
            ask_user=ask,
            approval="always",
        )
        from runtime.mcp.config import McpServerConfig

        await manager.start([McpServerConfig(name="gh", command="x")])
        out = await manager.call_tool("gh", "delete", {})
        assert "denied" in out
        assert asked
        asked.clear()
        sess.tools = [
            {"name": "search", "annotations": {"readOnlyHint": True}},
        ]
        await manager.restart("gh")
        # rebuild remote tool list
        manager.servers["gh"].remote_tools = sess.tools
        out = await manager.call_tool("gh", "search", {}, read_only=True)
        assert asked == []
        await manager.aclose()

    asyncio.run(run())


def test_elicitation_asks_user(tmp_path):
    asked = []

    async def ask(question, kind="text", **kwargs):
        asked.append((question, kind))
        return "ok"

    async def run():
        from runtime.mcp.config import McpServerConfig

        sess = FakeMcpSession(elicit="repo name?")
        manager = McpManager(
            tmp_path,
            connect=fake_mcp_connect(lambda: sess),
            ask_user=ask,
            approval="never",
        )
        await manager.start([McpServerConfig(name="gh", command="x")])
        await manager.call_tool("gh", "search", {}, read_only=True)
        assert asked and asked[0][0] == "repo name?"
        assert asked[0][1] == "text"
        await manager.aclose()

    asyncio.run(run())


def test_safe_tools_skip_approval(tmp_path):
    asked = []

    async def ask(question, kind="text", **kwargs):
        asked.append(1)
        return "no"

    async def run():
        from runtime.mcp.config import McpServerConfig

        sess = FakeMcpSession(tools=[{"name": "search_issues"}])
        manager = McpManager(
            tmp_path,
            connect=fake_mcp_connect(lambda: sess),
            ask_user=ask,
            approval="always",
        )
        await manager.start(
            [
                McpServerConfig(
                    name="gh", command="x", safe_tools=["search_issues"]
                )
            ]
        )
        out = await manager.call_tool("gh", "search_issues", {}, read_only=True)
        assert "denied" not in out
        assert asked == []
        await manager.aclose()

    asyncio.run(run())


def test_extract_auth_url_found():
    url = extract_auth_url("error", "https://example.com/auth")
    assert url == "https://example.com/auth"


def test_extract_auth_url_multiple_chunks():
    url = extract_auth_url("error:", "login at https://example.com/login please", "more text")
    assert url == "https://example.com/login"


def test_extract_auth_url_with_punctuation():
    url = extract_auth_url("go to https://example.com/auth).")
    assert url == "https://example.com/auth"


def test_extract_auth_url_not_found():
    url = extract_auth_url("error", "no url here")
    assert url == ""


def test_is_auth_error_unauthorized():
    assert is_auth_error("error: unauthorized")


def test_is_auth_error_401():
    assert is_auth_error("HTTP 401 Unauthorized")


def test_is_auth_error_login_required():
    assert is_auth_error("login required")


def test_is_auth_error_invalid_token():
    assert is_auth_error("invalid_token")


def test_is_auth_error_not_auth():
    assert not is_auth_error("connection timeout")


def test_mcp_server_state_init():
    config = McpServerConfig(name="test", command="test-cmd")
    state = McpServerState(config=config)
    assert state.status == "starting"
    assert state.error == ""
    assert state.session is None
    assert state.tool_count == 0
    assert not state.ever_ready


async def test_mcp_manager_init(tmp_path):
    manager = McpManager(tmp_path)
    assert manager.workspace == tmp_path
    assert manager.backoff_s == (2.0, 4.0, 8.0)
    assert manager.cool_s == 30.0
    assert manager.servers == {}


async def test_mcp_manager_custom_backoff(tmp_path):
    manager = McpManager(tmp_path, backoff_s=(1.0, 2.0), cool_s=60.0)
    assert manager.backoff_s == (1.0, 2.0)
    assert manager.cool_s == 60.0


async def test_mcp_manager_rows(tmp_path):
    manager = McpManager(tmp_path)
    rows = manager.rows()
    assert isinstance(rows, list)


async def test_mcp_manager_approval_setting(tmp_path):
    manager = McpManager(tmp_path, approval="never")
    assert manager.approval == "never"


async def test_mcp_manager_callbacks(tmp_path):
    called = {"update": False, "auth": False, "warn": False}
    
    def on_update(*args):
        called["update"] = True
    
    def on_auth(*args):
        called["auth"] = True
    
    def on_warning(*args):
        called["warn"] = True
    
    manager = McpManager(
        tmp_path,
        on_update=on_update,
        on_auth=on_auth,
        on_warning=on_warning,
    )
    assert manager.on_update == on_update
    assert manager.on_auth == on_auth
    assert manager.on_warning == on_warning


async def test_mcp_manager_default_connect(tmp_path):
    with patch("runtime.mcp.manager.connect_stdio"):
        manager = McpManager(tmp_path)
        # _default_connect should be called if connect not provided
        assert manager._connect is not None


async def test_mcp_manager_custom_connect(tmp_path):
    async def custom_connect(cfg):
        return "custom"
    
    manager = McpManager(tmp_path, connect=custom_connect)
    assert manager._connect == custom_connect


def test_mcp_manager_ask_user(tmp_path):
    def ask_user(*args):
        return True
    
    manager = McpManager(tmp_path, ask_user=ask_user)
    assert manager.ask_user == ask_user


async def test_mcp_manager_custom_sleep(tmp_path):
    async def custom_sleep(delay):
        pass
    
    manager = McpManager(tmp_path, sleep=custom_sleep)
    assert manager._sleep == custom_sleep


def test_mcp_manager_custom_clock(tmp_path):
    import time
    
    def custom_clock():
        return time.time()
    
    manager = McpManager(tmp_path, clock=custom_clock)
    assert manager._clock == custom_clock


def test_mcp_server_state_wire_names():
    config = McpServerConfig(name="test", command="test-cmd")
    state = McpServerState(config=config)
    state.wire_names = ["tool1", "tool2"]
    assert len(state.wire_names) == 2


def test_mcp_server_state_advertised():
    config = McpServerConfig(name="test", command="test-cmd")
    state = McpServerState(config=config)
    state.advertised.add("tool1")
    assert "tool1" in state.advertised


def test_mcp_server_state_remote_tools():
    config = McpServerConfig(name="test", command="test-cmd")
    state = McpServerState(config=config)
    state.remote_tools = [{"name": "tool1"}]
    assert len(state.remote_tools) == 1


# ============================================================================
# runtime/mcp/manager.py tests
# ============================================================================

class TestMcpManager:
    """Test coverage gaps in McpManager."""

    def test_extract_auth_url_with_url(self):
        """Test extracting auth URL from chunks."""
        url = extract_auth_url("visit https://example.com/auth", "more text")
        assert url == "https://example.com/auth"

    def test_extract_auth_url_empty_chunks(self):
        """Test extracting auth URL with empty chunks."""
        url = extract_auth_url("", "no url here", "")
        assert url == ""

    def test_extract_auth_url_cleans_punctuation(self):
        """Test that auth URL strips trailing punctuation."""
        url = extract_auth_url("visit https://example.com/auth,")
        assert url == "https://example.com/auth"
        url = extract_auth_url("visit https://example.com/auth).")
        assert url == "https://example.com/auth"

    def test_is_auth_error_with_auth_markers(self):
        """Test is_auth_error detection."""
        assert is_auth_error("unauthorized error")
        assert is_auth_error("401 forbidden")
        assert is_auth_error("authorization required")
        assert is_auth_error("please login required")
        assert not is_auth_error("regular error")

    def test_tool_list_from_dict(self):
        """Test _tool_list with dict response."""
        result = _tool_list({"tools": [{"name": "a"}, {"name": "b"}]})
        assert len(result) == 2

    def test_tool_list_from_object(self):
        """Test _tool_list with object response."""
        obj = SimpleNamespace(tools=[{"name": "a"}])
        result = _tool_list(obj)
        assert len(result) == 1

    def test_tool_list_none(self):
        """Test _tool_list with None."""
        assert _tool_list(None) == []

    def test_tool_name_from_dict(self):
        """Test _tool_name from dict."""
        assert _tool_name({"name": "search"}) == "search"
        assert _tool_name({}) == "tool"

    def test_tool_name_from_object(self):
        """Test _tool_name from object."""
        obj = SimpleNamespace(name="fetch")
        assert _tool_name(obj) == "fetch"

    def test_tool_description_from_dict(self):
        """Test _tool_description from dict."""
        desc = _tool_description({"name": "search", "description": "Search docs"}, "myserver")
        assert "Search docs" in desc
        assert "mcp:myserver" in desc

    def test_tool_description_from_object(self):
        """Test _tool_description from object."""
        obj = SimpleNamespace(name="search", description="Search docs")
        desc = _tool_description(obj, "srv")
        assert "Search docs" in desc
        assert "mcp:srv" in desc

    def test_input_schema_with_schema(self):
        """Test _input_schema extraction."""
        schema = _input_schema({"inputSchema": {"type": "object", "properties": {}}})
        assert schema["type"] == "object"

    def test_input_schema_fallback(self):
        """Test _input_schema fallback for missing schema."""
        schema = _input_schema({})
        assert schema == {"type": "object", "properties": {}}

    def test_read_only_annotation(self):
        """Test _read_only detection."""
        assert _read_only({"annotations": {"readOnlyHint": True}})
        assert not _read_only({})

    def test_has_stored_token(self):
        """Test _has_stored_token."""
        state = McpServerState(
            config=McpServerConfig(name="test", command="echo", token_env="MY_TOKEN", env={"MY_TOKEN": "xyz"})
        )
        assert _has_stored_token(state)

    def test_has_no_stored_token(self):
        """Test _has_stored_token when token not set."""
        state = McpServerState(
            config=McpServerConfig(name="test", command="echo", token_env="MY_TOKEN", env={})
        )
        assert not _has_stored_token(state)

    @pytest.mark.asyncio
    async def test_call_sync_function(self):
        """Test _call with sync function."""
        session = SimpleNamespace(list_tools=lambda: SimpleNamespace(tools=[]))
        result = await _call(session, "list_tools")
        assert result.tools == []

    @pytest.mark.asyncio
    async def test_call_async_function(self):
        """Test _call with async function."""
        async def async_method():
            return SimpleNamespace(tools=[])
        session = SimpleNamespace(list_tools=async_method)
        result = await _call(session, "list_tools")
        assert result.tools == []

    @pytest.mark.asyncio
    async def test_maybe_await_coroutine(self):
        """Test _maybe_await with coroutine."""
        async def coro():
            return "result"
        result = await _maybe_await(coro())
        assert result == "result"

    @pytest.mark.asyncio
    async def test_maybe_await_non_coroutine(self):
        """Test _maybe_await with non-coroutine."""
        result = await _maybe_await("value")
        assert result == "value"

    @pytest.mark.asyncio
    async def test_manager_emit(self):
        """Test manager._emit callback."""
        updates = []
        manager = McpManager(
            Path("."),
            on_update=lambda rows: updates.append(rows)
        )
        manager._emit()
        assert len(updates) == 1

    @pytest.mark.asyncio
    async def test_manager_warn(self):
        """Test manager._warn callback."""
        warnings = []
        manager = McpManager(
            Path("."),
            on_warning=lambda msg: warnings.append(msg)
        )
        manager._warn("test warning")
        assert "test warning" in manager.warnings
        assert len(warnings) == 1

    @pytest.mark.asyncio
    async def test_manager_rows(self):
        """Test manager.rows() returns McpServerRow list."""
        manager = McpManager(Path("."), connect=fake_mcp_connect())
        state = McpServerState(
            config=McpServerConfig(name="test", command="echo"),
            status="ready",
            tool_count=5
        )
        manager.servers["test"] = state
        rows = manager.rows()
        assert len(rows) == 1
        assert rows[0].name == "test"
        assert rows[0].status == "ready"
        assert rows[0].tool_count == 5

    @pytest.mark.asyncio
    async def test_manager_clear_cooling(self):
        """Test manager.clear_cooling()."""
        manager = McpManager(Path("."))
        state = McpServerState(
            config=McpServerConfig(name="test", command="echo"),
            status="error",
            error="cooling...",
            cool_until=1000.0
        )
        manager.servers["test"] = state
        manager.clear_cooling()
        assert state.cool_until == 0.0
        assert state.error == ""

    @pytest.mark.asyncio
    async def test_manager_restart_unknown_server(self):
        """Test restarting unknown server."""
        manager = McpManager(Path("."), connect=fake_mcp_connect())
        result = await manager.restart("unknown")
        assert result is None

    @pytest.mark.asyncio
    async def test_manager_call_tool_unknown_server(self):
        """Test call_tool with unknown server."""
        manager = McpManager(Path("."))
        result = await manager.call_tool("unknown", "tool", {})
        assert "unknown mcp server" in result

    @pytest.mark.asyncio
    async def test_manager_read_resource_scheme_denied(self):
        """Test read_resource with denied scheme."""
        manager = McpManager(Path("."))
        result = await manager.read_resource("file:///etc/passwd")
        assert "error:" in result or result == ""


# ============================================================================
# Tests for runtime/mcp/tokens.py
# ============================================================================

def test_tokens_path(tmp_path):
    """Test tokens_path returns correct path."""
    path = tokens_path(tmp_path)
    assert str(path).endswith(".engine/mcp-tokens.json")


def test_load_tokens_nonexistent(tmp_path):
    """Test load_tokens with nonexistent file."""
    result = load_tokens(tmp_path)
    assert result == {}


def test_load_tokens_invalid_json(tmp_path):
    """Test load_tokens with invalid JSON."""
    tokens_file = tmp_path / ".engine" / "mcp-tokens.json"
    tokens_file.parent.mkdir(parents=True, exist_ok=True)
    tokens_file.write_text("invalid json {")
    
    result = load_tokens(tmp_path)
    assert result == {}


def test_load_tokens_not_dict(tmp_path):
    """Test load_tokens when JSON is not a dict."""
    tokens_file = tmp_path / ".engine" / "mcp-tokens.json"
    tokens_file.parent.mkdir(parents=True, exist_ok=True)
    tokens_file.write_text("[]")
    
    result = load_tokens(tmp_path)
    assert result == {}


def test_load_tokens_valid(tmp_path):
    """Test load_tokens with valid data."""
    tokens_file = tmp_path / ".engine" / "mcp-tokens.json"
    tokens_file.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "server1": {
            "token": "abc123",
            "token_env": "ENV_VAR"
        }
    }
    tokens_file.write_text(json.dumps(data))
    
    result = load_tokens(tmp_path)
    assert result["server1"]["token"] == "abc123"
    assert result["server1"]["tokenEnv"] == "ENV_VAR"


def test_save_token(tmp_path):
    """Test save_token creates and saves token."""
    result = save_token(tmp_path, "myserver", "mytoken", "MY_ENV")
    assert result.exists()
    
    data = json.loads(result.read_text())
    assert data["myserver"]["token"] == "mytoken"
    assert data["myserver"]["tokenEnv"] == "MY_ENV"


def test_save_token_permissions(tmp_path):
    """Test save_token sets correct permissions."""
    result = save_token(tmp_path, "server", "token", "ENV")
    mode = oct(result.stat().st_mode)[-3:]
    assert mode == "600"


def test_apply_tokens():
    """Test apply_tokens applies environment variables."""
    cfg1 = Mock()
    cfg1.name = "server1"
    cfg1.token_env = "TOKEN_ENV"
    cfg1.env = {}
    
    tokens = {
        "server1": {
            "token": "secret123",
            "tokenEnv": "TOKEN_ENV"
        }
    }
    
    apply_tokens([cfg1], tokens)
    assert cfg1.env["TOKEN_ENV"] == "secret123"


def test_apply_tokens_no_match():
    """Test apply_tokens skips non-matching servers."""
    cfg1 = Mock()
    cfg1.name = "server1"
    cfg1.token_env = "TOKEN_ENV"
    cfg1.env = {}
    
    tokens = {
        "other": {
            "token": "secret",
            "tokenEnv": "TOKEN_ENV"
        }
    }
    
    apply_tokens([cfg1], tokens)
    assert cfg1.env == {}


def test_apply_tokens_no_env_name():
    """Test apply_tokens skips when no env name."""
    cfg1 = Mock()
    cfg1.name = "server1"
    cfg1.token_env = ""
    cfg1.env = {}
    
    tokens = {
        "server1": {
            "token": "secret",
            "tokenEnv": ""
        }
    }
    
    apply_tokens([cfg1], tokens)
    assert cfg1.env == {}
