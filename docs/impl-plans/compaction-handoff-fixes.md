---
name: Compaction handoff fixes
overview: Fix the subagent→orchestrator handoff so `missing_checks`, task status, and child reports stay truthful. Work stays in `compress_for_parent` / `Subagent.finish` / `_run_child`; mid-loop `compact()` is out of scope except sharing a transcript-clip helper.
todos:
  - id: falsy-empty
    content: Fix tools_called / files_touched None-vs-empty fallback; tests for incomplete + empty edits
    status: completed
  - id: finish-masking
    content: Preserve run status in Subagent.finish except; distinguishable compaction error outcome
    status: completed
  - id: context-md-lock
    content: Lock write_context_md RMW; concurrent append test
    status: completed
  - id: bounded-transcript
    content: Message-bounded tail-biased transcript clip; use in compress_for_parent and _summarize
    status: completed
  - id: summarize-signals
    content: Surface summarize failure; line-aware SUMMARY_CLIP; parse leftover_questions; stop duplicating summary/outcome
    status: completed
  - id: run-status-merge
    content: Stop _run_child from clobbering incomplete; keep compressor summary on failed runs
    status: completed
  - id: docs-edits-only
    content: Document files_touched as edits-only; broaden _paths_from_history keys for None fallback
    status: completed
isProject: false
---

# Compaction handoff fixes

Scope is the **subagent → orchestrator** path, not mid-loop window shrinking:

`Subagent.finish` → [`compress_for_parent`](../../agents/compactor.py) → [`Orchestrator._run_child`](../../agents/orchestrator.py) → `Session._on_agent_result`

Mid-loop `compact()` / `_drop_oldest` stays a follow-up. The one exception: the same 20k JSON clip helper is reused in mid-loop `_summarize` so we do not leave a duplicate mid-JSON cut there.

Deprioritized: short-run skip of LLM summarize (`len(work) <= 4`). `_last_assistant_text` already excludes tool transcripts; leave the threshold as-is.

## Status merge (confirmed, not stale)

[`_run_child`](../../agents/orchestrator.py) lines 430–453:

```python
status = "ok"
outcome = ""
try:
    text = await child.run(task)
    ...
    outcome = text
except ...
    outcome = f"error: {exc}"
result = await child.finish(status)
if status in {"aborted", "failed", "max_turns"}:
    result.status = status
if status == "failed" and outcome.startswith("error:"):
    result.outcome = outcome
```

`outcome` is the **pre-finish** `child.run()` result. It is not stale. The bug is that it then overwrites compressor signals.

Status precedence after the fix:

- `aborted` / `failed` (run crashed) always win
- `incomplete` (missing required tools) beats `max_turns` and `ok`
- `max_turns` only replaces `ok`
- On `failed`, keep compressor `summary`; only fill `outcome` from the exception when the compressor left it empty. Do not wipe a useful closer/report.

## 1. Empty-set fallback (P1) — silently defeats `missing_checks`

Two `or` fallbacks treat empty containers as missing:

```python
called = tools_called or _tools_from_history(messages)
files = list(files_touched or _paths_from_history(messages))
```

`Subagent.finish` always passes `set(self._tools_called)` and `list(self._files_touched)`. `_tools_called` is only updated after a successful `execute`; cancelled/unfinished calls still appear in history `tool_calls`. An empty set is falsy, so a child that *attempted* `get_diagnostics` and failed/cancelled is marked as having called it.

Same pattern for files: zero edits (`[]`) currently falls back to every `path` arg in history, which contradicts the edits-only design.

Fix:

```python
called = tools_called if tools_called is not None else _tools_from_history(messages)
files = list(files_touched) if files_touched is not None else _paths_from_history(messages)
```

Tests in [`tests/test_profiles.py`](../../tests/test_profiles.py): `tools_called=set()` plus a history `tool_calls` for the required tool must stay `incomplete`; `files_touched=[]` plus a `read_file` path must stay empty.

