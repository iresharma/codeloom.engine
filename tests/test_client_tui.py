from __future__ import annotations

import pytest

pytest.importorskip("textual")
pytest.importorskip("rich")

from rich.console import Group
from rich.markdown import Markdown
from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import Select

from client_tui import (
    PROTOCOL_ALL,
    JudgeCard,
    ProtocolPanel,
    ToolsPanel,
    _chat_content,
    chat_render_markdown,
    chat_uses_markdown,
    format_judgement_markup,
)
from protocol.commands import StartSession
from protocol.events import JudgementMade, WarningOccurred


def test_assistant_and_engine_use_markdown():
    assert chat_uses_markdown("assistant")
    assert chat_uses_markdown("engine")
    assert chat_uses_markdown("assistant  history 1/3")
    assert not chat_uses_markdown("user")
    assert not chat_uses_markdown("user  history 1/3")


def test_open_fence_stays_literal():
    closed = "```python\nprint(1)\n```"
    assert chat_render_markdown("assistant", "# Hello\n\n- a")
    assert chat_render_markdown("assistant", closed)
    assert not chat_render_markdown("assistant", "```python\nprint(1)")
    assert not chat_render_markdown("user", "# Hello")
    assert not chat_render_markdown("assistant", "")


def test_chat_content_uses_rich_markdown():
    rendered = _chat_content("assistant", "# Title\n\nhello")
    assert isinstance(rendered, Group)
    assert any(isinstance(part, Markdown) for part in rendered.renderables)

    user = _chat_content("user", "**not bold**")
    assert isinstance(user, Group)
    assert all(not isinstance(part, Markdown) for part in user.renderables)
    assert any(isinstance(part, Text) and "**not bold**" in part.plain for part in user.renderables)

    streaming = _chat_content("assistant", "```\nnot yet")
    assert all(not isinstance(part, Markdown) for part in streaming.renderables)


class _ProtocolHarness(App):
    def compose(self) -> ComposeResult:
        yield ProtocolPanel(id="protocol")


def _log_text(panel: ProtocolPanel) -> str:
    log = panel._log()
    return "\n".join(strip.text for strip in log.lines)


async def test_protocol_panel_discovers_kinds_from_traffic():
    app = _ProtocolHarness()
    async with app.run_test():
        panel = app.query_one("#protocol", ProtocolPanel)
        panel.log_note("connected")
        panel.log_outbound(StartSession(workspace="/tmp/x"))
        panel.log_inbound(WarningOccurred(message="careful"), "warning: careful")
        panel.log_inbound(
            JudgementMade(
                tag="exec_approval",
                subject="git push --force",
                outcome="prompt",
                signals={"matches_user_request": 0.1},
                enforced=True,
                latency_ms=42,
            ),
            "judge exec_approval -> prompt",
        )

        assert set(panel._kinds) >= {
            PROTOCOL_ALL,
            "note",
            "StartSession",
            "WarningOccurred",
            "JudgementMade",
        }


async def test_protocol_panel_filter_hides_other_kinds():
    app = _ProtocolHarness()
    async with app.run_test() as pilot:
        panel = app.query_one("#protocol", ProtocolPanel)
        panel.log_note("connected")
        panel.log_outbound(StartSession(workspace="/tmp/x"))
        panel.log_inbound(
            JudgementMade(
                tag="exec_approval",
                subject="git push --force",
                outcome="prompt",
                signals={},
                enforced=True,
                latency_ms=42,
            ),
            "judge exec_approval -> prompt",
        )
        await pilot.pause()

        select = app.query_one("#protocol-filter", Select)
        select.value = "JudgementMade"
        await pilot.pause()

        text = _log_text(panel)
        assert "judge exec_approval" in text
        assert "StartSession" not in text
        assert "connected" not in text

        select.value = PROTOCOL_ALL
        await pilot.pause()
        text = _log_text(panel)
        assert "connected" in text
        assert "judge exec_approval" in text


async def test_protocol_panel_late_arriving_kind_keeps_current_filter():
    app = _ProtocolHarness()
    async with app.run_test() as pilot:
        panel = app.query_one("#protocol", ProtocolPanel)
        panel.log_note("connected")
        select = app.query_one("#protocol-filter", Select)
        select.value = "note"
        await pilot.pause()

        panel.log_outbound(StartSession(workspace="/tmp/x"))
        await pilot.pause()

        assert select.value == "note"


def _judgement(**overrides) -> JudgementMade:
    data = dict(
        tag="exec_approval",
        subject="git push --force",
        outcome="prompt",
        signals={"matches_user_request": 0.1},
        enforced=True,
        latency_ms=42,
    )
    data.update(overrides)
    return JudgementMade(**data)


def test_judgement_markup_is_colored_and_escapes_subject():
    text = format_judgement_markup(_judgement(subject="rm -rf [tmp]"))
    assert "judge" in text
    assert "prompt" in text
    assert "enforced" in text
    assert "rm -rf \\[tmp]" in text
    assert "matches_user_request=0.10" in text


async def test_protocol_panel_set_filter_adds_missing_kind():
    app = _ProtocolHarness()
    async with app.run_test() as pilot:
        panel = app.query_one("#protocol", ProtocolPanel)
        panel.log_note("connected")
        panel.set_filter("JudgementMade")
        await pilot.pause()

        text = _log_text(panel)
        assert "connected" not in text
        assert app.query_one("#protocol-filter", Select).value == "JudgementMade"

        panel.log_inbound(_judgement(), "ignored plaintext")
        await pilot.pause()
        text = _log_text(panel)
        assert "judge" in text
        assert "git push --force" in text


class _ToolsHarness(App):
    def compose(self) -> ComposeResult:
        yield ToolsPanel(id="tools")


async def test_tools_panel_pins_judgement_cards():
    app = _ToolsHarness()
    async with app.run_test():
        panel = app.query_one("#tools", ToolsPanel)
        panel.add_judgement(_judgement())
        panel.add_judgement(_judgement(tag="search_rerank", outcome="ranked", signals={}))

        cards = list(panel.query(JudgeCard))
        assert len(cards) == 2
        assert panel.border_title == "tools · 2 judged"
        assert cards[0].event.tag == "exec_approval"
        assert cards[1].event.tag == "search_rerank"
