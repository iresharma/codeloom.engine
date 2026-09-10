# Adding a skill

Skills are markdown folders the engine discovers on `StartSession`. Clients
never parse `SKILL.md`. The model sees a name catalog; `activate_skill` loads
the body into **that agent** only.

## Where to put them

First match on `name` wins:

1. `{workspace}/.engine/skills/<name>/SKILL.md`
2. `{workspace}/.cursor/skills/<name>/SKILL.md`
3. `~/.engine/skills/<name>/SKILL.md`

Do not put skills in `~/.cursor/skills-cursor/` — that tree is ignored.

```
docs/
├── SKILL.md
├── reference.md
└── scripts/
    └── check.sh
```

## SKILL.md

```markdown
---
name: docs
description: Write or update markdown docs. Use when the user mentions README or docs.
---

# Docs

Only edit markdown. Link extra detail from [reference.md](reference.md).
```

`name` and `description` are required for a useful catalog. Set
`disable-model-invocation: true` to hide the name from the model until a client
sends `ActivateSkill` (orch catalog unlock for the session) or that agent calls
`activate_skill`.

## What the model sees

- Every auto-invoke **name** is always listed.
- Descriptions are a lexical top-8 plus sticky names already shown on that
  agent (cap 12) so near-ties do not flicker.
- `activate_skill(name)` attaches the body to that `AgentLoop` until the child
  finishes or the orch session ends.
- `read_skill(name, path)` reads a sibling file inside the skill directory only.

The orchestrator and every profile have `activate_skill` / `read_skill`.
`ActivateSkill` does not push a body into children.

## Scripts

v1 has no `run_skill_script`. If the skill lives in the workspace and the
profile has `run_command`, the agent can run `scripts/` itself.
