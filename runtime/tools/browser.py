from __future__ import annotations

import asyncio
from pathlib import Path

_MISSING = "error: browser tools unavailable (install playwright and run playwright install chromium)"

_lock = asyncio.Lock()
_play = None
_page = None
_console: list[str] = []
_network: list[str] = []


async def _ensure():
    global _play, _page
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return None
    if _page is None:
        _play = await async_playwright().start()
        browser = await _play.chromium.launch(headless=True)
        context = await browser.new_context()
        _page = await context.new_page()
        _page.on("console", lambda msg: _console.append(f"{msg.type}: {msg.text}"))
        _page.on(
            "requestfailed",
            lambda req: _network.append(
                f"FAIL {req.method} {req.url} {req.failure}"
            ),
        )

        def on_response(res) -> None:
            if res.status >= 400:
                _network.append(f"{res.status} {res.request.method} {res.url}")

        _page.on("response", on_response)
    return _page


async def browser_open(url: str) -> str:
    async with _lock:
        page = await _ensure()
        if page is None:
            return _MISSING
        _console.clear()
        _network.clear()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        except Exception as exc:  # noqa: BLE001
            return f"error: {exc}"
        return f"opened {page.url} title={await page.title()!r}"


async def browser_console() -> str:
    async with _lock:
        if await _ensure() is None:
            return _MISSING
        if not _console:
            return "(no console messages)"
        return "\n".join(_console[-80:])


async def browser_screenshot(workspace: Path, name: str = "shot.png") -> str:
    async with _lock:
        page = await _ensure()
        if page is None:
            return _MISSING
        folder = workspace / ".engine" / "debug"
        folder.mkdir(parents=True, exist_ok=True)
        safe = Path(name).name or "shot.png"
        if not safe.lower().endswith(".png"):
            safe += ".png"
        target = folder / safe
        try:
            await page.screenshot(path=str(target), full_page=True)
        except Exception as exc:  # noqa: BLE001
            return f"error: {exc}"
        rel = target.relative_to(workspace).as_posix()
        return f"saved {rel}"


async def browser_network() -> str:
    async with _lock:
        if await _ensure() is None:
            return _MISSING
        if not _network:
            return "(no failed or 4xx/5xx requests)"
        return "\n".join(_network[-80:])
