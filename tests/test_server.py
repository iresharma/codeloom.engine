from __future__ import annotations

import socket
import sys
import urllib.request

import pytest

from runtime.tools.server import start_server, stop_all, stop_server, server_logs

SERVE = """
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

port = int(os.environ["PORT"])

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *_args):
        pass

HTTPServer(("127.0.0.1", port), Handler).serve_forever()
"""


def _command(tmp_path) -> str:
    script = tmp_path / "serve.py"
    script.write_text(SERVE)
    return f"{sys.executable} {script}"


def _get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=2) as response:
        return response.read().decode()


@pytest.mark.asyncio
async def test_start_server_listens_and_stop_kills(tmp_path):
    command = _command(tmp_path)
    try:
        result = await start_server(tmp_path, command, approval="never")
        assert result.startswith("listening http://127.0.0.1:")
        port = int(result.split("127.0.0.1:")[1].split()[0])
        assert _get(f"http://127.0.0.1:{port}/") == "ok"
        logs = await server_logs(tmp_path)
        assert f"127.0.0.1:{port}" in logs
        assert "stopped" == await stop_server(tmp_path)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            assert sock.connect_ex(("127.0.0.1", port)) != 0
        assert await server_logs(tmp_path) == "no server running"
    finally:
        await stop_all()


@pytest.mark.asyncio
async def test_start_server_replaces_previous(tmp_path):
    command = _command(tmp_path)
    try:
        first = await start_server(tmp_path, command, approval="never")
        first_port = int(first.split("127.0.0.1:")[1].split()[0])
        second = await start_server(tmp_path, command, approval="never")
        second_port = int(second.split("127.0.0.1:")[1].split()[0])
        assert _get(f"http://127.0.0.1:{second_port}/") == "ok"
        if first_port != second_port:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.3)
                assert sock.connect_ex(("127.0.0.1", first_port)) != 0
    finally:
        await stop_all()
