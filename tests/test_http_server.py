from __future__ import annotations

import asyncio
import http.client
import json
import socket

import pytest

from runtime.http_server import _MAX_BODY_LENGTH, HttpServer
from runtime.session import EngineSession
from runtime.store.edits import ensure_schema


@pytest.fixture
def session(tmp_path):
    db = tmp_path / "session.db"
    ensure_schema(db)
    return EngineSession(tmp_path, db)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _wait_listening(host: str, port: int, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.1):
                return
        except OSError:
            await asyncio.sleep(0.01)
    pytest.fail(f"http server did not start listening on {host}:{port}")


def _do_request(host, port, method, path, body=None):
    """Blocking http.client call; run in a thread executor so it does not
    starve the same-thread asyncio event loop running the server."""
    conn = http.client.HTTPConnection(host, port, timeout=2)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    status = resp.status
    payload = resp.read()
    conn.close()
    return status, payload


def test_post_command_round_trip(session):
    async def run():
        host, port = "127.0.0.1", _free_port()
        server = HttpServer(session, host=host, port=port)
        serve_task = asyncio.create_task(server.serve())
        await _wait_listening(host, port)
        try:
            body = json.dumps({"type": "ListSessions"})
            status, raw = await asyncio.get_running_loop().run_in_executor(
                None, _do_request, host, port, "POST", "/command", body
            )
            payload = json.loads(raw)
            assert status == 200
            assert "events" in payload
            assert isinstance(payload["events"], list)
            assert any(e.get("type") == "SessionList" for e in payload["events"])
        finally:
            server.stop()
            await serve_task

    asyncio.run(run())


def test_post_command_malformed_json_returns_400(session):
    async def run():
        host, port = "127.0.0.1", _free_port()
        server = HttpServer(session, host=host, port=port)
        serve_task = asyncio.create_task(server.serve())
        await _wait_listening(host, port)
        try:
            status, raw = await asyncio.get_running_loop().run_in_executor(
                None, _do_request, host, port, "POST", "/command", "not json"
            )
            payload = json.loads(raw)
            assert status == 400
            assert "error" in payload
        finally:
            server.stop()
            await serve_task

    asyncio.run(run())


def test_unknown_route_returns_404(session):
    async def run():
        host, port = "127.0.0.1", _free_port()
        server = HttpServer(session, host=host, port=port)
        serve_task = asyncio.create_task(server.serve())
        await _wait_listening(host, port)
        try:
            status, _raw = await asyncio.get_running_loop().run_in_executor(
                None, _do_request, host, port, "GET", "/nope", None
            )
            assert status == 404
        finally:
            server.stop()
            await serve_task

    asyncio.run(run())


def test_non_numeric_content_length_returns_400(session):
    async def run():
        host, port = "127.0.0.1", _free_port()
        server = HttpServer(session, host=host, port=port)
        serve_task = asyncio.create_task(server.serve())
        await _wait_listening(host, port)
        try:
            reader, writer = await asyncio.open_connection(host, port)
            writer.write(
                b"POST /command HTTP/1.1\r\n"
                b"Host: test\r\n"
                b"Content-Length: abc\r\n"
                b"\r\n"
            )
            await writer.drain()
            status_line = await asyncio.wait_for(reader.readline(), timeout=2.0)
            assert b"400" in status_line
            writer.close()
        finally:
            server.stop()
            await serve_task

    asyncio.run(run())


def test_oversized_content_length_returns_400_not_hang(session):
    async def run():
        host, port = "127.0.0.1", _free_port()
        server = HttpServer(session, host=host, port=port)
        serve_task = asyncio.create_task(server.serve())
        await _wait_listening(host, port)
        try:
            reader, writer = await asyncio.open_connection(host, port)
            oversized = _MAX_BODY_LENGTH + 1
            writer.write(
                b"POST /command HTTP/1.1\r\n"
                b"Host: test\r\n"
                + f"Content-Length: {oversized}\r\n".encode()
                + b"\r\n"
                # Deliberately do not send a body: if the server ever tried
                # readexactly(oversized) this would hang instead of failing
                # fast on the declared-length check.
            )
            await writer.drain()
            status_line = await asyncio.wait_for(reader.readline(), timeout=2.0)
            assert b"400" in status_line
            writer.close()
        finally:
            server.stop()
            await serve_task

    asyncio.run(run())


def test_truncated_body_closes_cleanly(session):
    async def run():
        host, port = "127.0.0.1", _free_port()
        server = HttpServer(session, host=host, port=port)
        serve_task = asyncio.create_task(server.serve())
        await _wait_listening(host, port)
        try:
            reader, writer = await asyncio.open_connection(host, port)
            writer.write(
                b"POST /command HTTP/1.1\r\n"
                b"Host: test\r\n"
                b"Content-Length: 100\r\n"
                b"\r\n"
                b"short"
            )
            await writer.drain()
            writer.write_eof()
            # Client declared more body than it sent then disconnected; the
            # server should close without a response and without raising.
            remainder = await asyncio.wait_for(reader.read(), timeout=2.0)
            assert remainder == b""
            writer.close()
        finally:
            server.stop()
            await serve_task

    asyncio.run(run())


def test_get_events_receives_sse_event(session):
    async def run():
        host, port = "127.0.0.1", _free_port()
        server = HttpServer(session, host=host, port=port)
        serve_task = asyncio.create_task(server.serve())
        await _wait_listening(host, port)
        try:
            reader, writer = await asyncio.open_connection(host, port)
            writer.write(b"GET /events HTTP/1.1\r\nHost: test\r\n\r\n")
            await writer.drain()

            # Read status line + headers until blank line.
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=2.0)
                if line in (b"\r\n", b"\n", b""):
                    break

            async def issue_command():
                await asyncio.sleep(0.05)
                cmd_reader, cmd_writer = await asyncio.open_connection(host, port)
                body = json.dumps({"type": "ListSessions"}).encode()
                request = (
                    b"POST /command HTTP/1.1\r\n"
                    b"Host: test\r\n"
                    b"Content-Type: application/json\r\n"
                    + f"Content-Length: {len(body)}\r\n".encode()
                    + b"\r\n"
                    + body
                )
                cmd_writer.write(request)
                await cmd_writer.drain()
                await cmd_reader.read()
                cmd_writer.close()

            asyncio.create_task(issue_command())

            data_line = b""
            deadline = asyncio.get_running_loop().time() + 3.0
            while asyncio.get_running_loop().time() < deadline:
                line = await asyncio.wait_for(reader.readline(), timeout=3.0)
                if line.startswith(b"data:"):
                    data_line = line
                    break

            assert data_line.startswith(b"data:")
            event = json.loads(data_line[len(b"data:") :].decode().strip())
            assert event.get("type") == "SessionList"

            writer.close()
        finally:
            server.stop()
            await serve_task

    asyncio.run(run())
