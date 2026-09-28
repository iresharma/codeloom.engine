# Tools

## Discovery and reload

There is no registration list. `discover_tools()` walks the `tools/` package
with `pkgutil`, imports and reloads every module except `base.py`,
`registry.py`, and anything starting with `_`, then collects any object
carrying an `_engine_tool` attribute.

Import failures are captured as registry errors and surfaced to the client as
`ErrorOccurred` rather than crashing the session, so one broken tool module
does not take down the engine. Duplicate tool names are rejected the same way.

Because discovery runs on every `StartSession` and uses `importlib.reload`, you
can edit a tool and pick it up by restarting the session — no server restart.

## The full tool catalogue

64 tools across navigation, tree-sitter, LSP, editing, execution, git, GitHub, docs, HTTP, browser, and memory.

**Navigation** — no language server needed.

| Tool | Purpose |
|---|---|
| `list_files` | Every workspace-relative path, one per line. |
| `read_file` | A numbered line window. Defaults to 200 lines from offset 1, capped at 400. Marks the file as read. |
| `search` | ripgrep across the workspace. Returns `path:line:text`. Default 80 matches, hard cap 200. |

**Tree-sitter** — instant, no server, works on partially broken files.

| Tool | Purpose |
|---|---|
| `list_symbols` | Outline of functions, classes, methods, types, imports. |
| `find_symbol` | One named definition's source plus the 1-based line/character of its name — the coordinate handoff into the LSP tools. |
| `get_node_at` | The node at a position: type, name, parent, enclosing definition, named children. |
| `query_tree` | A tree-sitter query. Presets: `imports`, `functions`, `classes`, `methods`, `calls`. Capped at 80 captures. |
| `parse_file` | Compact nested syntax tree with line ranges. Capped at 200 nodes and depth 8. |

**Language server** — real types, cross-file truth.

| Tool | Purpose |
|---|---|
| `goto_definition` | Resolve a symbol at a position. Understands imports and types. Max 20 locations. |
| `find_references` | Every usage across the indexed workspace. Max 50. |
| `hover` | Type signature and documentation. |
| `get_diagnostics` | Compiler and type-checker errors, warnings, hints. |
| `document_symbols` | Server-side outline with `SymbolKind`, catching interfaces and enums tree-sitter may miss. Max 200. |
| `rename_symbol` | Server-computed rename applied across every affected file as one undoable batch. |

**Text editing.**

| Tool | Purpose |
|---|---|
| `str_replace` | Replace one unique exact substring. Zero or multiple matches fail rather than guess. |
| `replace_lines` | Replace an inclusive 1-based line range, mirroring the `read_file` window. |
| `insert_at_line` | Insert before a 1-based line, or at `end+1` to append. |
| `create_file` | Create a new file. Fails if the path exists. Creates parent directories. |

**Structural editing.**

| Tool | Purpose |
|---|---|
| `replace_symbol` | Replace a whole function/class/type by name via tree-sitter. Robust to whitespace differences. |
| `insert_after_imports` | Insert after the last import block, or at the top if there are none. |
| `apply_patch` | Apply a unified diff. Line-number drift up to 5 lines is tolerated; any failing hunk rejects the entire patch. |

**Execution.**

| Tool | Purpose |
|---|---|
| `run_command` | Shell command in the workspace. No TTY. Default 120s timeout. Non-zero exit is information. |

**History.**

| Tool | Purpose |
|---|---|
| `undo_edit` | Revert the last edit batch, including multi-file renames. |
| `list_edits` | Recent edits in this session with their diffs. Default 20. |

**Git (LLM-facing).** The TUI git panel still uses `RequestGit`; these are for agents.

| Tool | Purpose |
|---|---|
| `git_status` | Branch, dirty flag, staged/unstaged/untracked paths. |
| `git_diff` | Worktree or staged (cached) diff. |
| `git_log` | Recent commits (`hash subject`). Optional path filter. |
| `git_show` | One revision: metadata, stat, clipped patch. |
| `git_blame` | Blame a file, optional 1-based line window. |
| `git_range` | Commits and diffstat for `base...head`. |

**GitHub.** Requires `gh` (authenticated). Read tools go to reviewer/researcher/debugger. Writes ask the user. `gh_pr_create` exists but is not given to any profile — worktree settle still opens writer PRs.

| Tool | Purpose |
|---|---|
| `gh_pr_list` / `gh_pr_view` / `gh_pr_comments` / `gh_pr_checks` | PRs, conversation, CI. |
| `gh_issue_list` / `gh_issue_view` | Issues. |
| `gh_run_list` / `gh_run_view` | Actions runs plus a clipped failed log. |
| `gh_release_list` / `gh_release_view` | Release notes / changelog. |
| `github_compare` | Ahead/behind, commits, files between two refs. |
| `github_search_code` | GitHub code search. `this_repo` scopes to the workspace remote. |
| `github_file` | Raw file from `owner/name` @ ref. Default 12k char window (hard cap 50k); pass `offset` to page. Directories tell you to use `github_tree`. |
| `github_repo` | Repo metadata: description, default branch, language, license, topics, stars. |
| `github_tree` | Files and dirs at a path (optional recursive, capped). Skips vendor/cache dirs. |
| `gh_pr_comment` / `gh_issue_create` | Approval-gated writes (researcher, debugger). |
| `gh_pr_create` | Implemented, unassigned. Settle still owns writer PRs. |

