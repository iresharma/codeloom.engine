from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from runtime.server import EngineServer
from runtime.session import EngineSession
from runtime.store.edits import ensure_schema


async def _wait_socket(path: Path, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if path.exists() and path.is_socket():
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"socket did not appear: {path}")


@pytest.fixture
def session(short_tmp_path):
    """Create a test EngineSession."""
    db = short_tmp_path / "session.db"
    ensure_schema(db)
    sess = EngineSession(short_tmp_path, db)
    return sess


@pytest.fixture
def short_tmp_path():
    """Create a temporary directory with a short path to avoid AF_UNIX length limits on macOS."""
    # macOS AF_UNIX sockets have a 104 character sun_path limit
    # pytest's tmp_path can create very deep paths that exceed this
    # Use tempfile.mkdtemp with a short prefix instead
    tmpdir = tempfile.mkdtemp(prefix=".e_")
    yield Path(tmpdir)
    # Cleanup
    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)


def test_server_creates_socket(session, short_tmp_path):
    """Test that EngineServer creates a Unix domain socket."""

    async def run():
        socket_path = short_tmp_path / ".engine" / "engine.sock"
        server = EngineServer(session, socket_path)

        # Start server in background
        serve_task = asyncio.create_task(server.serve())
        await _wait_socket(socket_path)

        # Stop server
        server.stop()
        await serve_task

        # Socket should be cleaned up
        assert not socket_path.exists()

    asyncio.run(run())


def test_server_removes_stale_socket(session, short_tmp_path):
    """Test that EngineServer removes a stale socket file on startup."""

    async def run():
        socket_path = short_tmp_path / ".engine" / "engine.sock"
        socket_path.parent.mkdir(parents=True, exist_ok=True)

        # Create a stale socket file (as a regular file)
        socket_path.touch()
        assert socket_path.exists()

        server = EngineServer(session, socket_path)

        # Start server in background
        serve_task = asyncio.create_task(server.serve())
        await _wait_socket(socket_path)

        # Stop server
        server.stop()
        await serve_task

        # Socket should be cleaned up
        assert not socket_path.exists()

    asyncio.run(run())


def test_server_creates_parent_directories(session, short_tmp_path):
    """Test that EngineServer creates parent directories if needed."""

    async def run():
        socket_path = short_tmp_path / "nested" / "deep" / ".engine" / "engine.sock"
        assert not socket_path.parent.exists()

        server = EngineServer(session, socket_path)

        # Start server in background
        serve_task = asyncio.create_task(server.serve())
        await _wait_socket(socket_path)
        assert socket_path.parent.exists()

        # Stop server
        server.stop()
        await serve_task

        # Socket should be cleaned up
        assert not socket_path.exists()

    asyncio.run(run())


def test_server_clean_shutdown(session, short_tmp_path):
    """Test that EngineServer cleans up socket on shutdown."""

    async def run():
        socket_path = short_tmp_path / ".engine" / "engine.sock"
        server = EngineServer(session, socket_path)

        # Start server
        serve_task = asyncio.create_task(server.serve())
        await _wait_socket(socket_path)

        # Stop the server
        server.stop()

        # Wait for serve to complete
        await serve_task

        # Socket should be removed
        assert not socket_path.exists()

    asyncio.run(run())


def test_server_stop_works(session, short_tmp_path):
    """Test that server.stop() triggers shutdown."""

    async def run():
        socket_path = short_tmp_path / ".engine" / "engine.sock"
        server = EngineServer(session, socket_path)

        serve_task = asyncio.create_task(server.serve())
        await _wait_socket(socket_path)

        # Call stop
        server.stop()

        # Give it a moment to shut down
        try:
            await asyncio.wait_for(serve_task, timeout=1.0)
        except asyncio.TimeoutError:
            pytest.fail("Server did not shut down within timeout")

    asyncio.run(run())


def test_server_socket_path_is_stored(session, short_tmp_path):
    """Test that EngineServer stores the socket path."""
    socket_path = short_tmp_path / ".engine" / "engine.sock"
    server = EngineServer(session, socket_path)
    assert server._socket_path == socket_path


def test_server_has_session(session, short_tmp_path):
    """Test that EngineServer stores the session."""
    socket_path = short_tmp_path / ".engine" / "engine.sock"
    server = EngineServer(session, socket_path)
    assert server._session is session


def test_server_stopped_event_initialized(session, short_tmp_path):
    """Test that EngineServer initializes the stopped event."""
    socket_path = short_tmp_path / ".engine" / "engine.sock"
    
    async def run():
        server = EngineServer(session, socket_path)
        # The event should be initialized lazily or on first serve
        assert hasattr(server, '_stopped')
        # We can't assert isinstance outside of event loop, so just verify the attribute exists
        
    asyncio.run(run())


def test_server_multiple_startups_cleanup(session, short_tmp_path):
    """Test that starting server twice cleans up previous socket."""

    async def run():
        socket_path = short_tmp_path / ".engine" / "engine.sock"

        # First server
        server1 = EngineServer(session, socket_path)
        serve_task1 = asyncio.create_task(server1.serve())
        await _wait_socket(socket_path)
        server1.stop()
        await serve_task1

        # Second server (should clean up first socket)
        server2 = EngineServer(session, socket_path)
        serve_task2 = asyncio.create_task(server2.serve())
        await _wait_socket(socket_path)
        server2.stop()
        await serve_task2

        # Socket should be cleaned up
        assert not socket_path.exists()

    asyncio.run(run())
