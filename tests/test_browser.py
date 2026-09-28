from __future__ import annotations

import asyncio

import pytest
from unittest.mock import patch

from runtime.tools.browser import (
    ScreenshotResult,
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


@pytest.mark.asyncio
async def test_browser_screenshot_unavailable(tmp_path):
    with patch("runtime.tools.browser._state_for", return_value=None):
        result = await browser_screenshot(tmp_path)
        assert isinstance(result, ScreenshotResult)
        assert "unavailable" in result.text
        assert result.image is None