## 2. `Subagent.finish` exception masking (P2)

[`agents/subagent.py`](../../agents/subagent.py) `except` reports `status="failed"` for any `compress_for_parent` bug, even when `child.run` succeeded. `_run_child` will not put it back to `ok`.

Preserve the `status` argument. Distinguish compaction failure in `outcome` (e.g. `compaction error: …`), not `error:` (that prefix is the run-crash signal).

## 3. `write_context_md` race (P3)

Read-modify-write with no lock in [`write_context_md`](../../agents/compactor.py). Today's single-thread asyncio will not interleave two sync RMWs, but the orch `write_context` tool and child finish share the file, and git work already uses `asyncio.to_thread`. Add a module-level `threading.Lock` around the existing RMW (keep cap-trim). Test with two threads appending distinct notes; both must survive.

## 4. Summarize input: message-bounded, tail-biased (P4 + original #3)

Both `compress_for_parent` and mid-loop `_summarize` do `json.dumps(...)[:20_000]`. That cuts mid-JSON **and** keeps the opening of `work`, dropping the closer/error/final file state.

Add `_bounded_transcript(items, limit=20_000) -> str`:

- If `json.dumps(items, default=str)` fits, use it
- Else drop **whole messages from the middle**, always keeping a small head (first message) and as much of the **tail** as fits, with an omitted-middle marker
- Never slice the JSON string

Use it in `compress_for_parent` and `_summarize`.

## 5. Rest of the original list

**Silent summarize failure (#4).** Stop swallowing. Keep last closer as `outcome`. Set `summary` to a visible marker (`summarize failed: TimeoutError`) so the orch sees it. Do not change `status` to `failed`.

**Line-aware `SUMMARY_CLIP` (new #5).** After `text.strip()`, clip on the last full newline before 400 chars (if that newline is past ~half the budget); otherwise keep the hard cut. Prompt already asks for labeled lines; this protects `verdict` / `leftover`.

**`leftover_questions` is dead (#1).** Do not switch the summarizer to JSON. Parse labeled lines from the LLM report and, if empty, from the last closer. Accept `leftover:` / `leftover_questions:` (case-insensitive); split the value on `;`. `as_text()` already emits the field when non-empty; orch prompt already tells the orch to ask the user.

**`summary` / `outcome` duplicate (#7).** Stop assigning `outcome = summary` after a successful LLM call. `summary` = LLM report (or failure marker); `outcome` = last closer. In `as_text()`, omit `outcome` when it equals `summary` so short runs do not print the closer twice.

**`_paths_from_history` (#6).** After the None-vs-empty fix this is fallback-only (`files_touched is None`). Broaden keys to `path`, `file`, `target`, `dest`; skip `glob` (pattern, not a file). Cheap; most tools already use `path`.

**`files_touched` edits-only (#5).** Document in `compress_for_parent` / README: when `Subagent.finish` supplies the list, it is successful edits via `record_edit`, not reads. Do not start recording reads.

**`_run_child` clobber (#8).** Implement the precedence above (small helper `_apply_run_status(result, run_status, run_outcome)` in [`agents/orchestrator.py`](../../agents/orchestrator.py) so it can be unit-tested without a full spawn).

## Tests / docs

- [`tests/test_profiles.py`](../../tests/test_profiles.py): empty-set `called` / empty `files_touched`; leftover parse; summary ≠ outcome; summarize failure marker; line-aware clip; bounded transcript keeps tail
- [`tests/test_compaction.py`](../../tests/test_compaction.py): `_bounded_transcript` + mid-loop `_summarize` uses it; concurrent `write_context_md`
- New focused tests (or a thin helper test) for finish-status preservation and `_apply_run_status`
- Short README note: `files_touched` = edits; leftover is parsed from labeled lines

## Out of scope

- Changing `len(work) > 4` summarize gate
- Redesigning mid-loop `compact()` / `_drop_oldest` / trim ladders (follow-up)
- Recording reads into `_files_touched`
