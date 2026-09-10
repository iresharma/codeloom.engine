from __future__ import annotations

import asyncio
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from runtime.mcp.bridge import (
    flatten_mcp_result,
    sanitize_mcp_name,
    scheme_allowed,
)
from runtime.mcp.config import DEFAULT_MCP_PROFILES, McpServerConfig
from tools.base import Tool, ToolContext

AUTH_MARKERS = (
    "unauthorized",
    "unauthenticated",
    "invalid_token",
    "invalid token",
    "401",
    "forbidden",
    "auth required",
    "authorization required",
    "login required",
    "needs_auth",
    "please log in",
    "please login",
)
_URL = re.compile(r"https://[^\s\"'<>]+")


def extract_auth_url(*chunks: str) -> str:
    for chunk in chunks:
        if not chunk:
            continue
        match = _URL.search(chunk)
        if match:
            return match.group(0).rstrip(").,]}>\"'")
    return ""


def is_auth_error(exc: BaseException | str) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in AUTH_MARKERS)


@dataclass
class McpServerState:
    config: McpServerConfig
    status: str = "starting"
    error: str = ""
    session: object | None = None
    tool_count: int = 0
    ever_ready: bool = False
    cool_until: float = 0.0
    connect_attempts: int = 0
    advertised: set[str] = field(default_factory=set)
    wire_names: list[str] = field(default_factory=list)
    remote_tools: list = field(default_factory=list)


