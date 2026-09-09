from __future__ import annotations

import html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

MAX_FETCH = 50_000
USER_AGENT = "engine-researcher/1.0"

_TAG = re.compile(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>|<[^>]+>", re.I)
_SPACE = re.compile(r"\s+")


def web_fetch(url: str, *, timeout: float = 20.0) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return "error: url must be http or https"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_FETCH + 1)
            content_type = response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return f"error: HTTP {exc.code} {exc.reason}"
    except urllib.error.URLError as exc:
        return f"error: {exc.reason}"
    except TimeoutError:
        return "error: fetch timed out"
    except OSError as exc:
        return f"error: {exc}"
    truncated = len(raw) > MAX_FETCH
    raw = raw[:MAX_FETCH]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
    if "html" in content_type.lower() or text.lstrip()[:15].lower().startswith("<!doctype html") or text.lstrip().lower().startswith("<html"):
        text = _SPACE.sub(" ", html.unescape(_TAG.sub(" ", text))).strip()
    if truncated:
        text += "\n...[truncated]"
    return text or "(empty)"


def web_search(query: str, *, count: int = 5) -> str:
    key = (os.environ.get("BRAVE_API_KEY") or "").strip()
    if not key:
        return (
            "error: web_search unavailable (set BRAVE_API_KEY); "
            "pass a URL to web_fetch instead"
        )
    count = max(1, min(int(count or 5), 10))
    params = urllib.parse.urlencode({"q": query, "count": str(count)})
    request = urllib.request.Request(
        "https://api.search.brave.com/res/v1/web/search?" + params,
        headers={
            "Accept": "application/json",
            "X-Subscription-Token": key,
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return f"error: HTTP {exc.code} {exc.reason}"
    except urllib.error.URLError as exc:
        return f"error: {exc.reason}"
    except (OSError, json.JSONDecodeError, TimeoutError) as exc:
        return f"error: {exc}"
    results = ((payload.get("web") or {}).get("results")) or []
    if not results:
        return "(no results)"
    lines = []
    for item in results[:count]:
        title = item.get("title") or ""
        url = item.get("url") or ""
        desc = item.get("description") or ""
        lines.append(f"{title}\n{url}\n{desc}".strip())
    return "\n\n".join(lines)
