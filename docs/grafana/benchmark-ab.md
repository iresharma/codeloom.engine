# A/B benchmark harness

Compare `main` (no TypeSafe judge) to this branch (judge enforcing) on the same target repo and prompt. Metrics land on the existing Pushgateway dashboard as `instance=baseline` and `instance=judge`.

```bash
python scripts/bench_ab.py \
  --repo https://github.com/org/target \
  --prompt "Add a failing test for X and make it pass" \
  --workdir /tmp/bench-empty \
  --job-prefix tui-settings
```

`--workdir` must be empty (created if missing). Keys come from the process environment, falling back to this checkout's `env.sh`. They are exported into the engine processes, not written into the target clones.

Required: `OPENROUTER_API_KEY`, `TYPESAFE_API_KEY` or `TYPESAFE_JEV_API_KEY`. Optional: `OPENROUTER_MODEL`, `ENGINE_PUSHGATEWAY_URL` (unset means no Grafana).

## What it does

1. Clones the engine at `--baseline-ref` (default `main`) and `--treatment-ref` (default: current branch) into `engine-main/` and `engine-judge/`.
2. Creates a venv in each and `pip install -r requirements.txt`.
3. Clones `--repo` once, then clones that copy to `ws-baseline/` and `ws-judge/` so both share the same SHA.
4. Starts both `app.py` processes:
   - Shared: `ENGINE_METRICS_JOB` from `--job-prefix` (`PREFIX-engine`, or `engine` if omitted), `ENGINE_MAX_CONTINUES=1` (one extra 16-turn slice to 48, then handoff).
   - Baseline: `ENGINE_JUDGE=off`, `ENGINE_EXEC_APPROVAL=auto`, `ENGINE_METRICS_INSTANCE=baseline`.
   - Judge: `ENGINE_JUDGE=enforcing`, exec defaults to `judged`, `ENGINE_METRICS_INSTANCE=judge`.
5. Submits `--prompt` via this checkout's `dummy_client.py --message --auto` on both sides in parallel. There is no default wall-clock timeout — the client waits until each orchestrator is idle with no live children, and waits extra after a child finishes so the orch can merge-gate and pump a follow-up (settle/PR). Pass `--timeout N` if you want a safety fuse. Worktree settle auto-answers `pr` (override with `--settle keep|merge|discard`). Event text is written to `logs/client-*.log`; the harness transcript to `logs/run.log`; chat history to `logs/transcript-*.txt`.
6. Stops both servers and prints a side-by-side result table (cost, tokens, turns, tools, elapsed, agents, files changed, last reply, PR URL). The same text is written to `summary.txt`. Trees stay on disk.

## Auto-answer policy

| Prompt | Answer |
| --- | --- |
| Worktree settle | `pr` (opens a GitHub PR; needs `gh` auth. `--settle keep` restores the old behaviour) |
| MCP auth | `no` |
| Turn cap continue/handoff/stop | `continue` (engine hands off at the second ceiling because `ENGINE_MAX_CONTINUES=1`) |
| Exec / confirm / trust | `yes` |

Judge `block` still refuses without asking.

## Grafana

Same dashboard. Filter by `job` (`tui-settings-engine` if you passed `--job-prefix tui-settings`, else `engine`) and `instance=baseline` vs `instance=judge` (or `engine_run_info{workspace="ws-baseline"}`). Compare cost, tokens, turns, tools, elapsed. Judge panels populate only on the treatment run.

The dashboard does not score patch quality — `git diff` in each workspace and under `.engine/worktrees/`. The shell table is the same numbers Grafana has, plus each side's last assistant reply.

## Flags

| Flag | Default |
| --- | --- |
| `--engine-url` | this checkout (local path, so unpushed commits are included) |
| `--baseline-ref` | `main` |
| `--treatment-ref` | current branch |
| `--timeout` | `0` (wait until idle; pass a positive number for a wall-clock fuse) |
| `--job-prefix` | none (`job=engine`). `tui-settings` → `job=tui-settings-engine` |
| `--settle` | `pr` |

## Flow report

`scripts/bench_flow.py` turns a finished `bench_ab.py` run into one HTML
report: a side-by-side Mermaid flowchart of each side's agents plus judge
gate decisions, per-agent narrative pulled from the transcripts (what was
found/built, reviewer verdicts, leftovers), and a data-derived "why the
outcome differs" summary (cost/token deltas, extra agents the judge spawned,
gates that fired, final PR vs. abort). It only reads `--workdir`'s existing
`summary.txt`/`logs/*`, so it can be run standalone after the fact:

```bash
python scripts/bench_flow.py --workdir /tmp/bench-empty
```

Writes `<workdir>/flow-report.html` (override with `--out`).
