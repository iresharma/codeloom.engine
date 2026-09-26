from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Union, get_args, get_origin

_SKIP_PARAMS = {"ctx", "context", "self"}
_JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean"}

ELIDED_MARKER = "\n... [{n} chars elided] ...\n"


def elide_middle(text: str, head: int, tail: int) -> str:
    """Keep the first `head` and last `tail` characters, naming what went.

    Head *and* tail, never head alone. Every runner prints its verdict last
    ("92 passed, 2 failed in 4.2s"), so a head-only cut removes the one line
    an agent needs and leaves it free to invent the counts. Each caller
    names its own budget; this is only the mechanism.
    """
    if head < 0 or tail < 0:
        raise ValueError("head and tail must be non-negative")
    if len(text) <= head + tail:
        return text
    omitted = len(text) - head - tail
    kept_tail = text[-tail:] if tail else ""
    return text[:head] + ELIDED_MARKER.format(n=omitted) + kept_tail


@dataclass
class ToolContext:
    workspace: Path
    language: Any = None
    lsp: Any = None
    files: Any = None
    journal: Any = None
    session_id: str | None = None
    on_edit: Any = None
    config: Any = None
    ask_user: Any = None
    on_output: Any = None
    on_proc: Any = None
    agent_id: str = ""
    role: str = ""
    profile: str = ""
    write_globs: list[str] | None = None
    write_lock: Any = None
    skills: Any = None
    activate_skill: Any = None
    unlocked_skills: Any = None
    on_memory: Any = None
    judge: Any = None
    on_judgement: Any = None
    user_request: str = ""
    # The verify command the harness chose for this worktree. `run_verify`
    # runs only this; an agent cannot choose or override it.
    verify_command: str = ""
    # The commit that worktree branched from, so `run_verify` can tell a
    # failure the change caused from one already there.
    verify_base: str = ""
    # The last run_command this agent ran that exited 0. The harness verify
    # stage falls back to it when config and detection both come up empty.
    last_command_ok: str = ""


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable
    family: str = ""
    mcp_profiles: tuple[str, ...] | None = None

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    async def execute(self, ctx: ToolContext, arguments: dict) -> str:
        sig = inspect.signature(self.fn)
        allowed = {name for name in sig.parameters if name not in _SKIP_PARAMS}
        kwargs = {key: value for key, value in arguments.items() if key in allowed}
        if any(name in sig.parameters for name in ("ctx", "context")):
            result = self.fn(ctx, **kwargs)
        else:
            result = self.fn(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        if result is None:
            return ""
        return result if isinstance(result, str) else str(result)


def tool(
    description: str | None = None,
    *,
    name: str | None = None,
    parameters: dict | None = None,
    family: str = "",
) -> Callable:
    """Register a function as an engine tool.

    Drop a module under tools/ (or a subpackage) and decorate the handler.
    The engine imports the package tree and picks it up; no registry edit.
    """

    def wrap(fn: Callable) -> Callable:
        spec = Tool(
            name=name or fn.__name__,
            description=(description or fn.__doc__ or fn.__name__).strip(),
            parameters=parameters or _schema_from_fn(fn),
            fn=fn,
            family=family,
        )
        fn._engine_tool = spec
        return fn

    return wrap


def _schema_from_fn(fn: Callable) -> dict:
    try:
        hints = inspect.get_type_hints(fn)
    except (TypeError, NameError, AttributeError, ValueError):
        hints = {}
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, param in inspect.signature(fn).parameters.items():
        if name in _SKIP_PARAMS:
            continue
        properties[name] = {"type": _json_type(hints.get(name, str))}
        if param.default is inspect.Parameter.empty:
            required.append(name)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _json_type(annotation: Any) -> str:
    origin = get_origin(annotation)
    if origin is Union:
        args = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(args) == 1:
            return _json_type(args[0])
    return _JSON_TYPES.get(annotation, "string")
