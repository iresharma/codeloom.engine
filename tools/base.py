from __future__ import annotations

import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Union, get_args, get_origin

_SKIP_PARAMS = {"ctx", "context", "self"}
_JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean"}


@dataclass
class ToolContext:
    workspace: Path


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable

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
        )
        fn._engine_tool = spec
        return fn

    return wrap


def _schema_from_fn(fn: Callable) -> dict:
    try:
        hints = inspect.get_type_hints(fn)
    except Exception:
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
