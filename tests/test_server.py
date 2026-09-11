from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from runtime.server import EngineServer
from runtime.session import EngineSession
from runtime.store.edits import ensure_schema


@pytest.fixture
def session(tmp_path):
    """Create a test EngineSession."""
    db = tmp_path / "session.db"
    ensure_schema(db)
    sess = EngineSession(tmp_path, db)
    return sess


def test_server_creates_socket(session, tmp_path):
    """Test that EngineServer creates a Unix domain socket."""

    async def run():
        socket_path = tmp_path / ".engine" / "engine.sock"
        server = EngineServer(session, socket_path)

        # Start server in background
        serve_task = asyncio.create_task(server.serve())
        await asyncio.sleep(0.05)  # Give it time to bind

        # Check socket file exists
        assert socket_path.exists()
        assert socket_path.is_socket()

        # Stop server
        server.stop()
        await serve_task

        # Socket should be cleaned up
        assert not socket_path.exists()

    asyncio.run(run())


def test_server_removes_stale_socket(session, tmp_path):
    """Test that EngineServer removes a stale socket file on startup."""

    async def run():
        socket_path = tmp_path / ".engine" / "engine.sock"
        socket_path.parent.mkdir(parents=True, exist_ok=True)

        # Create a stale socket file (as a regular file)
        socket_path.touch()
        assert socket_path.exists()

        server = EngineServer(session, socket_path)

        # Start server in background
        serve_task = asyncio.create_task(server.serve())
        await asyncio.sleep(0.05)  # Give it time to bind

        # Socket should exist and be a proper socket
        assert socket_path.exists()
        assert socket_path.is_socket()

        # Stop server
        server.stop()
        await serve_task

        # Socket should be cleaned up
        assert not socket_path.exists()

    asyncio.run(run())


def test_server_creates_parent_directories(session, tmp_path):
    """Test that EngineServer creates parent directories if needed."""

    async def run():
        socket_path = tmp_path / "nested" / "deep" / ".engine" / "engine.sock"
        assert not socket_path.parent.exists()

        server = EngineServer(session, socket_path)

        # Start server in background
        serve_task = asyncio.create_task(server.serve())
        await asyncio.sleep(0.05)  # Give it time to bind

        # Parent directories should be created
        assert socket_path.parent.exists()
        assert socket_path.exists()

        # Stop server
        server.stop()
        await serve_task

        # Socket should be cleaned up
        assert not socket_path.exists()

    asyncio.run(run())


def test_server_clean_shutdown(session, tmp_path):
    """Test that EngineServer cleans up socket on shutdown."""

    async def run():
        socket_path = tmp_path / ".engine" / "engine.sock"
        server = EngineServer(session, socket_path)

        # Start server
        serve_task = asyncio.create_task(server.serve())
        await asyncio.sleep(0.05)

        # Socket should exist
        assert socket_path.exists()

        # Stop the server
        server.stop()

        # Wait for serve to complete
        await serve_task

        # Socket should be removed
        assert not socket_path.exists()

    asyncio.run(run())


def test_server_stop_works(session, tmp_path):
    """Test that server.stop() triggers shutdown."""

    async def run():
        socket_path = tmp_path / ".engine" / "engine.sock"
        server = EngineServer(session, socket_path)

        serve_task = asyncio.create_task(server.serve())
        await asyncio.sleep(0.05)

        # Verify server is running
        assert socket_path.exists()

        # Call stop
        server.stop()

        # Give it a moment to shut down
        try:
            await asyncio.wait_for(serve_task, timeout=1.0)
        except asyncio.TimeoutError:
            pytest.fail("Server did not shut down within timeout")

    asyncio.run(run())


def test_server_socket_path_is_stored(session, tmp_path):
    """Test that EngineServer stores the socket path."""
    socket_path = tmp_path / ".engine" / "engine.sock"
    server = EngineServer(session, socket_path)
    assert server._socket_path == socket_path


def test_server_has_session(session, tmp_path):
    """Test that EngineServer stores the session."""
    socket_path = tmp_path / ".engine" / "engine.sock"
    server = EngineServer(session, socket_path)
    assert server._session is session


def test_server_stopped_event_initialized(session, tmp_path):
    """Test that EngineServer initializes the stopped event."""
    socket_path = tmp_path / ".engine" / "engine.sock"
    server = EngineServer(session, socket_path)
    assert isinstance(server._stopped, asyncio.Event)
    assert not server._stopped.is_set()


def test_server_multiple_startups_cleanup(session, tmp_path):
    """Test that starting server twice cleans up previous socket."""

    async def run():
        socket_path = tmp_path / ".engine" / "engine.sock"

        # First server
        server1 = EngineServer(session, socket_path)
        serve_task1 = asyncio.create_task(server1.serve())
        await asyncio.sleep(0.05)
        assert socket_path.exists()
        server1.stop()
        await serve_task1

        # Second server (should clean up first socket)
        server2 = EngineServer(session, socket_path)
        serve_task2 = asyncio.create_task(server2.serve())
        await asyncio.sleep(0.05)
        assert socket_path.exists()
        server2.stop()
        await serve_task2

        # Socket should be cleaned up
        assert not socket_path.exists()

    asyncio.run(run())