class McpManager:
    def __init__(
        self,
        workspace,
        *,
        connect: Callable | None = None,
        backoff_s: tuple[float, ...] = (2.0, 4.0, 8.0),
        cool_s: float = 30.0,
        sleep=asyncio.sleep,
        clock=time.monotonic,
        ask_user=None,
        on_update=None,
        on_auth=None,
        on_warning=None,
        approval: str = "auto",
    ):
        self.workspace = workspace
        self._connect = connect or self._default_connect
        self.backoff_s = backoff_s
        self.cool_s = cool_s
        self._sleep = sleep
        self._clock = clock
        self.ask_user = ask_user
        self.on_update = on_update
        self.on_auth = on_auth
        self.on_warning = on_warning
        self.approval = approval
        self.servers: dict[str, McpServerState] = {}
        self.warnings: list[str] = []

    async def _default_connect(self, cfg: McpServerConfig):
        return await connect_stdio(cfg, ask_user=self.ask_user)

    def rows(self):
        from protocol.snapshot import McpServerRow

        return [
            McpServerRow(
                name=state.config.name,
                status=state.status,
                transport=state.config.transport,
                tool_count=state.tool_count,
                error=state.error,
            )
            for state in self.servers.values()
        ]

    def _emit(self) -> None:
        if self.on_update is not None:
            self.on_update(self.rows())

    def _warn(self, message: str) -> None:
        self.warnings.append(message)
        if self.on_warning is not None:
            self.on_warning(message)

    def _bind_elicitation(self, session) -> None:
        if session is None or self.ask_user is None:
            return

        async def callback(context, params):
            message = getattr(params, "message", None)
            if message is None and isinstance(params, dict):
                message = params.get("message")
            answer = await self.ask_user(str(message or params), kind="text")
            return {"action": "accept", "content": {"answer": answer}}

        session._engine_elicit = callback
        if getattr(session, "elicitation_callback", None) is None:
            try:
                session.elicitation_callback = callback
            except Exception:
                pass

    async def start(self, configs: list[McpServerConfig]) -> None:
        for cfg in configs:
            await self._start_one(cfg)
        self._emit()

    async def _start_one(self, cfg: McpServerConfig) -> McpServerState:
        state = self.servers.get(cfg.name) or McpServerState(config=cfg)
        state.config = cfg
        self.servers[cfg.name] = state
        if not cfg.enabled:
            state.status = "disabled"
            state.error = ""
            return state
        if cfg.unresolved:
            state.status = "error"
            state.error = f"unresolved ${{env:{cfg.unresolved}}}"
            self._warn(f"mcp server {cfg.name}: {state.error}")
            return state
        if cfg.transport not in {"stdio", ""}:
            state.status = "error"
            state.error = f"unsupported transport {cfg.transport}"
            return state
        try:
            session = await self._connect(cfg)
        except Exception as exc:  # noqa: BLE001
            return await self._fail_connect(state, exc)
        self._bind_elicitation(session)
        state.session = session

        def _streams() -> tuple[str, str]:
            refresh = getattr(session, "refresh_stdio", None)
            if refresh is not None:
                refresh()
            return getattr(session, "stdout", "") or "", getattr(session, "stderr", "") or ""

        url = extract_auth_url(*_streams())
        try:
            initialize = getattr(session, "initialize", None)
            if initialize is not None:
                await initialize()
            tools = await _call(session, "list_tools")
        except Exception as exc:  # noqa: BLE001
            url = extract_auth_url(*_streams()) or url
            if url or is_auth_error(exc):
                return await self._needs_auth(state, url, exc, first=not state.ever_ready)
            return await self._fail_connect(state, exc)
        url = extract_auth_url(*_streams()) or url
        if url:
            return await self._needs_auth(state, url, "login url", first=not state.ever_ready)
        state.remote_tools = _tool_list(tools)
        state.advertised = await self._list_resource_uris(session)
        state.status = "ready"
        state.error = ""
        state.ever_ready = True
        state.cool_until = 0.0
        state.tool_count = len(state.remote_tools)
        return state

    async def _list_resource_uris(self, session) -> set[str]:
        try:
            listed = await _call(session, "list_resources")
        except Exception:
            return set()
        uris = set()
        resources = getattr(listed, "resources", listed)
        if isinstance(listed, dict):
            resources = listed.get("resources") or []
        for item in resources or []:
            uri = item.get("uri") if isinstance(item, dict) else getattr(item, "uri", "")
            if uri:
                uris.add(str(uri))
        return uris

    async def _fail_connect(self, state: McpServerState, exc: BaseException | str) -> McpServerState:
        if is_auth_error(exc):
            return await self._needs_auth(state, "", exc, first=not state.ever_ready)
        state.status = "error"
        state.error = str(exc)
        state.session = None
        self._warn(f"mcp server {state.config.name}: {state.error}")
        return state

    async def _needs_auth(
        self,
        state: McpServerState,
        url: str,
        exc: BaseException | str,
        *,
        first: bool,
    ) -> McpServerState:
        state.status = "needs_auth"
        if first and not _has_stored_token(state):
            state.error = f"never authed: {exc}" if exc else "never authed"
        else:
            state.error = "token rejected; CompleteMcpAuth"
        await self._close_session(state)
        if self.on_auth is not None:
            await _maybe_await(
                self.on_auth(state.config.name, url, first=first, token_env=state.config.token_env)
            )
        else:
            self._warn(f"mcp server {state.config.name} needs auth")
        return state

    async def aclose(self) -> None:
        for state in list(self.servers.values()):
            await self._close_session(state)
        self._emit()

    async def _close_session(self, state: McpServerState) -> None:
        session = state.session
        state.session = None
        if session is None:
            return
        closer = getattr(session, "aclose", None) or getattr(session, "close", None)
        if closer is None:
            return
        try:
            result = closer()
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            pass

    async def stop(self) -> None:
        await self.aclose()
        self.servers.clear()

    def clear_cooling(self) -> None:
        now = self._clock()
        for state in self.servers.values():
            state.cool_until = 0.0
            if state.status == "error" and "cooling" in state.error:
                state.error = ""

    async def restart(self, name: str) -> McpServerState | None:
        state = self.servers.get(name)
        if state is None:
            return None
        await self._close_session(state)
        state.cool_until = 0.0
        await self._start_one(state.config)
        self._emit()
        return self.servers.get(name)

    def tools(self) -> list[Tool]:
        tools: list[Tool] = []
        used: set[str] = set()
        for state in self.servers.values():
            state.wire_names = []
            if state.status != "ready":
                continue
            allowed = tuple(state.config.profiles) if state.config.profiles else DEFAULT_MCP_PROFILES
            for remote in state.remote_tools:
                remote_name = _tool_name(remote)
                wire = sanitize_mcp_name(state.config.name, remote_name)
                if wire in used:
                    self._warn(f"mcp tool name collision, skipped: {wire}")
                    continue
                used.add(wire)
                state.wire_names.append(wire)
                tools.append(
                    self._wrap_tool(state, remote_name, wire, remote, allowed)
                )
        if any(state.status == "ready" for state in self.servers.values()):
            allowed = set()
            for state in self.servers.values():
                if state.status != "ready":
                    continue
                allowed.update(state.config.profiles or DEFAULT_MCP_PROFILES)
            tools.append(self._list_resources_tool(tuple(sorted(allowed))))
            tools.append(self._read_resource_tool(tuple(sorted(allowed))))
        return tools

    def _wrap_tool(self, state: McpServerState, remote_name: str, wire: str, remote, allowed) -> Tool:
        schema = _input_schema(remote)
        read_only = _read_only(remote)
        safe = remote_name in set(state.config.safe_tools)

        async def execute(ctx: ToolContext, **kwargs) -> str:
            return await self.call_tool(
                state.config.name,
                remote_name,
                kwargs,
                ctx=ctx,
                read_only=read_only or safe,
            )

        return Tool(
            name=wire,
            description=_tool_description(remote, state.config.name),
            parameters=schema,
            fn=execute,
            family="mcp",
            mcp_profiles=allowed,
        )

    def _list_resources_tool(self, allowed) -> Tool:
        async def execute(ctx: ToolContext) -> str:  # noqa: ARG001
            lines = []
            for state in self.servers.values():
                if state.status != "ready" or state.session is None:
                    continue
                try:
                    listed = await _call(state.session, "list_resources")
                except Exception as exc:  # noqa: BLE001
                    lines.append(f"{state.config.name}: error: {exc}")
                    continue
                resources = getattr(listed, "resources", listed)
                if isinstance(listed, dict):
                    resources = listed.get("resources") or []
                for item in resources or []:
                    uri = item.get("uri") if isinstance(item, dict) else getattr(item, "uri", "")
                    name = item.get("name") if isinstance(item, dict) else getattr(item, "name", "")
                    lines.append(f"{state.config.name}\t{uri}\t{name}")
            return "\n".join(lines) or "(no resources)"

        return Tool(
            name="mcp_list_resources",
            description="List resources advertised by connected MCP servers.",
            parameters={"type": "object", "properties": {}},
            fn=execute,
            family="mcp",
            mcp_profiles=allowed,
        )

    def _read_resource_tool(self, allowed) -> Tool:
        async def execute(ctx: ToolContext, uri: str) -> str:  # noqa: ARG001
            return await self.read_resource(uri)

        return Tool(
            name="mcp_read_resource",
            description="Read one MCP resource by URI. file:// is rejected.",
            parameters={
                "type": "object",
                "properties": {
                    "uri": {"type": "string", "description": "Resource URI"},
                },
                "required": ["uri"],
            },
            fn=execute,
            family="mcp",
            mcp_profiles=allowed,
        )

    async def read_resource(self, uri: str) -> str:
        advertised: set[str] = set()
        for state in self.servers.values():
            advertised |= state.advertised
        err = scheme_allowed(uri, advertised or None)
        if err:
            return err
        last = "error: no MCP server could read the resource"
        for state in self.servers.values():
            if state.status != "ready" or state.session is None:
                continue
            if state.advertised and uri not in state.advertised:
                continue
            try:
                result = await self._call_session(state, "read_resource", uri)
            except Exception as exc:  # noqa: BLE001
                last = f"error: {exc}"
                continue
            return flatten_mcp_result(result) or str(result)
        return last

    async def call_tool(
        self,
        server: str,
        remote_name: str,
        arguments: dict,
        *,
        ctx: ToolContext | None = None,
        read_only: bool = False,
    ) -> str:
        state = self.servers.get(server)
        if state is None:
            return f"error: unknown mcp server {server}"
        if not read_only:
            denied = await self._approve(ctx, server, remote_name)
            if denied:
                return denied
        try:
            result = await self._ensure_call(state, remote_name, arguments)
        except _AuthRejected as exc:
            await self._needs_auth(state, "", exc, first=False)
            self._emit()
            return f"error: token rejected; CompleteMcpAuth ({server})"
        except _Cooling as exc:
            return str(exc)
        except Exception as exc:  # noqa: BLE001
            return f"error: {exc}"
        return flatten_mcp_result(result)

    async def _approve(self, ctx: ToolContext | None, server: str, remote_name: str) -> str:
        approval = self.approval
        if ctx is not None and ctx.config is not None:
            approval = getattr(ctx.config, "exec_approval", approval) or approval
        if approval == "never":
            return ""
        ask = self.ask_user
        if ctx is not None and ctx.ask_user is not None:
            ask = ctx.ask_user
        if ask is None:
            if approval == "always":
                return "error: user denied"
            return ""
        if approval not in {"always", "auto"}:
            return ""
        answer = await ask(
            f"Allow MCP tool {server}.{remote_name}?",
            kind="confirm",
            choices=["yes", "no"],
            default="no",
        )
        if str(answer).strip().lower() not in {"yes", "y"}:
            return "error: user denied"
        return ""

    async def _ensure_call(self, state: McpServerState, remote_name: str, arguments: dict):
        now = self._clock()
        if state.cool_until and now < state.cool_until:
            remain = max(0, int(state.cool_until - now))
            raise _Cooling(
                f"error: mcp server {state.config.name} is down; cooling, "
                f"retry in {remain}s or ReloadIntegrations"
            )
        if state.session is None or state.status != "ready":
            await self._reconnect_with_backoff(state)
        try:
            return await self._invoke(state, remote_name, arguments)
        except _AuthRejected:
            raise
        except Exception:
            await self._reconnect_with_backoff(state)
            try:
                return await self._invoke(state, remote_name, arguments)
            except _AuthRejected:
                raise
            except Exception:
                state.status = "error"
                state.cool_until = self._clock() + self.cool_s
                state.error = (
                    f"mcp server {state.config.name} is down; cooling, "
                    f"retry in {int(self.cool_s)}s or ReloadIntegrations"
                )
                await self._close_session(state)
                self._emit()
                raise _Cooling(f"error: {state.error}")

    async def _invoke(self, state: McpServerState, remote_name: str, arguments: dict):
        session = state.session
        if session is None:
            raise RuntimeError("mcp session missing")
        try:
            return await _call(session, "call_tool", remote_name, arguments)
        except Exception as exc:
            if is_auth_error(exc):
                raise _AuthRejected(str(exc)) from exc
            raise

    async def _call_session(self, state: McpServerState, method: str, *args):
        if state.session is None:
            raise RuntimeError("mcp session missing")
        return await _call(state.session, method, *args)

    async def _reconnect_with_backoff(self, state: McpServerState) -> None:
        last = None
        for delay in self.backoff_s:
            state.connect_attempts += 1
            try:
                await self._close_session(state)
                session = await self._connect(state.config)
                self._bind_elicitation(session)
                state.session = session
                initialize = getattr(session, "initialize", None)
                if initialize is not None:
                    await initialize()
                state.status = "ready"
                state.error = ""
                state.ever_ready = True
                return
            except Exception as exc:  # noqa: BLE001
                last = exc
                if is_auth_error(exc):
                    raise _AuthRejected(str(exc)) from exc
                if delay:
                    await self._sleep(delay)
        if last is not None:
            raise last
        raise RuntimeError("mcp reconnect failed")


