from __future__ import annotations

from tools.base import ToolContext, ToolResult, tool
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
async def browser_open(ctx: ToolContext, url: str) -> str:
    return await impl.browser_open(url, agent_id=ctx.agent_id)


@tool(
    description="Return captured browser console messages since the last browser_open.",
    parameters={"type": "object", "properties": {}},
)
async def browser_console(ctx: ToolContext) -> str:
    return await impl.browser_console(agent_id=ctx.agent_id)


@tool(
    description=(
        "Screenshot the current page into .engine/debug/ and send the image "
        "to the model and the client. Viewport JPEG by default; set full_page "
        "for the whole page."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Filename under .engine/debug/ (default shot.jpg).",
            },
            "full_page": {
                "type": "boolean",
                "description": "Capture the full scrollable page instead of the viewport.",
            },
        },
    },
)
async def browser_screenshot(
    ctx: ToolContext, name: str = "shot.jpg", full_page: bool = False
) -> str | ToolResult:
    result = await impl.browser_screenshot(
        ctx.workspace, name, full_page=full_page, agent_id=ctx.agent_id
    )
    if result.image or result.wire:
        return ToolResult(
            text=result.text,
            image=result.image,
            image_mime="image/jpeg",
            wire_image=result.wire,
        )
    return result.text


@tool(
    description="Failed and 4xx/5xx network requests since the last browser_open.",
    parameters={"type": "object", "properties": {}},
)
async def browser_network(ctx: ToolContext) -> str:
    return await impl.browser_network(agent_id=ctx.agent_id)
