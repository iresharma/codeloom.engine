from __future__ import annotations

from tools.base import tool
from runtime.tools.envinfo import runtime_info as runtime_info_impl


@tool(
    description="Local versions of python, node, go, git, gh, and rg if installed.",
    parameters={"type": "object", "properties": {}},
)
def runtime_info() -> str:
    return runtime_info_impl()
