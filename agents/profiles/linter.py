from agents.profile import AgentProfile

PROFILE = AgentProfile(
    name="linter",
    description="Find issues in the codebase. Do not edit files.",
    system_prompt=(
        "You are a linter subagent. Find bugs, style issues, and missing tests. "
        "You may read files, inspect syntax, and run linters via run_command. "
        "Do not edit files. Return a prioritized list of findings with paths."
    ),
    tool_names=[
        "list_tree",
        "read_file",
        "search_files",
        "parse_file",
        "query_tree",
        "get_node_at",
        "list_symbols",
        "get_diagnostics",
        "document_symbols",
        "run_command",
    ],
    max_turns=25,
)
