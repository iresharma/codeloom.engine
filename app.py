from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from llm.provider import build_provider
from runtime.server import EngineServer
from runtime.session import EngineSession


def default_socket(workspace: Path) -> Path:
    return workspace.resolve() / ".engine" / "engine.sock"


async def run(workspace: Path, socket_path: Path) -> None:
    session = EngineSession(workspace, build_provider())
    await session.start()
    server = EngineServer(session, socket_path)
    print(f"engine listening on {socket_path}")
    try:
        await server.serve()
    finally:
        await session.shutdown()
        await server.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Start the agentic coder engine server.")
    parser.add_argument("workspace", nargs="?", default=".", help="Project root the engine will operate on")
    parser.add_argument("--socket", default=None, help="Unix socket path (default: <workspace>/.engine/engine.sock)")
    args = parser.parse_args()
    workspace = Path(args.workspace).resolve()
    socket_path = Path(args.socket).resolve() if args.socket else default_socket(workspace)
    asyncio.run(run(workspace, socket_path))


if __name__ == "__main__":
    main()
