from __future__ import annotations

import asyncio
import json
from contextlib import suppress

from protocol.codec import STREAM_LIMIT, ProtocolError, decode_command
from runtime.session import EngineSession

_REASONS = {
    200: "OK",
    400: "Bad Request",
    404: "Not Found",
    500: "Internal Server Error",
}


class HttpServer:
    def __init__(self, session: EngineSession, host: str, port: int):
        self._session = session
        self._host = host
        self._port = port
        self._stopped: asyncio.Event | None = None
        self._server: asyncio.base_events.Server | None = None

    def _stop_event(self) -> asyncio.Event:
        if self._stopped is None:
            self._stopped = asyncio.Event()
        return self._stopped

    @property
    def bound_port(self) -> int | None:
        if self._server is None or not self._server.sockets:
            return None
        return self._server.sockets[0].getsockname()[1]

    async def serve(self) -> None:
        server = await asyncio.start_server(
            self._on_client, host=self._host, port=self._port, limit=STREAM_LIMIT
        )
        self._server = server
        async with server:
            await self._stop_event().wait()

    def stop(self) -> None:
        self._stop_event().set()

    async def _on_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            request_line = await reader.readline()
            if not request_line:
                return
            try:
                method, path, _version = request_line.decode().strip().split(" ", 2)
            except ValueError:
                await self._send_response(
                    writer, 400, json.dumps({"error": "malformed request line"}).encode()
                )
                return

            headers: dict[str, str] = {}
            while True:
                header_line = await reader.readline()
                if header_line in (b"\r\n", b"\n", b""):
                    break
                name, _, value = header_line.decode().partition(":")
                headers[name.strip().lower()] = value.strip()

            if method == "POST" and path == "/command":
                await self._handle_command(reader, writer, headers)
            elif method == "GET" and path == "/events":
                await self._handle_events(writer)
            else:
                await self._send_response(
                    writer, 404, json.dumps({"error": "not found"}).encode()
                )
        except Exception as exc:  # noqa: BLE001
            with suppress(Exception):
                await self._send_response(
                    writer, 500, json.dumps({"error": str(exc)}).encode()
                )
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    async def _handle_command(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        headers: dict[str, str],
    ) -> None:
        try:
            length = int(headers.get("content-length", "0"))
        except ValueError:
            await self._send_response(
                writer, 400, json.dumps({"error": "invalid Content-Length"}).encode()
            )
            return

        try:
            body = await reader.readexactly(length) if length else b""
        except asyncio.IncompleteReadError:
            await self._send_response(
                writer, 400, json.dumps({"error": "incomplete request body"}).encode()
            )
            return

        try:
            command = decode_command(body)
        except (ProtocolError, TypeError, ValueError) as exc:
            await self._send_response(
                writer, 400, json.dumps({"error": str(exc)}).encode()
            )
            return

        queue = self._session.subscribe()
        try:
            await self._session.handle(command)
            events = []
            while True:
                try:
                    events.append(queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
        finally:
            self._session.unsubscribe(queue)

        body_bytes = json.dumps([event.to_json() for event in events]).encode()
        await self._send_response(writer, 200, body_bytes)

    async def _handle_events(self, writer: asyncio.StreamWriter) -> None:
        queue = self._session.subscribe()
        try:
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/event-stream\r\n"
                b"Cache-Control: no-cache\r\n"
                b"Connection: keep-alive\r\n"
                b"\r\n"
            )
            await writer.drain()
            while True:
                event = await queue.get()
                frame = f"data: {json.dumps(event.to_json())}\n\n".encode()
                try:
                    writer.write(frame)
                    await writer.drain()
                except (ConnectionError, BrokenPipeError):
                    break
        except asyncio.CancelledError:
            raise
        finally:
            self._session.unsubscribe(queue)

    async def _send_response(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        body: bytes,
    ) -> None:
        reason = _REASONS.get(status, "")
        headers = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode()
        writer.write(headers + body)
        await writer.drain()
