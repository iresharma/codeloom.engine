from __future__ import annotations

import asyncio
from pathlib import Path

from agents.agent_loop import AgentLoop
from agents.profile import discover_profiles
from agents.subagent import Subagent
from protocol.commands import ActivateSkill, StartSession, SubmitUserMessage
from protocol.events import SkillActivated
from protocol.snapshot import SkillRow
from runtime.session import EngineSession
from runtime.skills.catalog import SkillCatalog, rank_descriptions, render_catalog
from runtime.skills.discover import discover_skills, read_skill_file
from tests.fakes import FakeProvider
from tools.registry import discover_tools


def _write_skill(root: Path, name: str, description: str, body: str, *, disable: bool = False):
    dest = root / name
    dest.mkdir(parents=True, exist_ok=True)
    front = [f"name: {name}", f"description: {description}"]
    if disable:
        front.append("disable-model-invocation: true")
    (dest / "SKILL.md").write_text(
        "---\n" + "\n".join(front) + "\n---\n" + body, encoding="utf-8"
    )
    return dest


def test_skill_tools_registered():
    names = discover_tools().names()
    assert "activate_skill" in names
    assert "read_skill" in names


def test_discover_first_match_and_duplicate(tmp_path, monkeypatch):
    home = tmp_path / "home"
    engine = tmp_path / ".engine" / "skills"
    cursor = tmp_path / ".cursor" / "skills"
    user = home / ".engine" / "skills"
    _write_skill(engine, "shared", "from engine", "ENGINE")
    _write_skill(cursor, "shared", "from cursor", "CURSOR")
    _write_skill(cursor, "cursor_only", "cursor skill", "C")
    _write_skill(user, "shared", "from user", "USER")
    _write_skill(user, "user_only", "user skill", "U")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    skills = {item.name: item for item in discover_skills(tmp_path, home=home)}
    assert skills["shared"].body.strip() == "ENGINE"
    assert skills["shared"].source == "engine"
    assert "cursor_only" in skills
    assert "user_only" in skills


