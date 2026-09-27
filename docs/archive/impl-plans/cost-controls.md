---
name: Cost controls
overview: "Cut research-turn cost: prompt cache, append-only child transcripts (ship with github_file windows), one ask/researcher per user message, Haiku for ask/tester, per-agent stats."
todos:
  - id: cache-and-windows
    content: cache_control + freeze child system; github_file windows; disable in-loop child compact (one slice)
    status: completed
  - id: spawn-models-stats
    content: spawn-once survey profiles; Haiku ask/tester; skip labeled compress; AgentFinished usage
    status: completed
isProject: false
---

# Cost controls

A Graphify research turn cost **$5.35**: 2.44M prompt tokens, **0 cache hits**, four Sonnet 5 children, leftover respawns. 91% of the bill was uncached input.

## Sequencing (do not split)

**Disable in-loop child compact and `github_file` windowing in the same change.** Children that keep 50k file dumps and never compact will hit the 120k overflow fuse mid-run. Overflow still force-compacts; the window exists so a normal survey never needs that fuse.

## Cache economics

Sonnet 5: **$2/M** input, **$0.20/M** cache read, **~1.25×** cache write, **~5 min** ephemeral TTL.

Researcher (many turns, growing prefix) is a clear win: turn 1 writes, turns 2–N read at 10%. Short ask/tester loops (2–3 turns on Haiku) may see the write premium eat most of the hit savings. Still mark breakpoints on every OpenRouter call — Haiku prefixes are small, the extra write is pennies, and one code path is simpler than per-profile cache gating.

## Child compact

| When | Keep? |
|---|---|
| Child tool loop (`compact()` at 90% of 120k) | **Off** (`compact_trigger=2.0`) |
| Child exit (`compress_for_parent`) | **On** |
| Provider overflow | **On** (fuse) |

Orch stays 0.7 / keep-3.

**Frozen child system.** Subagents snapshot system (prompt + memory + skills catalog) on first `_build_messages`. `remember()` during that child's run (or a sibling's) does **not** appear in its later system prefix — by design, so the cache prefix stays identical. The tool result still carries the note. Orch re-renders every turn.

## Spawn + models

- At most one `ask` and one `researcher` on **follow-up inbox turns** after those profiles already ran this user message. First user turn may still fan out in parallel. Leftover questions go to the user; do not respawn to chase them. `status=incomplete` may still respawn once.
- **Researcher `max_turns` stays 32.** No per-agent turn telemetry yet (`Stats.agent_runs` is the instrument). Cutting to 16 without counts would `max_turns` → incomplete → respawn, undoing spawn-once. Raise/lower later from telemetry; the knob is already `AgentProfile.max_turns`.
- Ask and tester: `anthropic/claude-haiku-4.5`. Researcher, orch, coder, reviewer, debugger: `OPENROUTER_MODEL`.
- `compress_for_parent` skips the extra LLM call when the closer already has labeled `what` plus `facts` or `verdict`.

## concurrent_tools

`Subagent` defaulted to `False` because `AgentLoop` defaulted to `False` and the runtime-foundation plan deferred parallel dispatch. Orch already sets `True` (spawn several personalities). Children get `True`: gather preserves tool_call order in history; writers serialize via `write_lock`. Not a known race.

## Per-agent stats

`AgentFinished` and `Stats.agent_runs` (cap 20) carry cost / tokens / cached / requests. No protocol version bump.
