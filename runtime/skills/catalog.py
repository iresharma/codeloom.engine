from __future__ import annotations

import re

from runtime.skills.discover import Skill, discover_skills

TOP_K = 8
STICKY_CAP = 12
_TOKEN = re.compile(r"[a-z0-9]+")


class SkillCatalog:
    def __init__(self, skills: list[Skill] | None = None):
        self._skills: dict[str, Skill] = {item.name: item for item in (skills or [])}

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def list(self) -> list[Skill]:
        return list(self._skills.values())

    def rows(self):
        return [item.row() for item in self._skills.values()]

    def names(self, *, unlocked: set[str] | None = None, activated: set[str] | None = None) -> list[str]:
        extra = set(unlocked or ()) | set(activated or ())
        return [
            item.name
            for item in self._skills.values()
            if item.auto or item.name in extra
        ]


def lexical_score(query: str, skill: Skill) -> int:
    needles = set(_TOKEN.findall((query or "").lower()))
    hay = set(_TOKEN.findall(f"{skill.name} {skill.description}".lower()))
    if not needles:
        return 0
    return len(needles & hay)


def rank_descriptions(
    skills: list[Skill],
    query: str,
    sticky: set[str],
    *,
    top_k: int = TOP_K,
    sticky_cap: int = STICKY_CAP,
) -> set[str]:
    scored = [(lexical_score(query, item), item.name) for item in skills]
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    top = {name for _, name in scored[:top_k]}
    shown = set(top)
    shown |= sticky & {item.name for item in skills}
    if len(shown) > sticky_cap:
        order = {name: score for score, name in scored}
        sticky_only = shown - top
        victims = sorted(sticky_only, key=lambda name: (order.get(name, 0), name))
        drop = len(shown) - sticky_cap
        shown -= set(victims[:drop])
    sticky.clear()
    sticky.update(shown)
    return shown


def render_catalog(
    catalog: SkillCatalog,
    query: str,
    sticky: set[str],
    *,
    unlocked: set[str] | None = None,
    activated: set[str] | None = None,
    bodies: dict[str, str] | None = None,
    top_k: int = TOP_K,
    sticky_cap: int = STICKY_CAP,
) -> str:
    extra = set(unlocked or ()) | set(activated or ())
    visible = [
        item
        for item in catalog.list()
        if item.auto or item.name in extra
    ]
    names = [item.name for item in visible]
    shown = rank_descriptions(visible, query, sticky, top_k=top_k, sticky_cap=sticky_cap)
    by_name = {item.name: item for item in visible}
    lines = ["Available skills (call activate_skill by name):"]
    for name in names:
        item = by_name[name]
        if name in shown and item.description:
            lines.append(f"- {name}: {item.description}")
        else:
            lines.append(f"- {name}")
    text = "\n".join(lines)
    if bodies:
        chunks = []
        for name, body in bodies.items():
            chunks.append(f"### {name}\n{body.rstrip()}")
        if chunks:
            text += "\n\nActive skill bodies:\n" + "\n\n".join(chunks)
    return text


def load_catalog(workspace, *, home=None) -> SkillCatalog:
    return SkillCatalog(discover_skills(workspace, home=home))
