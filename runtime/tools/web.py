from __future__ import annotations

import html as html_lib
import json
import os
import re
import urllib.parse
from html.parser import HTMLParser

MAX_FETCH = 50_000
USER_AGENT = "engine-researcher/1.0"
BROWSER_UA = "Mozilla/5.0 (compatible; engine-researcher/1.0)"
HTML_RAW_CAP = 200_000
MARKDOWN_CAP = 20_000
SPA_MIN_HTML = 1500
SPA_MIN_EXTRACT = 120

_SKIP = {
    "script",
    "style",
    "nav",
    "footer",
    "header",
    "aside",
    "form",
    "noscript",
    "svg",
    "iframe",
    "template",
    "button",
}
_VOID = {"br", "hr", "img", "meta", "link", "input"}
_HEADINGS = {f"h{i}": i for i in range(1, 7)}
_BLOCK = {
    "p",
    "div",
    "section",
    "article",
    "main",
    "li",
    "tr",
    "blockquote",
    "pre",
    "ul",
    "ol",
    "table",
    "thead",
    "tbody",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
}
_SPACE = re.compile(r"[ \t]+")
_BLANK = re.compile(r"\n{3,}")
_GH_HOST = re.compile(r"^(?:www\.)?github\.com$", re.I)
_GH_RESERVED = {
    "login",
    "settings",
    "marketplace",
    "explore",
    "topics",
    "orgs",
    "search",
    "notifications",
    "about",
    "features",
    "pricing",
    "sponsors",
    "customer-stories",
}


def web_fetch(url: str, *, timeout: float = 20.0) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return "error: url must be http or https"
    hint = github_fetch_hint(url)
    if hint:
        return hint
    from runtime.tools.httpx import raw_request

    status, headers, text, err = raw_request(
        "GET",
        url,
        headers={"User-Agent": BROWSER_UA, "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8"},
        timeout=timeout,
        cap=HTML_RAW_CAP,
    )
    if err:
        return err
    if status >= 400:
        return f"error: HTTP {status}"
    content_type = ""
    for key, value in (headers or {}).items():
        if key.lower() == "content-type":
            content_type = value or ""
            break
    ctype = content_type.lower()
    if "json" in ctype:
        return _clip(text, MARKDOWN_CAP)
    if "html" in ctype or _looks_like_html(text):
        return _from_html(url, text)
    return _clip(text, MARKDOWN_CAP) or "(empty)"


