from __future__ import annotations

from tools.base import tool
from runtime.tools.pkg import pkg_info as pkg_info_impl


@tool(
    description=(
        "Look up a package on a public registry. "
        "ecosystem: pypi, npm, crates, go, maven, nuget, rubygems. "
        "No download."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ecosystem": {
                "type": "string",
                "description": "pypi, npm, crates, go, maven, nuget, or rubygems.",
            },
            "name": {
                "type": "string",
                "description": "Package name (Maven: group:artifact).",
            },
        },
        "required": ["ecosystem", "name"],
    },
)
def pkg_info(ecosystem: str, name: str) -> str:
    return pkg_info_impl(ecosystem, name)
