from __future__ import annotations

import asyncio
import json
from contextlib import suppress

from protocol.codec import ProtocolError, decode_command
from runtime.session import EngineSession

_MAX_HEADER_BYTES = 64 * 1024


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

    async def serve(self) -> None:
        server = await asyncio.start_server(
            self._on_client, host=self._host, port=self._port
        )
        self._server = server
        try:
            async with server:
                await self._stop_event().wait()
        finally:
            self._server = None

    def stop(self) -> None:
        self._stop_event().set()

    async def _on_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            request = await self._read_request(reader, writer)
            if request is None:
                return
            method, path, body = request
            if method == "POST" and path == "/command":
                await self._handle_command(writer, body)
            elif method == "GET" and path == "/events":
                await self._handle_events(reader, writer)
            else:
                await self._write_response(writer, 404, {"error": "not found"})
        except (ConnectionError, BrokenPipeError):
            pass
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    async def _read_request(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> tuple[str, str, bytes] | None:
        request_line = await reader.readline()
        if not request_line:
            return None
        parts = request_line.decode("latin-1").strip().split()
        if len(parts) < 2:
            return None
        method, path = parts[0], parts[1]

        headers: dict[str, str] = {}
        total = len(request_line)
        while True:
            try:
                line = await reader.readline()
            except asyncio.LimitOverrunError as exc:
                await self._write_response(writer, 400, {"error": str(exc)})
                return None
            total += len(line)
            if total > _MAX_HEADER_BYTES:
                await self._write_response(
                    writer, 400, {"error": "request headers too large"}
                )
                return None
            if not line or line in (b"\r\n", b"\n"):
                break
            text = line.decode("latin-1").strip()
            if ":" in text:
                key, _, value = text.partition(":")
                headers[key.strip().lower()] = value.strip()

        body = b""
        if method == "POST":
            try:
                length = int(headers.get("content-length", "0") or "0")
            except ValueError as exc:
                await self._write_response(writer, 400, {"error": str(exc)})
                return None
            if length > 0:
                try:
                    body = await reader.readexactly(length)
                except asyncio.IncompleteReadError as exc:
                    await self._write_response(writer, 400, {"error": str(exc)})
                    return None
        return method, path, body

    async def _handle_command(
        self, writer: asyncio.StreamWriter, body: bytes
    ) -> None:
        try:
            text = body.decode("utf-8")
            command = decode_command(text)
        except (ProtocolError, ValueError, TypeError, UnicodeDecodeError) as exc:
            await self._write_response(writer, 400, {"error": str(exc)})
            return

        queue = self._session.subscribe()
        events = []
        try:
            await self._session.handle(command)
        except Exception as exc:  # noqa: BLE001
            self._session.emit_error(f"{type(command).__name__} failed: {exc}")
        finally:
            while True:
                try:
                    events.append(queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            self._session.unsubscribe(queue)

        await self._write_response(writer, 200, [event.to_json() for event in events])

    async def _handle_events(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/event-stream\r\n"
            b"Cache-Control: no-cache\r\n"
            b"Connection: keep-alive\r\n"
            b"\r\n"
        )
        await writer.drain()
        queue = self._session.subscribe()
        wait_disconnect: asyncio.Task = asyncio.create_task(reader.read(1))
        stream_events: asyncio.Task = asyncio.create_task(
            self._stream_events(writer, queue)
        )
        try:
            done, pending = await asyncio.wait(
                {wait_disconnect, stream_events},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                if task.cancelled():
                    continue
                exc = task.exception()
                if exc is not None and not isinstance(
                    exc, (ConnectionError, BrokenPipeError)
                ):
                    raise exc
        finally:
            self._session.unsubscribe(queue)

    async def _stream_events(
        self,
        writer: asyncio.StreamWriter,
        queue,
    ) -> None:
        while True:
            event = await queue.get()
            chunk = f"data: {json.dumps(event.to_json())}\n\n".encode()
            writer.write(chunk)
            await writer.drain()

    async def _write_response(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        payload,
    ) -> None:
        reasons = {200: "OK", 400: "Bad Request", 404: "Not Found"}
        body = json.dumps(payload).encode("utf-8")
        header = (
            f"HTTP/1.1 {status} {reasons.get(status, '')}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode("latin-1")
        writer.write(header + body)
        await writer.drain()
