from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import deque
from pathlib import Path
from typing import Any

from protocol.events import parse_event
from protocol.snapshot import EngineSnapshot, FileTreeNode, OpenFileState


HELP = """
protocol
  say <text> / prompt <text>   submit_user_message
  answer <prompt_id> <text>    answer_prompt
  open <path> / close <path>
  snap                         request_snapshot
  abort [agent_id]
  shutdown
  start <workspace>            start_session
  raw {json}                   send a raw line
  quit

debug
  tree / git / agents / stats / chat / pending / file
  events [n]                   last N events (default 20)
  filter [type]                live-print only this event type
  quiet / verbose
  log [path]                   JSONL log (default .engine/debug.jsonl)
  check                        snapshot vs local mirror
  help
""".strip()


def default_socket(workspace: Path) -> Path:
    return workspace.resolve() / ".engine" / "engine.sock"


def clip(text: str, limit: int = 120) -> str:
    text = (text or "").replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def print_out(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


class LocalMirror:
    def __init__(self) -> None:
        self.snapshot = EngineSnapshot(workspace="")
        self.last_error: str | None = None
        self.context_path: str | None = None
        self.history: deque[dict[str, Any]] = deque(maxlen=200)

    def load_snapshot(self, data: dict[str, Any] | EngineSnapshot) -> None:
        self.snapshot = data if isinstance(data, EngineSnapshot) else EngineSnapshot.from_json(data)

    def apply_event(self, data: dict[str, Any]) -> None:
        self.history.append(data)
        name = data.get("event")
        if name == "session_ready" and data.get("snapshot"):
            self.load_snapshot(data["snapshot"])
            return
        try:
            event = parse_event(data)
        except (ValueError, TypeError):
            return
        snap = self.snapshot
        kind = type(event).__name__
        if kind == "ChatMessageAdded":
            snap.messages.append(event.message)
        elif kind == "UserPromptRequested":
            snap.pending_prompt = event.prompt
        elif kind == "FileTreeUpdated":
            snap.file_tree = event.tree
        elif kind == "FileContent":
            snap.open_files = [item for item in snap.open_files if item.path != event.path]
            snap.open_files.append(OpenFileState(path=event.path, content=event.content))
        elif kind == "FileChanged":
            if event.content is not None:
                for item in snap.open_files:
                    if item.path == event.path:
                        item.content = event.content
                        break
        elif kind in {"AgentStarted", "AgentUpdated", "AgentFinished"}:
            row = event.agent
            snap.agents = [item for item in snap.agents if item.id != row.id]
            snap.agents.append(row)
        elif kind == "GitStateUpdated":
            snap.git = event.git
        elif kind == "StatsUpdated":
            snap.stats = event.stats
        elif kind == "ContextFileUpdated":
            self.context_path = event.path
        elif kind == "ErrorOccurred":
            self.last_error = event.message
        elif kind == "SessionEnded":
            snap.pending_prompt = None

    def to_compare_dict(self) -> dict[str, Any]:
        snap = self.snapshot
        return {
            "workspace": snap.workspace,
            "messages": [(item.role, item.content) for item in snap.messages],
            "open_files": sorted(item.path for item in snap.open_files),
            "tree": sorted(node.name for node in snap.file_tree),
            "agents": sorted(
                (item.id, item.role, item.profile, item.status, item.current_tool)
                for item in snap.agents
            ),
            "stats": (
                snap.stats.tokens_in,
                snap.stats.tokens_out,
                snap.stats.active_agents,
            ),
            "git": (snap.git.branch, snap.git.dirty, list(snap.git.status_lines)),
            "pending": None
            if snap.pending_prompt is None
            else (snap.pending_prompt.id, snap.pending_prompt.question),
        }


def pretty_event(data: dict[str, Any]) -> str:
    name = data.get("event") or "event"
    if name == "chat_message_added":
        message = data.get("message") or {}
        return f"[chat] {message.get('role')}: {clip(str(message.get('content') or ''))}"
    if name in {"agent_started", "agent_updated", "agent_finished"}:
        agent = data.get("agent") or {}
        tool = f" tool={agent.get('current_tool')}" if agent.get("current_tool") else ""
        profile = agent.get("profile") or agent.get("role") or ""
        return f"[agent] {agent.get('id')} {profile} {agent.get('status')}{tool}"
    if name == "git_state_updated":
        git = data.get("git") or {}
        lines = git.get("status_lines") or []
        return f"[git] {git.get('branch') or '-'} dirty={git.get('dirty')} {len(lines)} files"
    if name == "stats_updated":
        stats = data.get("stats") or {}
        return (
            f"[stats] in={stats.get('tokens_in')} out={stats.get('tokens_out')} "
            f"active={stats.get('active_agents')} elapsed={stats.get('elapsed_s')}"
        )
    if name == "file_tree_updated":
        return f"[tree] {len(data.get('tree') or [])} roots"
    if name == "file_content":
        return f"[file] {data.get('path')} ({len(data.get('content') or '')} bytes)"
    if name == "file_changed":
        return f"[diff] {data.get('path')}"
    if name == "user_prompt_requested":
        prompt = data.get("prompt") or {}
        return f"[prompt] id={prompt.get('id')}  {clip(str(prompt.get('question') or ''))}"
    if name == "context_file_updated":
        return f"[context] {data.get('path')}"
    if name == "error_occurred":
        return f"[error] {data.get('message')}"
    if name == "session_ended":
        return f"[session] ended ({data.get('reason')})"
    if name == "session_ready":
        snap = data.get("snapshot") or {}
        return f"[session] ready workspace={snap.get('workspace')}"
    return f"[{name}] {clip(json.dumps(data, ensure_ascii=False), 160)}"


def pretty_tree(nodes: list[FileTreeNode], indent: str = "") -> list[str]:
    lines = []
    for node in nodes:
        mark = "/" if node.is_dir else ""
        lines.append(f"{indent}{node.name}{mark}")
        if node.children:
            lines.extend(pretty_tree(node.children, indent + "  "))
    return lines


def pretty_snapshot(snap: EngineSnapshot) -> str:
    parts = [
        f"workspace: {snap.workspace}",
        f"stats: in={snap.stats.tokens_in} out={snap.stats.tokens_out} "
        f"active={snap.stats.active_agents} elapsed={snap.stats.elapsed_s:.1f}s",
        f"git: {snap.git.branch or '-'} dirty={snap.git.dirty} files={len(snap.git.status_lines)}",
        f"agents: {len(snap.agents)}  messages: {len(snap.messages)}  open: {len(snap.open_files)}",
    ]
    if snap.pending_prompt:
        parts.append(f"pending: {snap.pending_prompt.id} {clip(snap.pending_prompt.question)}")
    return "\n".join(parts)


def diff_mirrors(local: dict[str, Any], remote: dict[str, Any]) -> list[str]:
    mismatches = []
    for key in sorted(set(local) | set(remote)):
        if local.get(key) != remote.get(key):
            mismatches.append(f"{key}: local={local.get(key)!r} remote={remote.get(key)!r}")
    return mismatches


class EngineClient:
    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path
        self.mirror = LocalMirror()
        self.verbose = True
        self.filter_type: str | None = None
        self.log_path: Path | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._seq = 0
        self._closed = False

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.open_unix_connection(str(self.socket_path))
        hello = await self._readline()
        if hello:
            self._dispatch(hello)

    async def close(self) -> None:
        self._closed = True
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass

    async def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._writer is None:
            raise RuntimeError("not connected")
        self._seq += 1
        req_id = str(payload.get("id") or self._seq)
        payload["id"] = req_id
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[req_id] = future
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        self._writer.write(line.encode("utf-8"))
        await self._writer.drain()
        return await future

    async def read_loop(self) -> None:
        try:
            while not self._closed:
                data = await self._readline()
                if data is None:
                    print_out("[client] disconnected")
                    break
                self._dispatch(data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print_out(f"[client] read error: {exc}")

    async def _readline(self) -> dict[str, Any] | None:
        assert self._reader is not None
        raw = await self._reader.readline()
        if not raw:
            return None
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            return {}
        return json.loads(line)

    def _dispatch(self, data: dict[str, Any]) -> None:
        if not data:
            return
        req_id = data.get("id")
        if req_id is not None and str(req_id) in self._pending:
            future = self._pending.pop(str(req_id))
            if not future.done():
                future.set_result(data)
            if data.get("snapshot") and not data.get("event"):
                self.mirror.load_snapshot(data["snapshot"])
            if not data.get("event"):
                print_out(f"[ack] ok={data.get('ok')} id={req_id}" + (f" error={data.get('error')}" if data.get("error") else ""))
        if data.get("event"):
            self.mirror.apply_event(data)
            self._log_event(data)
            if self._should_print(data.get("event")):
                print_out(pretty_event(data))
        elif data.get("snapshot") and req_id is None:
            self.mirror.load_snapshot(data["snapshot"])

    def _should_print(self, event_type: str | None) -> bool:
        if not self.verbose:
            return False
        if self.filter_type and event_type != self.filter_type:
            return False
        return True

    def _log_event(self, data: dict[str, Any]) -> None:
        if self.log_path is None:
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(data, ensure_ascii=False) + "\n")


class Repl:
    def __init__(self, client: EngineClient) -> None:
        self.client = client

    async def run(self) -> None:
        print_out(HELP)
        print_out(f"connected {self.client.socket_path}")
        while True:
            try:
                line = await asyncio.to_thread(input, "engine> ")
            except (EOFError, KeyboardInterrupt):
                print_out("")
                break
            text = line.strip()
            if not text:
                continue
            try:
                keep = await self.dispatch(text)
            except Exception as exc:
                print_out(f"[error] {exc}")
                continue
            if not keep:
                break

    async def dispatch(self, line: str) -> bool:
        cmd, _, rest = line.partition(" ")
        rest = rest.strip()
        name = cmd.lower()
        if name in {"quit", "exit", "q"}:
            return False
        if name == "help":
            print_out(HELP)
            return True
        if name in {"say", "prompt"}:
            if not rest:
                print_out("usage: say <text>")
                return True
            await self.client.send({"cmd": "submit_user_message", "text": rest})
            return True
        if name == "answer":
            prompt_id, _, text = rest.partition(" ")
            if not prompt_id or not text.strip():
                print_out("usage: answer <prompt_id> <text>")
                return True
            await self.client.send({"cmd": "answer_prompt", "prompt_id": prompt_id, "text": text.strip()})
            return True
        if name == "open":
            await self.client.send({"cmd": "open_file", "path": rest})
            return True
        if name == "close":
            await self.client.send({"cmd": "close_file", "path": rest})
            return True
        if name == "snap":
            reply = await self.client.send({"cmd": "request_snapshot"})
            if reply.get("snapshot"):
                snap = EngineSnapshot.from_json(reply["snapshot"])
                self.client.mirror.load_snapshot(snap)
                print_out(pretty_snapshot(snap))
            return True
        if name == "abort":
            payload: dict[str, Any] = {"cmd": "abort_agent"}
            if rest:
                payload["agent_id"] = rest
            await self.client.send(payload)
            return True
        if name == "shutdown":
            await self.client.send({"cmd": "shutdown"})
            return True
        if name == "start":
            if not rest:
                print_out("usage: start <workspace>")
                return True
            reply = await self.client.send({"cmd": "start_session", "workspace": rest})
            if reply.get("snapshot"):
                self.client.mirror.load_snapshot(reply["snapshot"])
                print_out(pretty_snapshot(self.client.mirror.snapshot))
            return True
        if name == "raw":
            await self.client.send(json.loads(rest))
            return True
        if name == "tree":
            lines = pretty_tree(self.client.mirror.snapshot.file_tree)
            print_out("\n".join(lines) or "(empty tree)")
            return True
        if name == "git":
            git = self.client.mirror.snapshot.git
            print_out(
                f"branch={git.branch or '-'} dirty={git.dirty} "
                f"staged={len(git.staged_diff)}B unstaged={len(git.unstaged_diff)}B"
            )
            for item in git.status_lines:
                print_out(f"  {item}")
            return True
        if name == "agents":
            rows = self.client.mirror.snapshot.agents
            if not rows:
                print_out("(no agents)")
                return True
            for agent in rows:
                tool = f" tool={agent.current_tool}" if agent.current_tool else ""
                print_out(f"{agent.id}  {agent.role}  {agent.profile or '-'}  {agent.status}{tool}")
            return True
        if name == "stats":
            print_out(pretty_snapshot(self.client.mirror.snapshot).split("\n")[1])
            return True
        if name == "chat":
            messages = self.client.mirror.snapshot.messages
            if not messages:
                print_out("(no messages)")
                return True
            for message in messages:
                print_out(f"{message.role}: {clip(message.content, 200)}")
            return True
        if name == "pending":
            prompt = self.client.mirror.snapshot.pending_prompt
            if prompt is None:
                print_out("(no pending prompt)")
            else:
                print_out(f"{prompt.id}: {prompt.question}")
            return True
        if name == "file":
            files = self.client.mirror.snapshot.open_files
            if not files:
                print_out("(no open file)")
                return True
            item = files[-1]
            preview = "\n".join(item.content.splitlines()[:12])
            print_out(f"{item.path}\n{preview}")
            return True
        if name == "events":
            count = int(rest) if rest else 20
            items = list(self.client.mirror.history)[-count:]
            if not items:
                print_out("(no events yet)")
                return True
            for item in items:
                print_out(pretty_event(item))
            return True
        if name == "filter":
            self.client.filter_type = rest or None
            print_out(f"filter={self.client.filter_type or 'off'}")
            return True
        if name == "quiet":
            self.client.verbose = False
            print_out("quiet")
            return True
        if name == "verbose":
            self.client.verbose = True
            print_out("verbose")
            return True
        if name == "log":
            path = Path(rest) if rest else Path(".engine/debug.jsonl")
            self.client.log_path = path
            print_out(f"logging {path}")
            return True
        if name == "check":
            before = self.client.mirror.to_compare_dict()
            reply = await self.client.send({"cmd": "request_snapshot"})
            if not reply.get("snapshot"):
                print_out("check failed: no snapshot in ack")
                return True
            remote = LocalMirror()
            remote.load_snapshot(reply["snapshot"])
            mismatches = diff_mirrors(before, remote.to_compare_dict())
            if not mismatches:
                print_out("check ok: local mirror matches snapshot")
            else:
                print_out("check mismatches:")
                for item in mismatches:
                    print_out(f"  {item}")
            return True
        print_out(f"unknown command: {cmd}  (help)")
        return True


async def wait_for_socket(path: Path, timeout: float = 8.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if path.exists():
            return
        await asyncio.sleep(0.05)
    raise FileNotFoundError(f"engine socket not found: {path}")


async def maybe_spawn(workspace: Path, socket_path: Path) -> asyncio.subprocess.Process | None:
    if socket_path.exists():
        return None
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(Path(__file__).resolve().parent / "app.py"),
        str(workspace),
        "--socket",
        str(socket_path),
    )
    await wait_for_socket(socket_path)
    print_out(f"[client] spawned engine pid={process.pid}")
    return process


async def async_main(args: argparse.Namespace) -> None:
    workspace = Path(args.workspace).resolve()
    socket_path = Path(args.socket).resolve() if args.socket else default_socket(workspace)
    child = None
    if args.spawn:
        child = await maybe_spawn(workspace, socket_path)
    elif not socket_path.exists():
        raise SystemExit(
            f"socket not found: {socket_path}\nstart the engine with: python app.py {workspace}\n"
            "or pass --spawn"
        )
    client = EngineClient(socket_path)
    await client.connect()
    reader = asyncio.create_task(client.read_loop(), name="dummy-read")
    try:
        await Repl(client).run()
    finally:
        reader.cancel()
        try:
            await reader
        except asyncio.CancelledError:
            pass
        await client.close()
        if child is not None and child.returncode is None:
            child.terminate()
            try:
                await asyncio.wait_for(child.wait(), timeout=2)
            except asyncio.TimeoutError:
                child.kill()


def main() -> None:
    parser = argparse.ArgumentParser(description="REPL dummy client for the engine Unix socket.")
    parser.add_argument("--workspace", default=".", help="Workspace used to find the default socket")
    parser.add_argument("--socket", default=None, help="Unix socket path")
    parser.add_argument("--spawn", action="store_true", help="Start app.py if the socket is missing")
    args = parser.parse_args()
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
