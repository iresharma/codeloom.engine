from __future__ import annotations

from runtime.tools.web import web_fetch, web_search


def test_web_fetch_rejects_non_http():
    assert web_fetch("file:///etc/passwd").startswith("error:")
    assert web_fetch("not-a-url").startswith("error:")


def test_web_search_without_key(monkeypatch):
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    result = web_search("python asyncio")
    assert result.startswith("error:")
    assert "BRAVE_API_KEY" in result
