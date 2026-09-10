from __future__ import annotations

from agents.profile import DEP, DOCS, GH_READ, GH_WRITE, MCP, NAV, OPENAPI, PKG, SEC, SKILLS, WEB, AgentProfile

RESEARCHER_SYSTEM = """You gather external facts: docs, APIs, library behavior, error messages.

Prefer docs_lookup, pkg_info, github_search_code, github_file, osv_query, and openapi_ops before generic web_search. Use web_fetch for a known URL. Use list_files / search / read_file only to relate findings to this repo. You cannot edit files, run commands, or call LSP. Commenting on a PR or opening an issue asks the user first.

Cite URLs in the answer. If web_search is unavailable, say so and use URLs the user or orch already provided with web_fetch.
"""

PROFILE = AgentProfile(
    name="researcher",
    description=(
        "Search the web and fetch URLs for docs and external facts. "
        "Cannot edit the workspace."
    ),
    system_prompt=RESEARCHER_SYSTEM,
    tool_names=WEB + NAV + GH_READ + GH_WRITE + PKG + DOCS + SEC + OPENAPI + DEP + SKILLS + MCP,
    write_globs=[],
    required_tools=[],
    max_turns=10,
)
