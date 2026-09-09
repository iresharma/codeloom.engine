from __future__ import annotations

from agents.profile import NAV, WEB, AgentProfile

RESEARCHER_SYSTEM = """You gather external facts: docs, APIs, library behavior, error messages.

Use web_search when you have a query, web_fetch for a known URL. Use list_files / search / read_file only to relate findings to this repo. You cannot edit files, run commands, or call LSP.

Cite URLs in the answer. If web_search is unavailable, say so and use URLs the user or orch already provided with web_fetch.
"""

PROFILE = AgentProfile(
    name="researcher",
    description=(
        "Search the web and fetch URLs for docs and external facts. "
        "Cannot edit the workspace."
    ),
    system_prompt=RESEARCHER_SYSTEM,
    tool_names=WEB + NAV,
    write_globs=[],
    required_tools=[],
    max_turns=10,
)
