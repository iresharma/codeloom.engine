from __future__ import annotations

import asyncio

import pytest
from unittest.mock import patch

from runtime.tools.browser import (
    ScreenshotResult,
    _PageState,
    browser_console,
    browser_network,
    browser_open,
    browser_screenshot,
)


def test_browser_missing_playwright():
    async def run():
        opened = await browser_open("https://example.com")
        console = await browser_console()
        return opened, console

    opened, console = asyncio.run(run())
    assert opened.startswith("error:") or opened.startswith("opened")
    if "unavailable" in opened:
        assert "unavailable" in console


@pytest.mark.asyncio
async def test_browser_open_unavailable():
    with patch("runtime.tools.browser._state_for", return_value=None):
        result = await browser_open("https://example.com")
        assert "unavailable" in result


@pytest.mark.asyncio
async def test_browser_console_unavailable():
    with patch("runtime.tools.browser._state_for", return_value=None):
        result = await browser_console()
        assert "unavailable" in result


@pytest.mark.asyncio
async def test_browser_network_unavailable():
    with patch("runtime.tools.browser._state_for", return_value=None):
        result = await browser_network()
        assert "unavailable" in result


class _Page:
    def __init__(self, fail: str | None = None) -> None:
        self.url = "https://example.com/"
        self.fail = fail
        self.calls = 0

    async def goto(self, url: str, wait_until: str, timeout: int) -> None:
        self.calls += 1
        if self.fail and self.calls == 1:
            raise RuntimeError(self.fail)

    async def title(self) -> str:
        return "Example"

    def is_closed(self) -> bool:
        return False


@pytest.mark.asyncio
async def test_browser_open_relaunches_after_page_crash():
    page = _Page("Page crashed")
    shutdowns: list[bool] = []

    async def state_for(_agent_id: str):
        return _PageState(page=page)

    async def shutdown_locked() -> None:
        shutdowns.append(True)

    with (
        patch("runtime.tools.browser._state_for", side_effect=state_for),
        patch("runtime.tools.browser._shutdown_locked", side_effect=shutdown_locked),
    ):
        result = await browser_open("https://example.com")
    assert result.startswith("opened")
    assert page.calls == 2
    assert shutdowns == [True]


@pytest.mark.asyncio
async def test_browser_open_does_not_retry_navigation_errors():
    page = _Page("net::ERR_CONNECTION_REFUSED")
    shutdowns: list[bool] = []

    async def state_for(_agent_id: str):
        return _PageState(page=page)

    async def shutdown_locked() -> None:
        shutdowns.append(True)

    with (
        patch("runtime.tools.browser._state_for", side_effect=state_for),
        patch("runtime.tools.browser._shutdown_locked", side_effect=shutdown_locked),
    ):
        result = await browser_open("https://example.com")
    assert result.startswith("error:")
    assert "ERR_CONNECTION_REFUSED" in result
    assert page.calls == 1
    assert shutdowns == []


@pytest.mark.asyncio
async def test_browser_screenshot_unavailable(tmp_path):
    with patch("runtime.tools.browser._state_for", return_value=None):
        result = await browser_screenshot(tmp_path)
        assert isinstance(result, ScreenshotResult)
        assert "unavailable" in result.text
        assert result.image is None
