from __future__ import annotations

import argparse
import asyncio
import platform
import signal
import sys
from pathlib import Path

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

    def _stop() -> None:
        session.close_session()
        server.stop()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _stop)

    print(f"listening on {engine_dir / 'engine.sock'}", flush=True)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
