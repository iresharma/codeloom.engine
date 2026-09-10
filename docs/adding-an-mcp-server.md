# Adding an MCP server

The engine is an MCP **client**. Users add servers in JSON; agents call the
bridged tools. Clients never speak MCP. They may render `McpServersUpdated`
and `McpAuthRequired`.

## Config

Canonical file: `{workspace}/.engine/mcp.json`. `.cursor/mcp.json` is merged
(engine wins on name clash). The first time Cursor servers appear, the engine
emits a warning and asks to confirm. The answer is stored in
`.engine/mcp-trust.json`.

```json
{
  "mcpServers": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": { "GITHUB_TOKEN": "${env:GITHUB_TOKEN}" },
      "profiles": ["researcher", "debugger"],
      "tokenEnv": "GITHUB_TOKEN",
      "safeTools": ["search_issues"]
    }
  }
}
```

Interpolation: `${env:NAME}`, `${workspaceFolder}`, `${userHome}`. A missing
`${env:NAME}` fails **that server** at load (`status=error`) — no blank secret.

v1 transport is stdio only. Put tokens in `env.sh`, not in events.

## Who can call the tools

Wire names are `mcp_{server}_{tool}`. The `mcp` sentinel on a profile expands
to live tools allowed for that profile. Default allowlist is `researcher` and
`debugger`. The orchestrator does not get MCP tools. `coder` stays off unless
you set `profiles`.

Unannotated tools are treated as destructive and go through `PromptBroker`
when `ENGINE_EXEC_APPROVAL` is `auto` or `always`. `readOnlyHint: true` or
`safeTools` skips the prompt. MCP writes go through the server process and
**bypass the engine write funnel** — keep filesystem-like servers off `coder`
unless you set `profiles`.

`mcp_list_resources` / `mcp_read_resource` are also in the `mcp` family.
`file://` URIs are rejected.

## Auth

The engine does not open a browser. If a server needs login, you get
`McpAuthRequired` plus `UserPromptRequested` (`kind=mcp_auth`) when `tokenEnv`
is set. Paste via `AnswerPrompt` or `CompleteMcpAuth`. Tokens land in
`.engine/mcp-tokens.json` (mode `0600`, gitignored with `.engine/`). A later
401 after a successful connect asks for `CompleteMcpAuth` again — not
`ReloadIntegrations`.

Without `tokenEnv`, you get the event and a warning only.

## Reload

`ReloadIntegrations` rediscovers skills and reconnects MCP. `SetMcpEnabled`
writes `enabled` into `.engine/mcp.json` and reloads.
