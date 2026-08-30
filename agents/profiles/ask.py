from agents.profile import AgentProfile

PROFILE = AgentProfile(
    name="ask",
    description="Read-only Q&A about the repository. Does not edit files.",
    system_prompt=(
        "You are an ask subagent. Answer questions about the codebase using "
        "read-only tools. Do not edit files or run mutating commands. "
        "Cite paths and be concise. When you are done, write a clear answer."
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
        "go_to_definition",
        "find_references",
        "hover",
        "document_symbols",
    ],
    max_turns=20,
)