**Docs, packages, advisories.**

| Tool | Purpose |
|---|---|
| `pkg_info` | Registry metadata: pypi, npm, crates, go, maven, nuget, rubygems. |
| `docs_lookup` | Official docs: mdn, pypi, npm, crates, go. |
| `tldr` | CLI cheat sheet from tldr-pages. |
| `osv_query` | OSV.dev vulnerabilities for a package/version. |
| `dep_why` | `npm ls` / `go mod why` / `pip show` / `cargo tree`. |

**HTTP.**

| Tool | Purpose |
|---|---|
| `http_request` | http/https only. GET/HEAD free; other methods ask the user. 50k cap. |
| `openapi_ops` | List METHOD path — summary from a Swagger/OpenAPI URL. |

**Repo hygiene.**

| Tool | Purpose |
|---|---|
| `todo_scan` | TODO/FIXME/XXX/HACK as `path:line:text`. Skips `.git` / `node_modules`. |
| `runtime_info` | Local python, node, go, git, gh, rg versions. |

**Web.** Used by the `researcher` personality. Survey a GitHub repo with `github_repo` / `github_tree` / `github_file` — do not fetch GitHub HTML.

| Tool | Purpose |
|---|---|
| `web_fetch` | HTTP GET. HTML becomes markdown (main/article, chrome dropped). `github.com` URLs are refused. SPA pages hint at debugger `browser_open`. |
| `web_search` | Brave Search if `BRAVE_API_KEY` is set; otherwise an error. |

**Memory.** Every personality (and the orch) can record lasting workspace facts. `remember` upserts a file note (`purpose` / `entry_points` / `constraints`) or a decision. Ask, coder, and researcher briefings are also ingested automatically on finish. Reads and edits only update `seen_sha` on files that already have a note. Stale file notes are flagged when the on-disk hash no longer matches.

| Tool | Purpose |
|---|---|
| `remember` | Upsert a file blurb (`section=files` + `path`, optional `purpose` / `entry_points` / `constraints`) or append an engineering / product / CI/CD / other decision. |

**Skills.** Every personality (and the orch) can load a `SKILL.md` body.

| Tool | Purpose |
|---|---|
| `activate_skill` | Load one skill body into this agent. |
| `read_skill` | Read a file inside that skill directory only. |

**MCP.** Live tools from configured servers, namespaced `mcp_{server}_{tool}`.
Default profiles: `researcher`, `debugger`. Also `mcp_list_resources` and
`mcp_read_resource` (`file://` rejected).

**Browser.** Used by `coder`, `tester`, `reviewer`, and `debugger`. Playwright and Chromium are required; a missing binary returns `error: browser tools unavailable`. Each agent has its own page on one shared Chromium.

| Tool | Purpose |
|---|---|
| `browser_open` | Headless Chromium, resets that agent's console/network logs. |
| `browser_console` | Console messages since last open. |
| `browser_screenshot` | Viewport JPEG under `.engine/debug/`; the image is sent to the model. |
| `browser_network` | Failed and 4xx/5xx requests since last open. |

**Server.** Used by `coder`, `tester`, and `debugger` — not `reviewer`. Leaves a process running so `browser_open` and `http_request` can hit `http://127.0.0.1:<port>`.

| Tool | Purpose |
|---|---|
| `start_server` | Spawn a command in the worktree, wait until the port accepts, then return. One server per worktree. |
| `stop_server` | Kill that worktree's server. |
| `server_logs` | Recent stdout/stderr from the running server. |

## Writing a new tool

Drop a module in `tools/` and decorate a function. Nothing else.

```python
from tools.base import ToolContext, tool


@tool(description="Count lines in a workspace file.")
def count_lines(ctx: ToolContext, path: str) -> str:
    _rel, text = read_text(ctx.workspace, path)
    return str(len(text.splitlines()))
```

The JSON schema is inferred from the signature. Type hints map to JSON types,
`Optional[X]` unwraps to `X`, parameters without defaults become required, and
`ctx` / `context` / `self` are excluded. Pass `parameters={...}` explicitly when
you want richer descriptions per field, which every built-in tool does.

At call time only arguments matching the signature are forwarded — a model
hallucinating an extra keyword gets it dropped rather than causing a
`TypeError`. Sync and async functions both work. A `None` return becomes an
empty string, and exceptions become `error: {message}` strings so a tool crash
becomes something the model can read and react to instead of an aborted turn.

For anything that writes, do not touch the filesystem directly. Write a pure
mutation and hand it to the funnel:

```python
from runtime.tools.edits import apply_edit


@tool(description="Strip trailing whitespace from a file.")
async def strip_trailing(ctx: ToolContext, path: str) -> str:
    def mutate(src):
        return "\n".join(line.rstrip() for line in src.text.splitlines())

    return await apply_edit(ctx, path, mutate, "strip_trailing")
```

That single call buys the staleness check, the write guard, newline and
encoding preservation, the syntax gate, an atomic replace, a journal entry, the
`FileEdited` event, and undo support.
