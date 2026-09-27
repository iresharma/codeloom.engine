from __future__ import annotations

import asyncio

from runtime.tools.browser import browser_console, browser_open
from unittest.mock import patch
import pytest
from runtime.tools.browser import browser_network, browser_screenshot


def test_browser_missing_playwright():
    async def run():
        opened = await browser_open("https://example.com")
        console = await browser_console()
        return opened, console

    opened, console = asyncio.run(run())
    assert opened.startswith("error:") or opened.startswith("opened")
    if "unavailable" in opened:
        assert "unavailable" in console


# ============================================================================
# Tests for runtime/tools/browser.py
# ============================================================================

@pytest.mark.asyncio
async def test_browser_open_unavailable():
    """Test browser open when playwright unavailable."""
    with patch("runtime.tools.browser._ensure", return_value=None):
        result = await browser_open("https://example.com")
        assert "unavailable" in result


@pytest.mark.asyncio
async def test_browser_console_unavailable():
    """Test console when playwright unavailable."""
    with patch("runtime.tools.browser._ensure", return_value=None):
        result = await browser_console()
        assert "unavailable" in result


@pytest.mark.asyncio
async def test_browser_network_unavailable():
    """Test network when playwright unavailable."""
    with patch("runtime.tools.browser._ensure", return_value=None):
        result = await browser_network()
        assert "unavailable" in result


@pytest.mark.asyncio
async def test_browser_screenshot_unavailable(tmp_path):
    """Test screenshot when playwright unavailable."""
    with patch("runtime.tools.browser._ensure", return_value=None):
        result = await browser_screenshot(tmp_path)
        assert "unavailable" in result
