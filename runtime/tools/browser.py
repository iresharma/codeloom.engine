from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_MISSING = "error: browser tools unavailable (install playwright and run playwright install chromium)"
MAX_IMAGE_BYTES = 1_500_000
VIEWPORT = {"width": 1280, "height": 800}

_lock = asyncio.Lock()
_play = None
_browser = None
_context = None
_launch_error: str | None = None
_pages: dict[str, _PageState] = {}


@dataclass
class ScreenshotResult:
    text: str
    image: bytes | None = None


@dataclass
class _PageState:
    page: Any
    console: list[str] = field(default_factory=list)
    network: list[str] = field(default_factory=list)


def _in_container() -> bool:
    if os.environ.get("container") == "docker":
        return True
    return os.path.exists("/.dockerenv")


def _launch_args() -> list[str]:
    # Docker's /dev/shm is 64MB. Chromium's renderer dies on navigation
    # without this, and a crashed page then fails every later load.
    args = ["--disable-dev-shm-usage", "--disable-gpu"]
    root = False
    try:
        root = os.geteuid() == 0
    except AttributeError:
        pass
    if root or _in_container():
        args.extend(["--no-sandbox", "--disable-setuid-sandbox"])
    return args


def _missing() -> str:
    if _launch_error:
        return f"error: browser tools unavailable ({_launch_error})"
    return _MISSING


def _page_dead(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(
        token in text
        for token in ("crash", "has been closed", "target closed", "browser has been closed")
    )


async def _ensure_browser():
    global _play, _browser, _context, _launch_error
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return None
    if _context is not None:
        return _context
    try:
        _play = await async_playwright().start()
        _browser = await _play.chromium.launch(headless=True, args=_launch_args())
        _context = await _browser.new_context(viewport=VIEWPORT)
    except Exception as exc:  # noqa: BLE001
        _launch_error = str(exc)
        _play = None
        _browser = None
        _context = None
        return None
    _launch_error = None
    return _context


def _attach(state: _PageState) -> None:
    page = state.page
    page.on("console", lambda msg: state.console.append(f"{msg.type}: {msg.text}"))
    page.on(
        "requestfailed",
        lambda req: state.network.append(f"FAIL {req.method} {req.url} {req.failure}"),
    )

    def on_response(res) -> None:
        if res.status >= 400:
            state.network.append(f"{res.status} {res.request.method} {res.url}")

    page.on("response", on_response)


async def _state_for(agent_id: str) -> _PageState | None:
    context = await _ensure_browser()
    if context is None:
        return None
    key = agent_id or "default"
    state = _pages.get(key)
    if state is not None:
        closed = getattr(state.page, "is_closed", None)
        if closed is None or not closed():
            return state
        _pages.pop(key, None)
    page = await context.new_page()
    state = _PageState(page=page)
    _attach(state)
    _pages[key] = state
    return state


async def browser_open(url: str, agent_id: str = "") -> str:
    async with _lock:
        last = ""
        for attempt in (1, 2):
            state = await _state_for(agent_id)
            if state is None:
                return _missing()
            state.console.clear()
            state.network.clear()
            try:
                await state.page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                return f"opened {state.page.url} title={await state.page.title()!r}"
            except Exception as exc:  # noqa: BLE001
                last = str(exc)
                # A renderer crash leaves the Page dead. Reusing it makes
                # every later navigation fail the same way.
                if attempt == 1 and _page_dead(exc):
                    await _shutdown_locked()
                    continue
                return f"error: {exc}"
        return f"error: {last}"


async def browser_console(agent_id: str = "") -> str:
    async with _lock:
        state = await _state_for(agent_id)
        if state is None:
            return _missing()
        if not state.console:
            return "(no console messages)"
        return "\n".join(state.console[-80:])


async def browser_screenshot(
    workspace: Path,
    name: str = "shot.jpg",
    full_page: bool = False,
    agent_id: str = "",
) -> ScreenshotResult:
    async with _lock:
        state = await _state_for(agent_id)
        if state is None:
            return ScreenshotResult(_missing())
        folder = workspace / ".engine" / "debug"
        folder.mkdir(parents=True, exist_ok=True)
        stem = Path(name).name or "shot.jpg"
        stem = Path(stem).stem or "shot"
        target = folder / f"{stem}.jpg"
        try:
            image = await state.page.screenshot(
                type="jpeg", quality=60, full_page=full_page
            )
        except Exception as exc:  # noqa: BLE001
            if _page_dead(exc):
                await _shutdown_locked()
            return ScreenshotResult(f"error: {exc}")
        target.write_bytes(image)
        rel = target.relative_to(workspace).as_posix()
        if len(image) > MAX_IMAGE_BYTES:
            return ScreenshotResult(
                f"saved {rel} ({len(image)} bytes; image omitted, too large)"
            )
        return ScreenshotResult(f"saved {rel}", image)


async def browser_network(agent_id: str = "") -> str:
    async with _lock:
        state = await _state_for(agent_id)
        if state is None:
            return _missing()
        if not state.network:
            return "(no failed or 4xx/5xx requests)"
        return "\n".join(state.network[-80:])


async def close_agent(agent_id: str) -> None:
    async with _lock:
        state = _pages.pop(agent_id or "default", None)
        if state is None:
            return
        with suppress(Exception):
            await state.page.close()
        if not _pages:
            await _shutdown_locked()


async def shutdown() -> None:
    async with _lock:
        await _shutdown_locked()


async def _shutdown_locked() -> None:
    global _play, _browser, _context, _launch_error
    for state in list(_pages.values()):
        with suppress(Exception):
            await state.page.close()
    _pages.clear()
    if _context is not None:
        with suppress(Exception):
            await _context.close()
    if _browser is not None:
        with suppress(Exception):
            await _browser.close()
    if _play is not None:
        with suppress(Exception):
            await _play.stop()
    _play = None
    _browser = None
    _context = None
    _launch_error = None
