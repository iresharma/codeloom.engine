"""Optional host-owned research transport; native direct transport is the default."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


def request(operation: str, arguments: dict) -> str | None:
    endpoint = os.environ.get("ENGINE_RESEARCH_GATEWAY", "").strip()
    if not endpoint:
        return None
    body = json.dumps({"operation": operation, "arguments": arguments}).encode()
    req = urllib.request.Request(endpoint, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + os.environ.get("ENGINE_RESEARCH_TOKEN", ""),
    })
    try:
        with urllib.request.urlopen(req, timeout=65) as response:
            payload = json.load(response)
        return payload["text"]
    except (OSError, ValueError, KeyError, urllib.error.URLError) as exc:
        return f"error: research gateway: {exc}"