class _Cooling(Exception):
    pass


class _AuthRejected(Exception):
    pass


def _has_stored_token(state: McpServerState) -> bool:
    env_name = state.config.token_env
    if not env_name:
        return False
    return bool(state.config.env.get(env_name))


def _tool_list(tools) -> list:
    if tools is None:
        return []
    if isinstance(tools, dict):
        return list(tools.get("tools") or [])
    listed = getattr(tools, "tools", tools)
    return list(listed or [])


def _tool_name(remote) -> str:
    if isinstance(remote, dict):
        return str(remote.get("name") or "tool")
    return str(getattr(remote, "name", None) or "tool")


def _tool_description(remote, server: str) -> str:
    if isinstance(remote, dict):
        desc = remote.get("description") or remote.get("name") or "MCP tool"
    else:
        desc = getattr(remote, "description", None) or getattr(remote, "name", None) or "MCP tool"
    return f"{desc} (mcp:{server})"


def _input_schema(remote) -> dict:
    if isinstance(remote, dict):
        schema = remote.get("inputSchema") or remote.get("input_schema") or remote.get("parameters")
    else:
        schema = (
            getattr(remote, "inputSchema", None)
            or getattr(remote, "input_schema", None)
            or getattr(remote, "parameters", None)
        )
    if isinstance(schema, dict) and schema.get("type"):
        return schema
    return {"type": "object", "properties": {}}


