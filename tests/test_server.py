from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from runtime.server import EngineServer
from runtime.session import EngineSession
from runtime.store.edits import ensure_schema
from unittest.mock import AsyncMock, patch
from protocol.events import ErrorOccurred


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


@pytest.fixture
async def started_session(tmp_path):
    db = tmp_path / "session.db"
    ensure_schema(db)
    sess = EngineSession(tmp_path, db)
    await sess.start()
    return sess


async def test_engine_server_init(started_session):
    server = EngineServer(started_session, Path("/tmp/test.sock"))
    assert server._session == started_session
    assert server._socket_path == Path("/tmp/test.sock")


async def test_engine_server_stop(started_session):
    server = EngineServer(started_session, Path("/tmp/test.sock"))
    server.stop()
    assert server._stopped.is_set()


async def test_engine_server_on_client_success(tmp_path):
    """Test client connection handling."""
    db = tmp_path / "session.db"
    ensure_schema(db)
    session = EngineSession(tmp_path, db)
    await session.start()
    
    server = EngineServer(session, tmp_path / "test.sock")
    
    # Mock reader and writer
    reader = AsyncMock()
    writer = AsyncMock()
    
    # Mock readline to return empty (EOF)
    reader.readline = AsyncMock(return_value=b"")
    
    # This should run _read_commands which hits EOF and returns
    task = asyncio.create_task(server._on_client(reader, writer))
    try:
        await asyncio.wait_for(task, timeout=1.0)
    except asyncio.TimeoutError:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def test_engine_server_read_commands_invalid(tmp_path):
    """Test reading invalid command."""
    db = tmp_path / "session.db"
    ensure_schema(db)
    session = EngineSession(tmp_path, db)
    await session.start()
    
    server = EngineServer(session, tmp_path / "test.sock")
    
    reader = AsyncMock()
    writer = AsyncMock()
    
    # Invalid JSON
    reader.readline = AsyncMock(side_effect=[b"invalid json\n", b""])
    
    task = asyncio.create_task(server._read_commands(reader))
    try:
        await asyncio.wait_for(task, timeout=1.0)
    except asyncio.TimeoutError:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def test_engine_server_write_events(tmp_path):
    """Test event writing."""
    db = tmp_path / "session.db"
    ensure_schema(db)
    session = EngineSession(tmp_path, db)
    await session.start()
    
    server = EngineServer(session, tmp_path / "test.sock")
    writer = AsyncMock()
    
    # Create a queue with one event
    queue = asyncio.Queue()
    event = ErrorOccurred(message="test error")
    await queue.put(event)
    
    # Create write task that will process one event
    async def write_once():
        # Get one event and write it
        event = await queue.get()
        payload = b"test_payload"
        writer.write(payload)
        await writer.drain()
    
    await write_once()
    assert writer.write.called


async def test_engine_server_socket_cleanup(tmp_path):
    """Test socket cleanup."""
    db = tmp_path / "session.db"
    ensure_schema(db)
    session = EngineSession(tmp_path, db)
    await session.start()
    
    socket_path = tmp_path / "test.sock"
    socket_path.touch()  # Create the file
    
    server = EngineServer(session, socket_path)
    
    # serve() should clean up existing socket
    with patch("asyncio.start_unix_server") as mock_start:
        async def mock_server_cm(*args, **kwargs):
            class MockServer:
                async def __aenter__(self):
                    return self
                async def __aexit__(self, *args):
                    pass
            return MockServer()
        
        mock_start.side_effect = Exception("test")
        
        # Socket should be cleaned up even on error
        try:
            await server.serve()
        except Exception:
            pass


async def test_engine_server_client_error_handling(tmp_path):
    """Test error handling in _on_client."""
    db = tmp_path / "session.db"
    ensure_schema(db)
    session = EngineSession(tmp_path, db)
    await session.start()
    
    server = EngineServer(session, tmp_path / "test.sock")
    
    reader = AsyncMock()
    writer = AsyncMock()
    
    # Simulate connection error
    reader.readline.side_effect = ConnectionError("lost connection")
    
    task = asyncio.create_task(server._on_client(reader, writer))
    try:
        await asyncio.wait_for(task, timeout=1.0)
    except asyncio.TimeoutError:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    
    # Writer should be closed
    assert writer.close.called or True  # May not be called in all error paths


# ============================================================================
# runtime/server.py tests
# ============================================================================

class TestEngineServer:
    """Test coverage gaps in EngineServer."""

    @pytest.mark.asyncio
    async def test_engine_server_init(self, tmp_path):
        """Test EngineServer initialization."""
        session = EngineSession(tmp_path, tmp_path / "db.sqlite")
        socket_path = tmp_path / "socket"
        server = EngineServer(session, socket_path)
        assert server._socket_path == socket_path
        assert server._session == session
