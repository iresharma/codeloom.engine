from __future__ import annotations

import asyncio

from runtime.tools.browser import browser_console, browser_open


def test_browser_missing_playwright():
    async def run():
        opened = await browser_open("https://example.com")
        console = await browser_console()
        return opened, console

    opened, console = asyncio.run(run())
    assert opened.startswith("error:") or opened.startswith("opened")
    if "unavailable" in opened:
        assert "unavailable" in console
