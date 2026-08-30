from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass
class LspLocation:
    path: str
    line: int
    col: int


class LspManager:
    """Holds an optional language-server connection.

    The engine can run without a server. Tools then return a structured
    unavailable result. Wire a real JSON-RPC client here later.
    """

    def __init__(self) -> None:
        self.available = False
        self.reason = "no language server is attached"

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        del method, params
        return {"ok": False, "reason": self.reason}

    async def diagnostics(self, path: str) -> list[dict[str, Any]]:
        del path
        return []

    async def definition(self, path: str, line: int, col: int) -> list[LspLocation]:
        del path, line, col
        return []

    async def references(self, path: str, line: int, col: int) -> list[LspLocation]:
        del path, line, col
        return []

    async def hover(self, path: str, line: int, col: int) -> str:
        del path, line, col
        return ""

    async def document_symbols(self, path: str) -> list[dict[str, Any]]:
        del path
        return []


def dump(value: Any) -> str:
    return json.dumps(value, indent=2)
