from __future__ import annotations

import json

from runtime.tools.web import (
    HTML_RAW_CAP,
    github_fetch_hint,
    html_to_markdown,
    web_fetch,
    web_search,
)


def test_web_fetch_rejects_non_http():
    assert web_fetch("file:///etc/passwd").startswith("error:")
    assert web_fetch("not-a-url").startswith("error:")


def test_web_search_without_key(monkeypatch):
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    result = web_search("python asyncio")
    assert result.startswith("error:")
    assert "BRAVE_API_KEY" in result


def test_github_url_does_not_fetch(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("should not fetch github.com HTML")

    monkeypatch.setattr("runtime.tools.httpx.raw_request", boom)
    text = web_fetch("https://github.com/acme/engine")
    assert text.startswith("error:")
    assert "github_repo" in text
    assert "acme/engine" in text
    assert "github_file" in github_fetch_hint(
        "https://github.com/acme/engine/blob/main/src/app.py"
    )
    assert "path=src/app.py" in github_fetch_hint(
        "https://github.com/acme/engine/blob/main/src/app.py"
    )
    assert "github_tree" in github_fetch_hint(
        "https://github.com/acme/engine/tree/main/src"
    )
    assert "gh_issue_view" in github_fetch_hint(
        "https://github.com/acme/engine/issues/12"
    )
    assert "gh_pr_view" in github_fetch_hint("https://www.github.com/acme/engine/pull/3")
    assert github_fetch_hint("https://github.com/login") == ""
    assert github_fetch_hint("https://raw.githubusercontent.com/acme/engine/main/README.md") == ""
    assert github_fetch_hint("https://example.com/acme/engine") == ""


def test_html_to_markdown_drops_nav():
    raw = """
    <html><head><title>Docs</title></head>
    <body>
      <nav>Home About</nav>
      <main>
        <h1>Install</h1>
        <p>Run <code>pip install x</code>.</p>
        <a href="https://example.com/guide">guide</a>
      </main>
    </body></html>
    """
    title, md = html_to_markdown(raw)
    assert title == "Docs"
    assert "# Install" in md
    assert "pip install x" in md
    assert "[guide](https://example.com/guide)" in md
    assert "Home About" not in md
    assert "cookie" not in md.lower()


def test_html_to_markdown_lists_and_pre():
    raw = """
    <html><body><article>
      <ul><li>one</li><li>two</li></ul>
      <pre><code>print(1)</code></pre>
      <footer>cookie banner</footer>
    </article></body></html>
    """
    _title, md = html_to_markdown(raw)
    assert "- one" in md
    assert "- two" in md
    assert "```" in md
    assert "print(1)" in md
    assert "cookie banner" not in md


def test_web_fetch_html_markdown(monkeypatch):
    html = """<!doctype html><html><head><title>Hello</title></head>
    <body><article><h1>API</h1><p>Version 3.</p></article></body></html>"""

    def fake_request(method, url, **kwargs):
        return 200, {"Content-Type": "text/html"}, html, ""

    monkeypatch.setattr("runtime.tools.httpx.raw_request", fake_request)
    text = web_fetch("https://example.com/docs")
    assert "title: Hello" in text
    assert "url: https://example.com/docs" in text
    assert "# API" in text
    assert "Version 3" in text


def test_web_fetch_spa_hint(monkeypatch):
    html = (
        "<!doctype html><html><head><title>App</title></head>"
        "<body><div id='root'></div>"
        + ("<script>window.__DATA__={}</script>" * 50)
        + "</body></html>"
    )
    assert len(html) > 1500

    def fake_request(method, url, **kwargs):
        return 200, {"Content-Type": "text/html; charset=utf-8"}, html, ""

    monkeypatch.setattr("runtime.tools.httpx.raw_request", fake_request)
    text = web_fetch("https://example.com/app")
    assert text.startswith("error:")
    assert "client-rendered" in text
    assert "browser_open" in text


def test_web_fetch_small_html_is_not_spa(monkeypatch):
    html = "<!doctype html><html><body><p>hi</p></body></html>"

    def fake_request(method, url, **kwargs):
        return 200, {"Content-Type": "text/html"}, html, ""

    monkeypatch.setattr("runtime.tools.httpx.raw_request", fake_request)
    text = web_fetch("https://example.com/tiny")
    assert not text.startswith("error:")
    assert "hi" in text


def test_web_fetch_json_and_plain(monkeypatch):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append(kwargs)
        if "json" in url:
            return 200, {"Content-Type": "application/json"}, '{"ok": true}', ""
        return 200, {"Content-Type": "text/plain"}, "hello world", ""

    monkeypatch.setattr("runtime.tools.httpx.raw_request", fake_request)
    assert '{"ok": true}' in web_fetch("https://example.com/x.json")
    assert web_fetch("https://example.com/note.txt") == "hello world"
    headers = (calls[0].get("headers") or {})
    assert "engine-researcher" in headers.get("User-Agent", "")
    assert calls[0].get("cap") == HTML_RAW_CAP


def test_web_fetch_http_error(monkeypatch):
    monkeypatch.setattr(
        "runtime.tools.httpx.raw_request",
        lambda *a, **k: (404, {}, "missing", ""),
    )
    assert web_fetch("https://example.com/gone").startswith("error: HTTP 404")


def test_web_fetch_blocked_host():
    result = web_fetch("http://169.254.169.254/latest/meta-data")
    assert result.startswith("error: blocked host")


def test_web_fetch_markdown_cap(monkeypatch):
    from runtime.tools.web import MARKDOWN_CAP

    body = "x" * (MARKDOWN_CAP + 80)

    def fake_request(method, url, **kwargs):
        return 200, {"Content-Type": "text/plain"}, body, ""

    monkeypatch.setattr("runtime.tools.httpx.raw_request", fake_request)
    text = web_fetch("https://example.com/big.txt")
    assert text.endswith("...[truncated]")
    assert len(text) < MARKDOWN_CAP + 30


def test_web_search_formats_results(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    payload = {
        "web": {
            "results": [
                {
                    "title": "Asyncio docs",
                    "url": "https://docs.python.org/3/library/asyncio.html",
                    "description": "Event loops",
                    "extra_snippets": ["gather runs tasks"],
                }
            ]
        }
    }

    def fake_request(method, url, **kwargs):
        return 200, {"Content-Type": "application/json"}, json.dumps(payload), ""

    monkeypatch.setattr("runtime.tools.httpx.raw_request", fake_request)
    text = web_search("python asyncio")
    assert "[1] Asyncio docs" in text
    assert "url: https://docs.python.org/3/library/asyncio.html" in text
    assert "Event loops" in text
    assert "gather runs tasks" in text


def test_web_search_empty_and_http_error(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "k")

    def empty(method, url, **kwargs):
        return 200, {}, json.dumps({"web": {"results": []}}), ""

    monkeypatch.setattr("runtime.tools.httpx.raw_request", empty)
    assert web_search("nothing") == "(no results)"

    def boom(method, url, **kwargs):
        return 401, {}, json.dumps({"message": "invalid key"}), ""

    monkeypatch.setattr("runtime.tools.httpx.raw_request", boom)
    text = web_search("python")
    assert text.startswith("error: HTTP 401")
    assert "invalid key" in text


def test_web_search_empty_or_non_object_body(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "k")

    def blank(method, url, **kwargs):
        return 200, {}, "", ""

    monkeypatch.setattr("runtime.tools.httpx.raw_request", blank)
    assert web_search("nothing") == "(no results)"

    def null_body(method, url, **kwargs):
        return 200, {}, "null", ""

    monkeypatch.setattr("runtime.tools.httpx.raw_request", null_body)
    assert web_search("nothing") == "(no results)"

    def array_body(method, url, **kwargs):
        return 200, {}, "[]", ""

    monkeypatch.setattr("runtime.tools.httpx.raw_request", array_body)
    assert web_search("nothing") == "(no results)"
