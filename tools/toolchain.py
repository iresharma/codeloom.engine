from __future__ import annotations

from runtime.tools.shell import DEFAULT_TIMEOUT
from runtime.tools.toolchain import run_toolchain
from tools.base import ToolContext, tool


@tool(
    description=(
        "Run this repo's package manager or language toolchain. "
        "verb: which, install, add, remove, run, typecheck, test, build. "
        "add/remove are coder-only. typecheck never emits JS. "
        "Do not hand-edit package.json, lockfiles, tsconfig, go.mod, or pyproject."
    ),
    parameters={
        "type": "object",
        "properties": {
            "verb": {
                "type": "string",
                "description": (
                    "which, install, add, remove, run, typecheck, test, or build."
                ),
            },
            "package": {
                "type": "string",
                "description": "Package name for add or remove.",
            },
            "script": {
                "type": "string",
                "description": "package.json script name for run.",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default 120, max 600).",
            },
        },
        "required": ["verb"],
    },
)
async def toolchain(
    ctx: ToolContext,
    verb: str,
    package: str = "",
    script: str = "",
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    return await run_toolchain(
        ctx, verb, package=package, script=script, timeout=timeout
    )
