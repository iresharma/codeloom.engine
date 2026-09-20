from __future__ import annotations

import asyncio
import json

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


async def _start_http_server(session) -> tuple[HttpServer, asyncio.Task, int]:
    server = HttpServer(session, host="127.0.0.1", port=0)
    task = asyncio.create_task(server.serve())
    deadline = asyncio.get_running_loop().time() + 2.0
    while server._server is None:
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError("http server did not start")
        await asyncio.sleep(0.01)
    port = server._server.sockets[0].getsockname()[1]
    return server, task, port


async def _stop_http_server(server: HttpServer, task: asyncio.Task) -> None:
    server.stop()
    await asyncio.wait_for(task, timeout=2.0)


async def _http_request(port: int, method: str, path: str, body: bytes = b"") -> tuple[int, dict, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        request = f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        if body:
            request += f"Content-Length: {len(body)}\r\n"
        request += "\r\n"
        writer.write(request.encode("latin-1") + body)
        await writer.drain()

        status_line = await reader.readline()
        status = int(status_line.decode("latin-1").split()[1])

        headers: dict[str, str] = {}
        while True:
            line = await reader.readline()
            if not line or line in (b"\r\n", b"\n"):
                break
            text = line.decode("latin-1").strip()
            if ":" in text:
                key, _, value = text.partition(":")
                headers[key.strip().lower()] = value.strip()

        length = int(headers.get("content-length", "0") or "0")
        response_body = await reader.readexactly(length) if length else b""
        return status, headers, response_body
    finally:
        writer.close()


async def test_post_command_happy_path(session):
    server, task, port = await _start_http_server(session)
    try:
        body = json.dumps({"type": "ListSessions"}).encode("utf-8")
        status, headers, resp_body = await _http_request(port, "POST", "/command", body)
        assert status == 200
        events = json.loads(resp_body.decode("utf-8"))
        assert isinstance(events, list)
        assert len(events) >= 1
        assert events[0]["type"] == "SessionList"
    finally:
        await _stop_http_server(server, task)


async def test_post_command_malformed_and_unknown_type(session):
    server, task, port = await _start_http_server(session)
    try:
        status, _, resp_body = await _http_request(
            port, "POST", "/command", b"not json"
        )
        assert status == 400
        payload = json.loads(resp_body.decode("utf-8"))
        assert "error" in payload

        status, _, resp_body = await _http_request(
            port,
            "POST",
            "/command",
            json.dumps({"type": "NotARealCommand"}).encode("utf-8"),
        )
        assert status == 400
        payload = json.loads(resp_body.decode("utf-8"))
        assert "error" in payload
    finally:
        await _stop_http_server(server, task)


async def test_post_command_malformed_content_length(session):
    server, task, port = await _start_http_server(session)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(
                b"POST /command HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Content-Length: abc\r\n"
                b"\r\n"
            )
            await writer.drain()

            status_line = await asyncio.wait_for(reader.readline(), timeout=2.0)
            assert b"400" in status_line

            headers: dict[str, str] = {}
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=2.0)
                if not line or line in (b"\r\n", b"\n"):
                    break
                text = line.decode("latin-1").strip()
                if ":" in text:
                    key, _, value = text.partition(":")
                    headers[key.strip().lower()] = value.strip()

            length = int(headers.get("content-length", "0") or "0")
            resp_body = await asyncio.wait_for(reader.readexactly(length), timeout=2.0)
            payload = json.loads(resp_body.decode("utf-8"))
            assert "error" in payload
        finally:
            writer.close()

        # server should still be usable for subsequent connections
        body = json.dumps({"type": "ListSessions"}).encode("utf-8")
        status, _, resp_body = await _http_request(port, "POST", "/command", body)
        assert status == 200
    finally:
        await _stop_http_server(server, task)


async def test_post_command_truncated_body_does_not_crash_server(session):
    server, task, port = await _start_http_server(session)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(
                b"POST /command HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Content-Length: 1000\r\n"
                b"\r\n"
                b"short body"
            )
            await writer.drain()
            if writer.can_write_eof():
                writer.write_eof()
        finally:
            writer.close()

        # give the server a moment to process/close the connection
        await asyncio.sleep(0.1)

        # server should still be usable for subsequent connections
        body = json.dumps({"type": "ListSessions"}).encode("utf-8")
        status, _, resp_body = await _http_request(port, "POST", "/command", body)
        assert status == 200
    finally:
        await _stop_http_server(server, task)


async def test_post_command_oversized_header_line_does_not_crash_server(session):
    server, task, port = await _start_http_server(session)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            huge_value = "x" * (70 * 1024)
            writer.write(
                (
                    "POST /command HTTP/1.1\r\n"
                    "Host: 127.0.0.1\r\n"
                    f"X-Huge: {huge_value}\r\n"
                    "\r\n"
                ).encode("latin-1")
            )
            await writer.drain()

            # either a 400 response or a clean connection close is acceptable
            try:
                status_line = await asyncio.wait_for(reader.readline(), timeout=2.0)
                if status_line:
                    assert b"400" in status_line
            except (asyncio.IncompleteReadError, ConnectionError):
                pass
        finally:
            writer.close()

        await asyncio.sleep(0.1)

        # server should still be usable for subsequent connections
        body = json.dumps({"type": "ListSessions"}).encode("utf-8")
        status, _, resp_body = await _http_request(port, "POST", "/command", body)
        assert status == 200
    finally:
        await _stop_http_server(server, task)


async def test_get_events_streams_after_command(session):
    server, task, port = await _start_http_server(session)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(b"GET /events HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            await writer.drain()

            status_line = await asyncio.wait_for(reader.readline(), timeout=2.0)
            assert b"200" in status_line

            headers: dict[str, str] = {}
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=2.0)
                if not line or line in (b"\r\n", b"\n"):
                    break
                text = line.decode("latin-1").strip()
                if ":" in text:
                    key, _, value = text.partition(":")
                    headers[key.strip().lower()] = value.strip()
            assert headers.get("content-type") == "text/event-stream"

            session.emit_error("test event")

            data_line = b""
            while not data_line.startswith(b"data: "):
                data_line = await asyncio.wait_for(reader.readline(), timeout=2.0)

            payload = json.loads(data_line.decode("utf-8")[len("data: "):].strip())
            assert "type" in payload
        finally:
            writer.close()
    finally:
        await _stop_http_server(server, task)
