from __future__ import annotations

import re
from urllib.parse import urlparse

_WIRE = re.compile(r"[^a-zA-Z0-9_]")
_BLOCKED_SCHEMES = {"file", "ftp", "sftp", "unix", "data"}
_ALLOWED_FALLBACK = {"https", "http"}


def sanitize_mcp_name(server: str, tool: str) -> str:
    raw = f"mcp_{server}_{tool}"
    return _WIRE.sub("_", raw)


def flatten_mcp_result(content) -> str:
    blocks = _as_blocks(content)
    parts: list[str] = []
    for block in blocks:
        kind = _block_type(block)
        if kind in {"text", ""}:
            text = _block_attr(block, "text")
            if text:
                parts.append(str(text))
            continue
        mime = _block_attr(block, "mimeType") or _block_attr(block, "mime_type") or kind
        data = _block_attr(block, "data") or _block_attr(block, "blob") or ""
        size = len(data) if data is not None else 0
        parts.append(f"omitted: {mime} {size} bytes; v1 is text-only")
    return "\n".join(parts) if parts else ""


def _as_blocks(content) -> list:
    if content is None:
        return []
    if isinstance(content, str):
        return [content]
    contents = getattr(content, "content", None)
    if contents is None and isinstance(content, list):
        contents = content
    if contents is None:
        return [content]
    return list(contents)


def _block_type(block) -> str:
    if isinstance(block, str):
        return "text"
    if isinstance(block, dict):
        return str(block.get("type") or "")
    return str(getattr(block, "type", "") or "")


def _block_attr(block, name: str):
    if isinstance(block, str):
        return block if name == "text" else None
    if isinstance(block, dict):
        return block.get(name)
    return getattr(block, name, None)


def scheme_allowed(uri: str, advertised: set[str] | None = None) -> str | None:
    parsed = urlparse(uri)
    scheme = (parsed.scheme or "").lower()
    if not scheme:
        return "error: resource URI missing scheme"
    if scheme in _BLOCKED_SCHEMES:
        return f"error: scheme {scheme}:// is not allowed; use read_file for workspace files"
    if advertised and uri in advertised:
        if scheme in _BLOCKED_SCHEMES:
            return f"error: scheme {scheme}:// is not allowed; use read_file for workspace files"
        return None
    if scheme in _ALLOWED_FALLBACK:
        if advertised is not None and uri not in advertised and advertised:
            return f"error: resource not advertised: {uri}"
        return None
    if advertised and uri in advertised:
        return None
    return f"error: scheme {scheme}:// is not allowed"
