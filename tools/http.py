from __future__ import annotations

from tools.base import ToolContext, tool
from runtime.tools.approve import require_approval
from runtime.tools.httpx import SAFE_METHODS, http_request as http_request_impl
from runtime.tools.httpx import openapi_ops as openapi_ops_impl


@tool(
    description=(
        "HTTP request. http/https only. GET/HEAD are free; "
        "POST/PUT/PATCH/DELETE ask the user. Cap 50k."
    ),
    parameters={
        "type": "object",
        "properties": {
            "method": {
                "type": "string",
                "description": "GET, HEAD, POST, PUT, PATCH, or DELETE.",
            },
            "url": {"type": "string", "description": "http or https URL."},
            "headers": {
                "type": "string",
                "description": "Optional headers: JSON object or Header: value lines.",
            },
            "body": {"type": "string", "description": "Optional request body."},
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default 20).",
            },
        },
        "required": ["method", "url"],
    },
)
async def http_request(
    ctx: ToolContext,
    method: str,
    url: str,
    headers: str = "",
    body: str = "",
    timeout: int = 20,
) -> str:
    if (method or "").upper() not in SAFE_METHODS:
        denied = await require_approval(ctx, f"Allow HTTP {method.upper()} {url}?")
        if denied:
            return denied
    return http_request_impl(
        method, url, headers=headers, body=body, timeout=timeout
    )


@tool(
    description="List operations from an OpenAPI/Swagger URL (METHOD path — summary).",
    parameters={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "http or https URL of a swagger/openapi document.",
            }
        },
        "required": ["url"],
    },
)
def openapi_ops(url: str) -> str:
    return openapi_ops_impl(url)
