from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, Union, get_args, get_origin, get_type_hints


@dataclass
class ProtocolMessage:
    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": type(self).__name__}
        for item in fields(self):
            value = getattr(self, item.name)
            if value is None:
                continue
            payload[item.name] = _encode(value)
        return payload

    @classmethod
    def from_json(cls, data: dict[str, Any]):
        hints = _type_hints(cls)
        kwargs: dict[str, Any] = {}
        for item in fields(cls):
            if item.name not in data:
                continue
            kwargs[item.name] = _decode(data[item.name], hints.get(item.name, Any))
        return cls(**kwargs)


def _type_hints(cls) -> dict[str, Any]:
    try:
        return get_type_hints(cls)
    except TypeError:
        module = __import__(cls.__module__, fromlist=["*"])
        globalns = vars(module)
        hints: dict[str, Any] = {}
        for name, raw in getattr(cls, "__annotations__", {}).items():
            hints[name] = _resolve_annotation(raw, globalns)
        return hints


def _resolve_annotation(raw: Any, globalns: dict[str, Any]) -> Any:
    if not isinstance(raw, str):
        return raw
    text = raw.strip()
    optional = False
    if text.endswith("| None"):
        text = text[: -len("| None")].rstrip()
        optional = True
    elif text.endswith("|None"):
        text = text[: -len("|None")].rstrip()
        optional = True
    try:
        resolved = eval(text, {"__builtins__": __builtins__}, globalns)
    except Exception:
        resolved = Any
    if optional:
        return Union[resolved, type(None)]
    return resolved


def _encode(value: Any) -> Any:
    if hasattr(value, "to_json") and callable(value.to_json):
        return value.to_json()
    if isinstance(value, list):
        return [_encode(item) for item in value]
    return value


def _decode(value: Any, annotation: Any) -> Any:
    annotation, optional = _unwrap_optional(annotation)
    if optional and (value is None or value == ""):
        return None
    origin = get_origin(annotation)
    if origin is list:
        inner = get_args(annotation)[0] if get_args(annotation) else Any
        return [_decode(item, inner) for item in value or []]
    if isinstance(annotation, type) and hasattr(annotation, "from_json"):
        if isinstance(value, annotation):
            return value
        return annotation.from_json(value)
    return value


def _unwrap_optional(annotation: Any) -> tuple[Any, bool]:
    if get_origin(annotation) is Union:
        args = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(args) == 1:
            return args[0], True
    return annotation, False
