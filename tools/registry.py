from __future__ import annotations

from llm.provider import ToolSpec
from tools.base import BaseTool, ToolContext, ToolResult


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        if not tool.name:
            raise ValueError("tool is missing a name")
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool:
        if name not in self._tools:
            raise KeyError(f"unknown tool: {name}")
        return self._tools[name]

    def for_agent(self, role: str, names: list[str] | None = None) -> list[BaseTool]:
        selected = []
        wanted = set(names) if names is not None else set(self._tools)
        for name, tool in self._tools.items():
            if name not in wanted:
                continue
            if role not in tool.allowed_roles:
                continue
            selected.append(tool)
        return selected

    def specs_for(self, role: str, names: list[str] | None = None) -> list[ToolSpec]:
        return [
            ToolSpec(
                name=tool.name,
                description=tool.description,
                parameters=tool.parameters,
            )
            for tool in self.for_agent(role, names)
        ]

    async def execute(self, name: str, ctx: ToolContext, args: dict) -> ToolResult:
        tool = self.get(name)
        if ctx.role not in tool.allowed_roles:
            return ToolResult(
                ok=False,
                content=f"tool {name!r} is not allowed for role {ctx.role!r}",
            )
        return await tool.execute(ctx, **(args or {}))
