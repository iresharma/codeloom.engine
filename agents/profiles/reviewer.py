from agents.profile import AgentProfile

PROFILE = AgentProfile(
    name="reviewer",
    description="Review diffs and propose a verdict. Does not edit files.",
    system_prompt=(
        "You are a reviewer subagent. Inspect git diffs and related files. "
        "Do not edit files. Return a verdict (approve, comment, request changes) "
        "with concrete notes referenced by path."
    ),
    tool_names=[
        "list_tree",
        "read_file",
        "search_files",
        "git_status",
        "git_diff",
        "git_log",
        "git_branch",
        "parse_file",
        "query_tree",
        "get_node_at",
        "list_symbols",
        "get_diagnostics",
        "go_to_definition",
        "find_references",
        "hover",
        "document_symbols",
    ],
    max_turns=25,
)
