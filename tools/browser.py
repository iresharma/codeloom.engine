from __future__ import annotations

from tools.base import ToolContext, tool
from runtime.tools import browser as impl


@tool(
    description="Open a URL in a headless browser (Playwright). Resets console/network logs.",
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "http or https URL."},
        },
        "required": ["url"],
    },
)
async def browser_open(url: str) -> str:
    return await impl.browser_open(url)


@tool(
    description="Return captured browser console messages since the last browser_open.",
    parameters={"type": "object", "properties": {}},
)
async def browser_console() -> str:
    return await impl.browser_console()


@tool(
    description=(
        "Screenshot the current page into .engine/debug/ and return the path. "
        "Does not send the image to the model."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Filename under .engine/debug/ (default shot.png).",
            }
        },
    },
)
async def browser_screenshot(ctx: ToolContext, name: str = "shot.png") -> str:
    return await impl.browser_screenshot(ctx.workspace, name)


@tool(
    description="Failed and 4xx/5xx network requests since the last browser_open.",
    parameters={"type": "object", "properties": {}},
)
async def browser_network() -> str:
    return await impl.browser_network()