def test_read_skill_jail(tmp_path):
    dest = _write_skill(tmp_path / ".engine" / "skills", "docs", "docs", "BODY")
    (dest / "reference.md").write_text("REF", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")
    skill = discover_skills(tmp_path)[0]
    assert "REF" in read_skill_file(skill, "reference.md")
    try:
        read_skill_file(skill, "../secret.txt")
        raise AssertionError("escaped")
    except ValueError:
        pass
    try:
        read_skill_file(skill, str(tmp_path / "secret.txt"))
        raise AssertionError("absolute")
    except ValueError:
        pass


def test_disable_model_invocation_catalog(tmp_path):
    _write_skill(tmp_path / ".engine" / "skills", "auto", "auto skill", "A")
    _write_skill(
        tmp_path / ".engine" / "skills", "hidden", "hidden skill", "H", disable=True
    )
    catalog = SkillCatalog(discover_skills(tmp_path))
    text = render_catalog(catalog, "auto", set())
    assert "- auto" in text
    assert "hidden" not in text
    rows = {row.name: row for row in catalog.rows()}
    assert rows["hidden"].auto is False
    assert isinstance(rows["hidden"], SkillRow)


def test_catalog_names_and_sticky():
    skills = [
        type("S", (), {"name": f"s{i}", "description": f"topic{i} extra", "auto": True})()
        for i in range(20)
    ]
    # rank_descriptions expects Skill-like name+description
    from runtime.skills.discover import Skill

    real = [
        Skill(name=f"s{i}", description=f"topic{i} keyword{i}", source="t", directory=Path("."), body="", auto=True)
        for i in range(20)
    ]
    sticky: set[str] = set()
    shown1 = rank_descriptions(real, "keyword0 keyword1 keyword2", sticky, top_k=3, sticky_cap=8)
    assert "s0" in shown1
    shown2 = rank_descriptions(real, "keyword10 keyword11 keyword12", sticky, top_k=3, sticky_cap=8)
    assert "s0" in shown2
    assert len(shown2) <= 8
    tight: set[str] = set()
    rank_descriptions(real, "keyword0 keyword1 keyword2", tight, top_k=3, sticky_cap=5)
    shown3 = rank_descriptions(real, "keyword10 keyword11 keyword12", tight, top_k=3, sticky_cap=5)
    assert {"s10", "s11", "s12"} <= shown3
    assert len(shown3) <= 5
    catalog = SkillCatalog(real)
    text = render_catalog(catalog, "nothing-matches-xyz", set())
    assert "- s19" in text
    assert catalog.get("s0") is not None


def test_activate_skill_without_description():
    from runtime.skills.discover import Skill

    skill = Skill(name="quiet", description="", source="t", directory=Path("."), body="BODY", auto=True)
    catalog = SkillCatalog([skill])
    loop = AgentLoop(FakeProvider(), skills=catalog)
    assert "quiet" in render_catalog(catalog, "", set())
    assert "BODY" in loop.activate_skill("quiet")


def test_activation_lifetimes(tmp_path):
    dest = _write_skill(tmp_path / ".engine" / "skills", "docs", "write docs", "DOCS BODY")
    (dest / "examples.md").write_text("EX", encoding="utf-8")
    _write_skill(
        tmp_path / ".engine" / "skills", "secret", "hidden", "SECRET", disable=True
    )
    catalog = SkillCatalog(discover_skills(tmp_path))
    orch = AgentLoop(FakeProvider(), skills=catalog, role="orchestrator")
    profiles = discover_profiles()
    child = Subagent(
        profiles.get("ask"),
        llm=FakeProvider(),
        tools=discover_tools().subset(profiles.get("ask").tool_names, profile="ask"),
        workspace=tmp_path,
        skills=catalog,
    )
    child.activate_skill("docs")
    assert "DOCS BODY" in child.context_dump()
    assert "DOCS BODY" not in orch.context_dump()
    orch.activate_skill("docs")
    assert "DOCS BODY" in orch.context_dump()
    orch.set_catalog_query("later question")
    orch.hydrate([])
    orch._history.append({"role": "user", "content": "second turn"})
    assert "DOCS BODY" in orch.context_dump()
    assert orch.unlock_skill("secret")
    unlocked = render_catalog(catalog, "hidden", set(), unlocked={"secret"})
    assert "secret" in unlocked
    assert "SECRET" not in unlocked


def test_activate_skill_command_session_scope(tmp_path):
    _write_skill(
        tmp_path / ".engine" / "skills", "hidden", "hidden skill", "SECRET", disable=True
    )

    async def run():
        session = EngineSession(tmp_path, tmp_path / "session.db")
        await session.start()
        session._llm = FakeProvider()
        await session.handle(StartSession(workspace=str(tmp_path)))
        session._loop._llm = FakeProvider()
        assert "hidden" not in session._loop.context_dump()
        await session.handle(ActivateSkill(name="hidden"))
        dump = session._loop.context_dump()
        assert "hidden" in dump
        assert "SECRET" not in dump
        session._loop.activate_skill("hidden")
        assert "SECRET" in session._loop.context_dump()
        await session.handle(SubmitUserMessage(text="again"))
        for _ in range(50):
            task = session._turn_task
            if task is None or task.done():
                break
            await asyncio.sleep(0.02)
        assert "SECRET" in session._loop.context_dump()
        # new session clears unlock + body
        session2 = EngineSession(tmp_path, tmp_path / "session.db")
        await session2.start()
        session2._llm = FakeProvider()
        await session2.handle(StartSession(workspace=str(tmp_path)))
        session2._loop._llm = FakeProvider()
        assert "SECRET" not in session2._loop.context_dump()
        assert "- hidden:" not in session2._loop.context_dump()
        await session.aclose()
        await session2.aclose()

    asyncio.run(run())


def test_skill_activated_event(tmp_path):
    _write_skill(tmp_path / ".engine" / "skills", "docs", "docs", "BODY")

    async def run():
        session = EngineSession(tmp_path, tmp_path / "session.db")
        await session.start()
        session._llm = FakeProvider()
        queue = session.subscribe()
        await session.handle(StartSession(workspace=str(tmp_path)))
        session._loop.activate_skill("docs")
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        assert any(isinstance(item, SkillActivated) and item.name == "docs" for item in events)
        await session.aclose()

    asyncio.run(run())
