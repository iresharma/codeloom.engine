from __future__ import annotations

from dataclasses import asdict, fields, is_dataclass
from typing import Any, TypeVar

T = TypeVar("T")


def to_dict(obj: Any) -> dict[str, Any]:
    data = asdict(obj)
    return {key: value for key, value in data.items() if value is not None}


def from_dict(cls: type[T], data: dict[str, Any]) -> T:
    if not is_dataclass(cls):
        raise TypeError(f"{cls} is not a dataclass")
    names = {item.name for item in fields(cls) if item.init}
    return cls(**{key: value for key, value in data.items() if key in names})
