from __future__ import annotations

import pytest

pytest.importorskip("textual")
pytest.importorskip("rich")

from rich.console import Group
from rich.markdown import Markdown
from rich.text import Text

from client_tui import _chat_content, chat_render_markdown, chat_uses_markdown


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
