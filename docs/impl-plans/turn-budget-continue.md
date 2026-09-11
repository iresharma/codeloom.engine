---
name: Turn budget continue
overview: "Stop treating max_turns as a hard kill. Same-child continue (Cursor Resume / OpenHands increase_limit), a forced closer two turns early, and orch handoff that does not cold-start. Do not raise the 32-turn cap."
todos:
  - id: config
    content: "Add EngineConfig.turn_slice=16, max_continues=3, turn_continue=prompt|never; env knobs; test_config"
    status: completed
  - id: loop-continue
    content: "AgentLoop: closer at N-2, PromptBroker offer at cap, bump max_turns on continue, stopped vs max_turns exit; AgentStateChanged ceiling updates"
    status: completed
  - id: orch-status
    content: "Replace string-detect in _run_child; stopped vs max_turns merge; ORCH_SYSTEM handoff; skip ingest on stopped"
    status: completed
  - id: tests-docs
    content: test_turn_budget.py, README limits, adding-a-profile.md
    status: completed
isProject: false
---

# Turn budget continue

`max_turns` is a cost fuse, not a death. A child still calling tools at the ceiling stays alive: the user can grant another slice on the same `_history`, hand leftover to the orch, or stop. The 32-turn profile cap is unchanged.

```mermaid
flowchart TD
  loop[AgentLoop while turn less than max_turns]
  closer[At N minus 2 inject closer]
  tools{Tool calls?}
  done[Return text status ok]
  cap{At ceiling?}
  offer[PromptBroker continue handoff stop]
  bump["max_turns plus slice same history"]
  handoff[Exit max_turns finish plus orch leftover]
  stopNode[Exit stopped no respawn]

  loop --> closer --> tools
  tools -->|no| done
  tools -->|yes| cap
  cap -->|no| loop
  cap -->|yes| offer
  offer -->|continue under max_continues| bump --> loop
  offer -->|handoff or never or timeout| handoff
  offer -->|stop| stopNode
```

## Config

[`EngineConfig`](../../runtime/config.py):

- `turn_slice` (16) — `ENGINE_TURN_SLICE`
- `max_continues` (3) — `ENGINE_MAX_CONTINUES` (32 + 16×3 = 80 absolute)
- `turn_continue` (`prompt` | `never`) — `ENGINE_TURN_CONTINUE`

Invalid `turn_continue` warns and falls back to `prompt`. `never` is for CI. There is no `always` auto-continue.

## Same-child continue

[`AgentLoop.run`](../../agents/agent_loop.py) is a `while turn < max_turns` loop. At the ceiling, if the last step still had tool calls:

- `never` or no `ask_user` → `max_turns` (handoff). Timeout / empty / unknown answers also hand off. Do not spend a slice unattended.
- Else `PromptBroker.ask` `kind=choice` with `continue` / `handoff` / `stop`, default `handoff`.
- **continue** (`continues < max_continues`): `max_turns += turn_slice` for this `run()` only, append a grant line, keep looping. Next `AgentStateChanged` carries the new ceiling. The original cap is restored when `run()` returns so a later orch turn does not inherit the grant.
- **stop** → `_exit_status = stopped`.
- Exhausted continues → forced handoff, no fourth prompt.

`finish()` is not called until `run()` returns. Worktree, LSP, and transcript stay live. `_run_child` reads `child._exit_status` instead of parsing `"stopped after"`.

Abort during the prompt still goes through [`PromptBroker.cancel_agent`](../../runtime/prompts.py).

## Closer at N−2

Before the complete at `turn == max_turns - 2` (skip if `max_turns < 3`), inject:

> You have two tool turns left. Finish the current edit, or write a closer with labeled leftover: (paths, done, next). Do not start new exploration.

Inject again at each **new** ceiling after a continue grant, once per ceiling. That is what makes handoff a real briefing instead of an empty `last_text`.

## Orch

[`_apply_run_status`](../../agents/orchestrator.py): `stopped` replaces `ok` like `max_turns`. `incomplete` still beats both.

[`ORCH_SYSTEM`](../../agents/orchestrator.py):

- `max_turns` → spawn one writer with leftover / paths / files_touched. Do not rediscover.
- `stopped` → tell the user; do not respawn.
- Ask/researcher stay spawn-once on inbox turns.

[`INGEST_STATUSES`](../../runtime/store/memory.py) stays `{ok, max_turns}`. `stopped` is not ingested.

No new event type. TUI/dummy already render `UserPromptRequested` choices.

## Out of scope

Raising profile `max_turns`, `ENGINE_TURN_CONTINUE=always`, and auto-approving worktree-local shell (`pwd` / `ls` / `mkdir`). Those delay the cliff or spend the budget before the cap.
