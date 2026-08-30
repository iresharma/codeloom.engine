from __future__ import annotations

import importlib
import pkgutil

import tools as tools_pkg
from tools.base import Tool, ToolContext

MAX_RESULT = 80_000


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self.errors: list[str] = []

    def register(self, spec: Tool) -> None:
        if spec.name in self._tools:
            self.errors.append(f"duplicate tool name: {spec.name}")
            return
        self._tools[spec.name] = spec

    def schemas(self) -> list[dict]:
        return [spec.schema() for spec in self._tools.values()]

    async def execute(self, name: str, ctx: ToolContext, arguments: dict) -> str:
        spec = self._tools.get(name)
        if spec is None:
            return f"error: unknown tool {name}"
        try:
            text = await spec.execute(ctx, arguments)
        except Exception as exc:
            return f"error: {exc}"
        if len(text) > MAX_RESULT:
            return text[:MAX_RESULT] + "\n...[truncated]"
        return text


def discover_tools() -> ToolRegistry:
    """Import every module under tools/ except this file and base.py."""
    registry = ToolRegistry()
    prefix = tools_pkg.__name__ + "."
    skip = {tools_pkg.__name__ + ".base", tools_pkg.__name__ + ".registry"}
    for info in pkgutil.walk_packages(tools_pkg.__path__, prefix):
        if info.name in skip or info.name.rsplit(".", 1)[-1].startswith("_"):
            continue
        try:
            module = importlib.import_module(info.name)
            module = importlib.reload(module)
        except Exception as exc:
            registry.errors.append(f"{info.name}: {exc}")
            continue
        for value in vars(module).values():
            spec = getattr(value, "_engine_tool", None)
            if isinstance(spec, Tool):
                registry.register(spec)
    return registry
