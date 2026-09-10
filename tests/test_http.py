from __future__ import annotations

import asyncio
from types import SimpleNamespace

from runtime.tools import httpx as http_impl
from runtime.tools.httpx import blocked_host, http_request, openapi_ops
from tools import http as http_tool
from tools.http import http_request as http_request_tool


def test_http_request_rejects_file():
    result = http_request("GET", "file:///etc/passwd")
    assert result.startswith("error:")
    assert "http" in result


def test_http_request_blocks_metadata():
    result = http_request("GET", "http://169.254.169.254/latest/meta-data")
    assert result.startswith("error: blocked host")
    result = http_request("GET", "http://metadata.google.internal/")
    assert result.startswith("error: blocked host")


def test_blocked_host_allows_loopback():
    assert not blocked_host("127.0.0.1")
    assert not blocked_host("localhost:8000")
    assert blocked_host("169.254.169.254:80")


def test_http_request_get(monkeypatch):
    monkeypatch.setattr(
        http_impl,
        "raw_request",
        lambda *a, **k: (200, {"Content-Type": "text/plain"}, "ok", ""),
    )
    text = http_request("GET", "https://example.com")
    assert "status: 200" in text
    assert "ok" in text


def test_openapi_ops_json(monkeypatch):
    spec = {
        "paths": {
            "/users": {
                "get": {"summary": "List users"},
                "post": {"summary": "Create user"},
            }
        }
    }
    import json

    monkeypatch.setattr(
        http_impl,
        "raw_request",
        lambda *a, **k: (200, {}, json.dumps(spec), ""),
    )
    text = openapi_ops("https://example.com/openapi.json")
    assert "GET /users — List users" in text
    assert "POST /users — Create user" in text


def test_http_post_denied(ctx):
    async def no(_question, kind="text"):
        return "no"

    ctx.ask_user = no
    ctx.config = SimpleNamespace(exec_approval="auto")

    async def run():
        return await http_request_tool(
            ctx, "POST", "https://example.com/x", body="{}"
        )

    assert asyncio.run(run()).startswith("error: user denied")


def test_http_get_skips_approval(ctx, monkeypatch):
    called = []

    async def ask(question, kind="text"):
        called.append(question)
        return "no"

    ctx.ask_user = ask
    monkeypatch.setattr(
        http_tool,
        "http_request_impl",
        lambda *a, **k: "status: 200\n\nok",
    )

    async def run():
        return await http_request_tool(ctx, "GET", "https://example.com")

    assert asyncio.run(run()) == "status: 200\n\nok"
    assert called == []
