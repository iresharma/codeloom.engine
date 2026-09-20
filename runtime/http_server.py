from __future__ import annotations

import asyncio
import json
from contextlib import suppress

from protocol.codec import STREAM_LIMIT, ProtocolError, decode_command
from runtime.session import EngineSession

_MAX_HEADER_LINE = 64 * 1024
# Cap on the Content-Length a client may declare, consistent with the
# unix-socket transport's NDJSON line limit (protocol.codec.STREAM_LIMIT).
# Also used as the asyncio StreamReader buffer limit so readline() raises
# asyncio.LimitOverrunError (caught below) instead of blocking/allocating
# unbounded memory on an oversized request line or header.
_MAX_BODY_LENGTH = STREAM_LIMIT


class _BadRequest(Exception):
    """Raised by _read_request for malformed input where a 400 response can
    still be sent (as opposed to a plain disconnect, signalled by None)."""


class HttpServer:
    """Minimal stdlib-only HTTP transport exposing the same session/protocol
    machinery as the unix-socket EngineServer, mirroring its serve()/stop()
    lifecycle so app.py can run both transports side by side."""

    def __init__(
        self,
        session: EngineSession,
        host: str = "127.0.0.1",
        port: int = 8765,
    ):
        self._session = session
        self._host = host
        self._port = port
        self._stopped: asyncio.Event | None = None

    def _stop_event(self) -> asyncio.Event:
        if self._stopped is None:
            self._stopped = asyncio.Event()
        return self._stopped

    async def serve(self) -> None:
        server = await asyncio.start_server(
            self._on_client,
            host=self._host,
            port=self._port,
            limit=_MAX_BODY_LENGTH,
        )
        try:
            async with server:
                await self._stop_event().wait()
        finally:
            pass

    def stop(self) -> None:
        self._stop_event().set()

    async def _on_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            try:
                request = await self._read_request(reader)
            except _BadRequest as exc:
                payload = json.dumps({"error": str(exc)}).encode()
                await self._write_response(writer, 400, "Bad Request", payload)
                return
            except (asyncio.LimitOverrunError, asyncio.IncompleteReadError):
                # Oversized request line/header, or a client that
                # disconnected mid-body. No well-formed request to act on;
                # close the connection cleanly rather than raising.
                return
            if request is None:
                return
            method, path, body = request
            if method == "POST" and path == "/command":
                await self._handle_command(writer, body)
            elif method == "GET" and path == "/events":
                await self._handle_events(writer)
            else:
                await self._write_response(
                    writer, 404, "Not Found", b'{"error": "not found"}\n'
                )
        except (ConnectionError, BrokenPipeError):
            pass
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    async def _read_request(
        self, reader: asyncio.StreamReader
    ) -> tuple[str, str, bytes] | None:
        request_line = await reader.readline()
        if not request_line:
            return None
        try:
            parts = request_line.decode("latin-1").strip().split(" ")
            method, path = parts[0], parts[1]
        except (IndexError, UnicodeDecodeError):
            return None

        headers: dict[str, str] = {}
        while True:
            line = await reader.readline()
            if not line or len(line) > _MAX_HEADER_LINE:
                break
            text = line.decode("latin-1").strip()
            if not text:
                break
            if ":" in text:
                key, _, value = text.partition(":")
                headers[key.strip().lower()] = value.strip()

        body = b""
        raw_length = headers.get("content-length", "0") or "0"
        try:
            length = int(raw_length)
        except ValueError:
            raise _BadRequest(f"invalid Content-Length: {raw_length!r}") from None
        if length < 0:
            raise _BadRequest(f"invalid Content-Length: {raw_length!r}")
        if length > _MAX_BODY_LENGTH:
            raise _BadRequest(
                f"Content-Length {length} exceeds the {_MAX_BODY_LENGTH} byte limit"
            )
        if length > 0:
            body = await reader.readexactly(length)
        return method, path, body

    async def _handle_command(
        self, writer: asyncio.StreamWriter, body: bytes
    ) -> None:
        try:
            command = decode_command(body)
        except (ProtocolError, TypeError, ValueError) as exc:
            payload = json.dumps({"error": str(exc)}).encode()
            await self._write_response(writer, 400, "Bad Request", payload)
            return

        subscriber = self._session.subscribe()
        try:
            try:
                await self._session.handle(command)
            except Exception as exc:  # noqa: BLE001
                self._session.emit_error(f"{type(command).__name__} failed: {exc}")
            events = []
            while True:
                try:
                    event = subscriber.get_nowait()
                except asyncio.QueueEmpty:
                    break
                events.append(event.to_json())
        finally:
            self._session.unsubscribe(subscriber)

        payload = json.dumps({"events": events}).encode()
        await self._write_response(writer, 200, "OK", payload)

    async def _handle_events(self, writer: asyncio.StreamWriter) -> None:
        subscriber = self._session.subscribe()
        try:
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/event-stream\r\n"
                b"Cache-Control: no-cache\r\n"
                b"Connection: keep-alive\r\n"
                b"\r\n"
            )
            await writer.drain()
            stop_event = self._stop_event()
            while not stop_event.is_set():
                get_task: asyncio.Task = asyncio.create_task(subscriber.get())
                stop_task: asyncio.Task = asyncio.create_task(stop_event.wait())
                pending: set[asyncio.Task]
                done, pending = await asyncio.wait(
                    {get_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                for task in pending:
                    with suppress(asyncio.CancelledError):
                        await task
                if stop_task in done:
                    break
                event = get_task.result()
                data = json.dumps(event.to_json())
                writer.write(f"data: {data}\n\n".encode())
                await writer.drain()
        finally:
            self._session.unsubscribe(subscriber)

    async def _write_response(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        reason: str,
        body: bytes,
    ) -> None:
        writer.write(
            f"HTTP/1.1 {status} {reason}\r\n".encode()
            + b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n".encode()
            + b"Connection: close\r\n"
            + b"\r\n"
            + body
        )
        await writer.drain()
