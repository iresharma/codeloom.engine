from __future__ import annotations

import re
import urllib.parse

from runtime.tools.httpx import fetch_text, get_json
from runtime.tools.pkg import pkg_info
from runtime.tools.web import web_fetch

SOURCES = ("mdn", "pypi", "npm", "crates", "go")
DOCS_CAP = 20_000
_TOPIC = re.compile(r"[^a-zA-Z0-9_-]+")


def docs_lookup(source: str, query: str) -> str:
    source = (source or "").strip().lower()
    query = (query or "").strip()
    if source not in SOURCES:
        return f"error: source must be one of {', '.join(SOURCES)}"
    if not query:
        return "error: query is required"
    if source == "mdn":
        return _mdn(query)
    if source == "go":
        return _go(query)
    return _clip(pkg_info(source, query), DOCS_CAP)


def tldr(topic: str) -> str:
    topic = _TOPIC.sub("", (topic or "").strip().lower())
    if not topic:
        return "error: topic is required"
    for folder in ("common", "linux", "osx"):
        url = (
            "https://raw.githubusercontent.com/tldr-pages/tldr/main/pages/"
            f"{folder}/{topic}.md"
        )
        text = fetch_text(url)
        if text.startswith("error: HTTP 404"):
            continue
        if text.startswith("error:"):
            return text
        return _clip(text, DOCS_CAP)
    return f"error: no tldr page for {topic}"


def _mdn(query: str) -> str:
    params = urllib.parse.urlencode({"q": query, "locale": "en-US"})
    payload, err = get_json("https://developer.mozilla.org/api/v1/search?" + params)
    if err:
        return err
    docs = (payload or {}).get("documents") or []
    if not docs:
        return "(no results)"
    blocks = []
    for item in docs[:8]:
        title = item.get("title") or ""
        path = item.get("mdn_url") or ""
        url = f"https://developer.mozilla.org{path}" if path else ""
        summary = item.get("summary") or ""
        blocks.append("\n".join(line for line in (title, url, summary) if line))
    return _clip("\n\n".join(blocks), DOCS_CAP)


def _go(query: str) -> str:
    if "/" in query or "." in query:
        url = "https://pkg.go.dev/" + urllib.parse.quote(query, safe="/")
    else:
        url = "https://pkg.go.dev/search?" + urllib.parse.urlencode(
            {"q": query, "m": "package"}
        )
    text = web_fetch(url)
    if text.startswith("error:"):
        return text
    return _clip(f"{url}\n{text}", DOCS_CAP)


def _clip(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    return text[:cap] + "\n...[truncated]"
