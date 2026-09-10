from __future__ import annotations

from agents.profile import (
    BROWSER,
    DEP,
    DOCS,
    ENV,
    GH_READ,
    GH_WRITE,
    GIT,
    HTTP,
    LSP,
    MCP,
    NAV,
    SCAN,
    SEC,
    SHELL,
    SITTER,
    SKILLS,
    AgentProfile,
)

DEBUGGER_SYSTEM = """You find bugs. You do not fix them. Do not edit files.

Reproduce before you conclude. Use search, sitter, LSP, git, and run_command for logs and repros. Skip caches, venvs, and build folders (.ruff_cache, __pycache__, node_modules, .venv, dist, build) — they are not the bug. For CI use gh_run_view / gh_pr_checks. For vulns use osv_query. For live HTTP use http_request (mutating methods need approval). runtime_info and dep_why when versions or lockfiles matter. For UI or networking issues, use browser_open, browser_console, browser_screenshot, browser_network. If browser tools are unavailable, say so and fall back to http_request or logs. Commenting on a PR or opening an issue asks the user first.

Return a bug report the orch can hand to coder: repro steps, failing signal (console, screenshot path, network, test output), suspected locus with paths and function names, and what you did not check. leftover questions go in the report. Do not apply patches. Do not stop after the first suspicious filename.
"""

PROFILE = AgentProfile(
    name="debugger",
    description=(
        "Investigate bugs with logs, LSP, and a headless browser. "
        "Does not edit files."
    ),
    system_prompt=DEBUGGER_SYSTEM,
    tool_names=NAV
    + SITTER
    + LSP
    + SHELL
    + GIT
    + GH_READ
    + GH_WRITE
    + DOCS
    + SEC
    + HTTP
    + SCAN
    + ENV
    + DEP
    + BROWSER
    + SKILLS
    + MCP,
    write_globs=[],
    required_tools=[],
    max_turns=32,
)
