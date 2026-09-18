# A/B benchmark harness

Compare `main` (no TypeSafe judge) to this branch (judge enforcing) on the same target repo and prompt. Metrics land on the existing Pushgateway dashboard as `instance=baseline` and `instance=judge`.

```bash
python scripts/bench_ab.py \
  --repo https://github.com/org/target \
  --prompt "Add a failing test for X and make it pass" \
  --workdir /tmp/bench-empty
```

`--workdir` must be empty (created if missing). Keys come from the process environment, falling back to this checkout's `env.sh`. They are exported into the engine processes, not written into the target clones.

Required: `OPENROUTER_API_KEY`, `TYPESAFE_API_KEY` or `TYPESAFE_JEV_API_KEY`. Optional: `OPENROUTER_MODEL`, `ENGINE_PUSHGATEWAY_URL` (unset means no Grafana).

## What it does

1. Clones the engine at `--baseline-ref` (default `main`) and `--treatment-ref` (default: current branch) into `engine-main/` and `engine-judge/`.
2. Creates a venv in each and `pip install -r requirements.txt`.
3. Clones `--repo` once, then clones that copy to `ws-baseline/` and `ws-judge/` so both share the same SHA.
4. Starts both `app.py` processes:
   - Shared: `ENGINE_METRICS_JOB=engine`, `ENGINE_MAX_CONTINUES=1` (one extra 16-turn slice to 48, then handoff).
   - Baseline: `ENGINE_JUDGE=off`, `ENGINE_EXEC_APPROVAL=auto`, `ENGINE_METRICS_INSTANCE=baseline`.
   - Judge: `ENGINE_JUDGE=enforcing`, exec defaults to `judged`, `ENGINE_METRICS_INSTANCE=judge`.
5. Submits `--prompt` via `dummy_client.py --message --auto` on both sides in parallel.
6. Stops both servers. Trees stay on disk.

## Auto-answer policy

| Prompt | Answer |
| --- | --- |
| Worktree settle | `keep` |
| MCP auth | `no` |
| Turn cap continue/handoff/stop | `continue` (engine hands off at the second ceiling because `ENGINE_MAX_CONTINUES=1`) |
| Exec / confirm / trust | `yes` |

Judge `block` still refuses without asking.

## Grafana

Same dashboard, `job=engine`. Filter by `instance=baseline` vs `instance=judge` (or `engine_run_info{workspace="ws-baseline"}`). Compare cost, tokens, turns, tools, elapsed. Judge panels populate only on the treatment run.

The dashboard does not score patch quality — `git diff` in each workspace and under `.engine/worktrees/`.

## Flags

| Flag | Default |
| --- | --- |
| `--engine-url` | this checkout (local path, so unpushed commits are included) |
| `--baseline-ref` | `main` |
| `--treatment-ref` | current branch |
| `--timeout` | `1800` seconds per side |
