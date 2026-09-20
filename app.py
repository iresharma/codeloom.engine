from __future__ import annotations

import argparse
import asyncio
import platform
import signal
import sys
from pathlib import Path

from runtime.http_server import HttpServer
from runtime.server import EngineServer
from runtime.session import EngineSession


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Engine JSON-IPC server")
    parser.add_argument(
        "workspace",
        nargs="?",
        default=".",
        help="project root (default: current directory)",
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help=(
            "enable the HTTP transport (POST /command, GET /events) "
            "alongside the unix socket"
        ),
    )
    parser.add_argument(
        "--http-host",
        default="127.0.0.1",
        help="host to bind the HTTP transport to (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=8765,
        help="port to bind the HTTP transport to (default: 8765)",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    workspace = Path(args.workspace).expanduser().resolve()
    engine_dir = workspace / ".engine"
    engine_dir.mkdir(parents=True, exist_ok=True)

    print("=== Engine Server Startup ===", flush=True)
    print(f"workspace: {workspace}", flush=True)
    print(f"engine dir: {engine_dir}", flush=True)
    print(f"db path: {engine_dir / 'session.db'}", flush=True)
    print(f"socket path: {engine_dir / 'engine.sock'}", flush=True)
    print(f"python version: {sys.version.split()[0]}", flush=True)
    print(f"platform: {platform.platform()}", flush=True)
    print("==============================", flush=True)

    session = EngineSession(workspace, db_path=engine_dir / "session.db")
    await session.start()
    server = EngineServer(session, socket_path=engine_dir / "engine.sock")
    http_server = (
        HttpServer(session, host=args.http_host, port=args.http_port)
        if args.http
        else None
    )
    loop = asyncio.get_running_loop()
    force = False

    async def _graceful() -> None:
        await session.aclose()
        server.stop()
        if http_server is not None:
            http_server.stop()

    def _stop() -> None:
        nonlocal force
        if force:
            session.close_session()
            server.stop()
            if http_server is not None:
                http_server.stop()
            return
        force = True
        loop.create_task(_graceful())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _stop)

    print(f"listening on {engine_dir / 'engine.sock'}", flush=True)
    if http_server is not None:
        print(f"http listening on {args.http_host}:{args.http_port}", flush=True)
        await asyncio.gather(server.serve(), http_server.serve())
    else:
        await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
