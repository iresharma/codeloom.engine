from __future__ import annotations

from pathlib import Path

from protocol.snapshot import SkillRow


def parse_skill_md(text: str) -> tuple[dict[str, str], str]:
    raw = text.lstrip("\ufeff")
    if not raw.startswith("---"):
        return {}, raw
    rest = raw[3:]
    if rest.startswith("\n"):
        rest = rest[1:]
    end = rest.find("\n---")
    if end < 0:
        return {}, text
    header = rest[:end]
    body = rest[end + 4 :].lstrip("\n")
    meta: dict[str, str] = {}
    for line in header.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip("'\"")
    return meta, body


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Skill:
    def __init__(
        self,
        name: str,
        description: str,
        source: str,
        directory: Path,
        body: str,
        auto: bool = True,
    ):
        self.name = name
        self.description = description
        self.source = source
        self.directory = directory
        self.body = body
        self.auto = auto

    def row(self) -> SkillRow:
        return SkillRow(
            name=self.name,
            description=self.description,
            source=self.source,
            auto=self.auto,
        )


def discover_skills(workspace: Path, *, home: Path | None = None) -> list[Skill]:
    workspace = Path(workspace).resolve()
    home = Path(home or Path.home())
    roots = [
        ("engine", workspace / ".engine" / "skills"),
        ("cursor", workspace / ".cursor" / "skills"),
        ("user", home / ".engine" / "skills"),
    ]
    by_name: dict[str, Skill] = {}
    for source, root in roots:
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            md = child / "SKILL.md" if child.is_dir() else None
            if md is None or not md.is_file():
                continue
            try:
                text = md.read_text(encoding="utf-8")
            except OSError:
                continue
            meta, body = parse_skill_md(text)
            name = (meta.get("name") or child.name).strip()
            if not name or name in by_name:
                continue
            auto = not _truthy(meta.get("disable-model-invocation") or "")
            skill = Skill(
                name=name,
                description=(meta.get("description") or "").strip(),
                source=source,
                directory=child.resolve(),
                body=body,
                auto=auto,
            )
            by_name[name] = skill
    return list(by_name.values())


def read_skill_file(skill: Skill, rel: str) -> str:
    if not rel or rel.endswith("/"):
        raise ValueError("path required")
    root = skill.directory.resolve()
    target = (root / rel).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path escapes skill directory: {rel}") from exc
    if not target.is_file():
        raise FileNotFoundError(rel)
    return target.read_text(encoding="utf-8")
