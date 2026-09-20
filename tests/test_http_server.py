from __future__ import annotations

import asyncio
import json
from contextlib import suppress

import pytest

from runtime.http_server import HttpServer
from runtime.session import EngineSession
from runtime.store.edits import ensure_schema


@pytest.fixture
async def session(tmp_path):
    db = tmp_path / "session.db"
    ensure_schema(db)
    sess = EngineSession(tmp_path, db)
    await sess.start()
    return sess


async def _wait_connect(host: str, port: int, timeout: float = 2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            reader, writer = await asyncio.open_connection(host, port)
            return reader, writer
        except OSError:
            await asyncio.sleep(0.01)
    pytest.fail(f"could not connect to {host}:{port}")


async def _read_http_response(reader: asyncio.StreamReader) -> tuple[int, dict, bytes]:
    status_line = await reader.readline()
    status = int(status_line.decode().split(" ", 2)[1])
    headers: dict[str, str] = {}
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        name, _, value = line.decode().partition(":")
        headers[name.strip().lower()] = value.strip()
    body = b""
    length = int(headers.get("content-length", "0"))
    if length:
        body = await reader.readexactly(length)
    return status, headers, body


async def _post_command(host: str, port: int, payload: dict) -> tuple[int, bytes]:
    reader, writer = await _wait_connect(host, port)
    body = json.dumps(payload).encode()
    request = (
        f"POST /command HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode() + body
    writer.write(request)
    await writer.drain()
    status, _headers, resp_body = await _read_http_response(reader)
    writer.close()
    return status, resp_body


async def test_http_server_init(session):
    server = HttpServer(session, "127.0.0.1", 0)
    assert server._session == session
    assert server._host == "127.0.0.1"
    assert server._port == 0


async def test_http_server_stop(session):
    server = HttpServer(session, "127.0.0.1", 0)
    server.stop()
    assert server._stopped.is_set()


async def test_post_command_happy_path(session):
    server = HttpServer(session, "127.0.0.1", 0)
    serve_task = asyncio.create_task(server.serve())
    try:
        # Give the server a moment to bind before connecting; retried via _wait_connect.
        host, port = "127.0.0.1", await _bound_port(server)
        status, body = await _post_command(host, port, {"type": "ListSessions"})
        assert status == 200
        events = json.loads(body)
        assert isinstance(events, list)
        assert len(events) == 1
        assert events[0]["type"] == "SessionList"
        assert events[0]["sessions"] == []
    finally:
        server.stop()
        await serve_task


async def test_post_command_malformed_json(session):
    server = HttpServer(session, "127.0.0.1", 0)
    serve_task = asyncio.create_task(server.serve())
    try:
        host, port = "127.0.0.1", await _bound_port(server)
        reader, writer = await _wait_connect(host, port)
        body = b"not json"
        request = (
            f"POST /command HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode() + body
        writer.write(request)
        await writer.drain()
        status, _headers, resp_body = await _read_http_response(reader)
        writer.close()
        assert status == 400
        payload = json.loads(resp_body)
        assert "error" in payload
    finally:
        server.stop()
        await serve_task


async def test_post_command_unknown_type(session):
    server = HttpServer(session, "127.0.0.1", 0)
    serve_task = asyncio.create_task(server.serve())
    try:
        host, port = "127.0.0.1", await _bound_port(server)
        status, body = await _post_command(host, port, {"type": "TotallyNotACommand"})
        assert status == 400
        payload = json.loads(body)
        assert "error" in payload
    finally:
        server.stop()
        await serve_task


def test_get_events_streams_event(short_tmp_path):
    async def run():
        db = short_tmp_path / "session.db"
        ensure_schema(db)
        sess = EngineSession(short_tmp_path, db)
        await sess.start()

        server = HttpServer(sess, "127.0.0.1", 0)
        serve_task = asyncio.create_task(server.serve())
        try:
            host, port = "127.0.0.1", await _bound_port(server)

            reader, writer = await _wait_connect(host, port)
            writer.write(f"GET /events HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
            await writer.drain()

            status_line = await asyncio.wait_for(reader.readline(), timeout=2.0)
            assert b"200" in status_line
            # consume headers
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=2.0)
                if line in (b"\r\n", b"\n", b""):
                    break

            async def trigger_command():
                await asyncio.sleep(0.05)
                await _post_command(host, port, {"type": "ListSessions"})

            trigger = asyncio.create_task(trigger_command())
            try:
                data_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                assert data_line.startswith(b"data: ")
                payload = json.loads(data_line[len(b"data: "):].decode().strip())
                assert "type" in payload
            finally:
                await trigger
                writer.close()
                # The SSE handler's per-connection task only notices a
                # disconnect once a write to it fails, so while the server is
                # still accepting connections, provoke one more write by
                # posting another command; this lets the handler's write fail
                # and its task exit before we call stop()/await serve_task.
                with suppress(Exception):
                    await _post_command(host, port, {"type": "ListSessions"})
                await asyncio.sleep(0.05)
        finally:
            server.stop()
            try:
                await asyncio.wait_for(serve_task, timeout=2.0)
            except asyncio.TimeoutError:
                serve_task.cancel()
                with suppress(asyncio.CancelledError):
                    await serve_task

    asyncio.run(run())


@pytest.fixture
def short_tmp_path():
    import shutil
    import tempfile
    from pathlib import Path

    tmpdir = tempfile.mkdtemp(prefix=".eh_")
    yield Path(tmpdir)
    shutil.rmtree(tmpdir, ignore_errors=True)


async def _bound_port(server: HttpServer) -> int:
    """Wait for the server's underlying asyncio.Server to bind and return its port."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 2.0
    while loop.time() < deadline:
        port = server.bound_port
        if port is not None:
            return port
        await asyncio.sleep(0.01)
    pytest.fail("server did not bind in time")