def _read_only(remote) -> bool:
    anns = None
    if isinstance(remote, dict):
        anns = remote.get("annotations") or {}
    else:
        anns = getattr(remote, "annotations", None) or {}
    if isinstance(anns, dict):
        return bool(anns.get("readOnlyHint") or anns.get("read_only_hint"))
    return bool(getattr(anns, "readOnlyHint", False) or getattr(anns, "read_only_hint", False))


async def _call(session, method: str, *args):
    fn = getattr(session, method, None)
    if fn is None:
        raise AttributeError(method)
    if method == "call_tool" and len(args) >= 2:
        try:
            return await fn(args[0], arguments=args[1])
        except TypeError:
            return await fn(*args)
    if method == "read_resource" and args:
        return await fn(args[0])
    result = fn(*args) if args else fn()
    if asyncio.iscoroutine(result):
        return await result
    return result


async def _maybe_await(value):
    if asyncio.iscoroutine(value):
        return await value
    return value


async def connect_stdio(cfg: McpServerConfig, ask_user=None):
    import tempfile

    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as exc:
        raise RuntimeError("mcp package not installed") from exc

    env = {**os.environ, "BROWSER": "echo", **(cfg.env or {})}
    params = StdioServerParameters(
        command=cfg.command,
        args=list(cfg.args),
        env=env,
        cwd=cfg.cwd,
    )
    errlog = tempfile.TemporaryFile()

    async def elicitation_callback(context, params):
        message = getattr(params, "message", None)
        if message is None and isinstance(params, dict):
            message = params.get("message")
        if ask_user is None:
            return {"action": "decline"}
        answer = await ask_user(str(message or params), kind="text")
        return {"action": "accept", "content": {"answer": answer}}

    try:
        cm = stdio_client(params, errlog=errlog)
    except TypeError:
        cm = stdio_client(params)
    read, write = await cm.__aenter__()
    try:
        session = ClientSession(read, write, elicitation_callback=elicitation_callback)
    except TypeError:
        session = ClientSession(read, write)
    await session.__aenter__()

    def _read_err() -> str:
        try:
            errlog.seek(0)
            return errlog.read().decode("utf-8", "replace")
        except Exception:
            return ""

    class _Bound:
        def __init__(self):
            self._cm = cm
            self._session = session
            self.stdout = ""
            self.stderr = _read_err()

        def refresh_stdio(self):
            self.stderr = _read_err()

        async def initialize(self):
            result = await session.initialize()
            self.refresh_stdio()
            return result

        async def list_tools(self):
            return await session.list_tools()

        async def list_resources(self):
            return await session.list_resources()

        async def read_resource(self, uri):
            return await session.read_resource(uri)

        async def call_tool(self, name, arguments=None):
            return await session.call_tool(name, arguments=arguments or {})

        async def aclose(self):
            with_suppress = True
            try:
                await session.__aexit__(None, None, None)
            except Exception:
                if not with_suppress:
                    raise
            try:
                await cm.__aexit__(None, None, None)
            except Exception:
                pass
            try:
                errlog.close()
            except Exception:
                pass

    return _Bound()