def github_fetch_hint(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.netloc or "").split("@")[-1]
    if host.startswith("[") or ":" in host and host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    if not _GH_HOST.match(host):
        return ""
    parts = [urllib.parse.unquote(p) for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        return ""
    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    if owner.lower() in _GH_RESERVED:
        return ""
    spec = f"{owner}/{repo}"
    extra = ""
    if len(parts) >= 3:
        action = parts[2].lower()
        if action == "blob" and len(parts) >= 5:
            ref = parts[3]
            path = "/".join(parts[4:])
            extra = f" github_file repo={spec} path={path} ref={ref}."
        elif action == "tree":
            ref = parts[3] if len(parts) >= 4 else ""
            path = "/".join(parts[4:]) if len(parts) >= 5 else ""
            bits = [f"github_tree repo={spec}"]
            if path:
                bits.append(f"path={path}")
            if ref:
                bits.append(f"ref={ref}")
            extra = " " + " ".join(bits) + "."
        elif action in {"issues", "issue"} and len(parts) >= 4:
            extra = f" gh_issue_view repo={spec} number={parts[3]}."
        elif action in {"pull", "pulls"} and len(parts) >= 4:
            extra = f" gh_pr_view repo={spec} number={parts[3]}."
        elif action in {"issues", "pull", "pulls"}:
            extra = f" github_repo repo={spec}."
    if not extra:
        extra = f" github_repo repo={spec}."
    return (
        f"error: use github_repo / github_tree / github_file for {spec}."
        f"{extra} Do not web_fetch github.com HTML."
    )


def web_search(query: str, *, count: int = 5) -> str:
    key = (os.environ.get("BRAVE_API_KEY") or "").strip()
    if not key:
        return (
            "error: web_search unavailable (set BRAVE_API_KEY); "
            "pass a URL to web_fetch instead"
        )
    count = max(1, min(int(count or 5), 10))
    params = urllib.parse.urlencode({"q": query, "count": str(count)})
    from runtime.tools.httpx import raw_request

    status, _headers, text, err = raw_request(
        "GET",
        "https://api.search.brave.com/res/v1/web/search?" + params,
        headers={
            "Accept": "application/json",
            "X-Subscription-Token": key,
            "User-Agent": USER_AGENT,
        },
        timeout=20.0,
        cap=MAX_FETCH,
    )
    if err:
        return err
    if status >= 400:
        reason = ""
        try:
            payload = json.loads(text or "{}")
            reason = str((payload or {}).get("message") or "")
        except json.JSONDecodeError:
            reason = (text or "")[:80]
        extra = f" {reason}" if reason else ""
        return f"error: HTTP {status}{extra}".rstrip()
    try:
        payload = json.loads(text or "null")
    except json.JSONDecodeError as exc:
        return f"error: {exc}"
    results = ((payload.get("web") or {}).get("results")) or []
    if not results:
        return "(no results)"
    blocks = []
    for index, item in enumerate(results[:count], start=1):
        title = item.get("title") or ""
        url = item.get("url") or ""
        desc = item.get("description") or ""
        lines = [f"[{index}] {title}".rstrip(), f"url: {url}".rstrip()]
        if desc:
            lines.append(desc)
        extras = item.get("extra_snippets") or []
        if isinstance(extras, list):
            for snippet in extras[:2]:
                if snippet:
                    lines.append(str(snippet))
        blocks.append("\n".join(line for line in lines if line))
    return "\n\n".join(blocks)


def _looks_like_html(text: str) -> bool:
    head = (text or "").lstrip()[:200].lower()
    return head.startswith("<!doctype html") or head.startswith("<html") or "<html" in head[:80]


def _from_html(url: str, text: str) -> str:
    title, extracted = html_to_markdown(text)
    body = (extracted or "").strip()
    if len(text) >= SPA_MIN_HTML and len(body) < SPA_MIN_EXTRACT:
        return (
            "error: page looks client-rendered. For GitHub use github_repo / "
            "github_tree / github_file. For a live UI spawn debugger (browser_open)."
        )
    lines = []
    if title:
        lines.append(f"title: {title}")
    lines.append(f"url: {url}")
    if body:
        lines.append("")
        lines.append(body)
    return _clip("\n".join(lines).strip(), MARKDOWN_CAP) or "(empty)"


def html_to_markdown(text: str) -> tuple[str, str]:
    parser = _MarkdownHTML()
    try:
        parser.feed(text or "")
        parser.close()
    except Exception:
        fallback = _SPACE.sub(" ", html_lib.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()
        return "", fallback
    title = _SPACE.sub(" ", parser.title).strip()
    preferred = _normalize_md(parser.preferred)
    general = _normalize_md(parser.general)
    body = preferred if len(preferred) >= min(80, len(general) // 3 + 1) and preferred else general
    return title, body


def _normalize_md(text: str) -> str:
    text = html_lib.unescape(text or "")
    text = _SPACE.sub(" ", text)
    text = text.replace(" \n", "\n").replace("\n ", "\n")
    text = _BLANK.sub("\n\n", text)
    return text.strip()


def _clip(text: str, cap: int) -> str:
    text = text or ""
    if len(text) <= cap:
        return text
    return text[:cap] + "\n...[truncated]"


class _MarkdownHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.general = ""
        self.preferred = ""
        self._skip = 0
        self._in_title = False
        self._in_pre = False
        self._href = ""
        self._main_depth = 0
        self._title_parts: list[str] = []
        self._link_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        attrs_d = {k.lower(): v or "" for k, v in attrs}
        if tag in _SKIP:
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "title":
            self._in_title = True
            return
        if tag in {"article", "main"}:
            self._main_depth += 1
        if tag in _HEADINGS:
            self._write("\n\n" + "#" * _HEADINGS[tag] + " ")
        elif tag == "p":
            self._write("\n\n")
        elif tag == "br":
            self._write("\n")
        elif tag == "hr":
            self._write("\n\n---\n\n")
        elif tag == "li":
            self._write("\n- ")
        elif tag == "pre":
            self._in_pre = True
            self._write("\n\n```\n")
        elif tag == "code" and not self._in_pre:
            self._write("`")
        elif tag == "blockquote":
            self._write("\n\n> ")
        elif tag == "a":
            self._href = attrs_d.get("href") or ""
            self._link_parts = []
        elif tag in _BLOCK:
            self._write("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP:
            if self._skip:
                self._skip -= 1
            return
        if self._skip:
            return
        if tag == "title":
            self._in_title = False
            self.title = "".join(self._title_parts)
            return
        if tag in {"article", "main"} and self._main_depth:
            self._main_depth -= 1
        if tag in _HEADINGS or tag in {"p", "blockquote"}:
            self._write("\n\n")
        elif tag == "pre":
            self._in_pre = False
            self._write("\n```\n\n")
        elif tag == "code" and not self._in_pre:
            self._write("`")
        elif tag == "a":
            label = _SPACE.sub(" ", "".join(self._link_parts)).strip()
            href = self._href
            self._href = ""
            self._link_parts = []
            if label and href:
                self._write(f"[{label}]({href})")
            elif label:
                self._write(label)
        elif tag in _BLOCK and tag not in _VOID:
            self._write("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
            return
        if self._skip or not data:
            return
        if self._href:
            self._link_parts.append(data)
            return
        if self._in_pre:
            self._write(data)
            return
        self._write(_SPACE.sub(" ", data))

    def _write(self, chunk: str) -> None:
        if not chunk:
            return
        self.general += chunk
        if self._main_depth:
            self.preferred += chunk
