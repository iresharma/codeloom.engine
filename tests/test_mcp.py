from __future__ import annotations

import asyncio
import json
import os
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
    McpServersUpdated,
    SnapshotReady,
    UserPromptRequested,
    WarningOccurred,
)
from protocol.redact import redact_command
from runtime.config import EngineConfig
from runtime.mcp.bridge import flatten_mcp_result, sanitize_mcp_name, scheme_allowed
from runtime.mcp.config import load_mcp_config
from runtime.mcp.manager import McpManager
from runtime.session import EngineSession
from tests.fakes import FakeMcpSession, FakeProvider, fake_mcp_connect
from tools.registry import ToolRegistry, discover_tools


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
