# Implementation plans

Historical design plans for this engine, in the order they were built.
How-to guides for extending the running system live in `docs/` (`adding-a-*.md`).

1. [Engine class design](engine-class-design.md) — core vs protocol vs transport
2. [IPC, session, snapshot](ipc-session-slice.md) — NDJSON Unix socket, SQLite snapshot, dummy REPL
3. [Dummy debug client](dummy-debug-client.md) — interactive REPL harness
4. [Protocol extendability](protocol-extendability.md) — `@command` / `@event` / `@handles`
5. [Tree-sitter and LSP tools](sitter-and-lsp-tools.md)
6. [Write-side edit tools](write-side-edit-tools.md) — edit funnel, syntax gate, undo journal
7. [Interpreter and dependency floor](interpreter-dependency-floor.md)
8. [Agent runtime foundation](agent-runtime-foundation.md) — streaming, abort, prompts, compaction
9. [Orchestrator and subagents](orch-subagent-model.md)
10. [Dummy client TUI](dummy-client-tui.md)
11. [MCP and skills](mcp-and-skills.md)
12. [Developer GitHub tools](developer-github-tools.md)
13. [Compaction handoff fixes](compaction-handoff-fixes.md)
14. [Researcher surveys](fix-researcher-surveys.md)
15. [Long-term memory](long-term-memory.md) — structured workspace notes instead of context.md
16. [Cost controls](cost-controls.md) — prompt cache, append-only children, spawn-once, Haiku ask/tester
