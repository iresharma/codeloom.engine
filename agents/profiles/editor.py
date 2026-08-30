from agents.profile import AgentProfile

PROFILE = AgentProfile(
    name="editor",
    description="Make code changes to complete a focused editing task.",
    system_prompt=(
        "You are an editor subagent. Make the requested code changes. "
        "Read before you write. Prefer edit_file for surgical updates. "
        "Keep the change scoped to the task. When finished, summarize files changed."
    ),
    tool_names=[
        "list_tree",
        "read_file",
        "write_file",
        "edit_file",
        "search_files",
        "parse_file",
        "query_tree",
        "get_node_at",
        "list_symbols",
        "get_diagnostics",
        "go_to_definition",
        "find_references",
        "hover",
        "document_symbols",
        "run_command",
    ],
    max_turns=40,
)
