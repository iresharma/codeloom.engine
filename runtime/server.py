from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path

from protocol.codec import STREAM_LIMIT, ProtocolError, decode_command, encode
from protocol.events import ErrorOccurred
from runtime.session import EngineSession


class EngineServer:
    def __init__(self, session: EngineSession, socket_path: Path):
        self._session = session
        self._socket_path = socket_path
        self._stopped = asyncio.Event()

    async def serve(self) -> None:
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self._socket_path.exists():
            self._socket_path.unlink()

        server = await asyncio.start_unix_server(
            self._on_client, path=str(self._socket_path), limit=STREAM_LIMIT
        )
        try:
            async with server:
                await self._stopped.wait()
        finally:
            if self._socket_path.exists():
                self._socket_path.unlink()

    def stop(self) -> None:
        self._stopped.set()

    async def _on_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        queue = self._session.subscribe()
        read_task = asyncio.create_task(self._read_commands(reader))
        write_task = asyncio.create_task(self._write_events(writer, queue))
        try:
            done, pending = await asyncio.wait(
                {read_task, write_task},
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
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    async def _read_commands(self, reader: asyncio.StreamReader) -> None:
        while True:
            line = await reader.readline()
            if not line:
                break
            try:
                command = decode_command(line)
            except (ProtocolError, TypeError, ValueError) as exc:
                self._session.emit_error(str(exc))
                continue
            try:
                await self._session.handle(command)
            except Exception as exc:  # noqa: BLE001
                self._session.emit_error(f"{type(command).__name__} failed: {exc}")

    async def _write_events(
        self,
        writer: asyncio.StreamWriter,
        queue: asyncio.Queue,
    ) -> None:
        while True:
            event = await queue.get()
            payload = encode(event)
            if len(payload) >= STREAM_LIMIT:
                payload = encode(
                    ErrorOccurred(
                        message=(
                            f"dropped {type(event).__name__}: {len(payload)} bytes "
                            f"exceeds the {STREAM_LIMIT} byte NDJSON line limit"
                        )
                    )
                )
            writer.write(payload)
            await writer.drain()
