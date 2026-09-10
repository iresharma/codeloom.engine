from __future__ import annotations

from tools.base import tool
from runtime.tools.osv import osv_query as osv_query_impl


@tool(
    description=(
        "Query OSV.dev for known vulnerabilities. "
        "ecosystem: pypi, npm, crates, go, maven, nuget, rubygems. "
        "Empty version lists advisories for the package."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ecosystem": {
                "type": "string",
                "description": "pypi, npm, crates, go, maven, nuget, or rubygems.",
            },
            "package": {"type": "string", "description": "Package name."},
            "version": {
                "type": "string",
                "description": "Specific version. Empty means package-wide.",
            },
        },
        "required": ["ecosystem", "package"],
    },
)
def osv_query(ecosystem: str, package: str, version: str = "") -> str:
    return osv_query_impl(ecosystem, package, version=version)
