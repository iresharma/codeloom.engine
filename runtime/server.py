from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from protocol.commands import parse_command
from protocol.events import Event
from runtime.session import EngineSession


class EngineServer:
    def __init__(self, session: EngineSession, socket_path: Path) -> None:
        self.session = session
        self.socket_path = socket_path
        self._server: asyncio.AbstractServer | None = None
        self._clients: list[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = []

    async def serve(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()
        fanout = self.session.subscribe()
        asyncio.create_task(self._fanout(fanout), name="engine-fanout")
        self._server = await asyncio.start_unix_server(self._on_client, path=str(self.socket_path))
        async with self._server:
            await self._server.serve_forever()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        if self.socket_path.exists():
            self.socket_path.unlink()

    async def _fanout(self, queue: asyncio.Queue[Event]) -> None:
        while True:
            event = await queue.get()
            line = json.dumps(event.to_json(), ensure_ascii=False) + "\n"
            stale = []
            for _, writer in self._clients:
                try:
                    writer.write(line.encode("utf-8"))
                    await writer.drain()
                except Exception:
                    stale.append(writer)
            self._clients = [(reader, writer) for reader, writer in self._clients if writer not in stale]

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._clients.append((reader, writer))
        hello = {
            "ok": True,
            "event": "session_ready",
            "snapshot": self.session.snapshot().to_json(),
        }
        writer.write((json.dumps(hello) + "\n").encode("utf-8"))
        await writer.drain()
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                reply = await self._handle_line(line)
                writer.write((json.dumps(reply) + "\n").encode("utf-8"))
                await writer.drain()
        finally:
            self._clients = [(item_reader, item_writer) for item_reader, item_writer in self._clients if item_writer is not writer]
            writer.close()
            await writer.wait_closed()

    async def _handle_line(self, line: str) -> dict[str, Any]:
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            return {"ok": False, "error": f"invalid json: {exc}"}
        request_id = data.get("id")
        try:
            command = parse_command(data)
            payload = await self.session.handle(command)
            reply: dict[str, Any] = {"ok": True, **(payload or {})}
        except Exception as exc:
            reply = {"ok": False, "error": str(exc)}
        if request_id is not None:
            reply["id"] = request_id
        return reply
