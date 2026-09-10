from __future__ import annotations

from runtime.skills.discover import read_skill_file
from tools.base import ToolContext, tool


@tool(
    description=(
        "Load a skill's SKILL.md body into this agent's context. "
        "Use a name from the Skills catalog."
    ),
    family="skills",
)
def activate_skill(ctx: ToolContext, name: str) -> str:
    if ctx.activate_skill is not None:
        return ctx.activate_skill(name)
    catalog = ctx.skills
    if catalog is None:
        return "error: skills are not available"
    skill = catalog.get(name)
    if skill is None:
        return f"error: unknown skill {name}"
    siblings = _siblings(skill)
    extra = f"\nSibling files: {', '.join(siblings)}" if siblings else ""
    return skill.body + extra


@tool(
    description="Read one file inside an activated skill directory (no path escape).",
    family="skills",
)
def read_skill(ctx: ToolContext, name: str, path: str) -> str:
    catalog = ctx.skills
    if catalog is None:
        return "error: skills are not available"
    skill = catalog.get(name)
    if skill is None:
        return f"error: unknown skill {name}"
    try:
        return read_skill_file(skill, path)
    except (ValueError, FileNotFoundError, OSError) as exc:
        return f"error: {exc}"


def _siblings(skill) -> list[str]:
    names = []
    try:
        for child in sorted(skill.directory.iterdir()):
            if child.name == "SKILL.md":
                continue
            names.append(child.name + ("/" if child.is_dir() else ""))
    except OSError:
        return []
    return names
