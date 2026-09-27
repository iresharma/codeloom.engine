# TypeSafe judge

The engine can optionally consult [TypeSafe](https://typesafe.ai)'s System
One API (`typesafe-sdk`, model `jev-latest`) at a handful of choke points:
`run_command` approval, tool-call verification, search re-ranking,
tool-result screening for prompt injection, compaction-by-relevance,
post-write diagnostics triage, loop progress control, intent routing
(a "locate" turn can resolve via search + rerank and seed `ask` with
`run_with_context`, never the orchestrator's own history; see
`agents/resolver.py`), a semantic write gate, and a
subagent merge gate (scores a subagent's result — drop / summarize / admit
in full — before it re-enters the orchestrator's context; see
`Orchestrator._apply_merge_gate`). TypeSafe is a fast
calibrated classifier, not an agent — the engine sends one `state` blob plus typed
questions (`Noul` for yes/no, `Choice` for picking one of a closed set,
`Score` for an ordinal rating) and gets back a probability and confidence per
question. It never generates an edit, a search query, or a file path;
**the LLM generates, TypeSafe judges, code decides.**

Every judge call degrades to "no opinion" on any failure — a missing key, a
timeout, a rate limit, a 5xx, or the `typesafe-sdk` package not being
installed. `runtime/judge.py`'s `JudgeManager` (owned by `EngineSession`,
built at bind, closed on shutdown — the same lifecycle as `LSPManager`) never
lets a judge failure reach a tool result, a protocol event, or the agent
loop; callers that get `None` back fall through to their pre-existing,
judge-less behaviour. Judgments may only add restriction or information —
they can escalate `auto` to a prompt or a refusal, but they can never
override `guard_write_path`, the syntax gate, the staleness check, or the
write denylist.

The write gate (Phase 7) is the highest-stakes call site, so it gets extra
caution: it always runs its judge call *before* `_apply_sync`'s synchronous
prepare-then-commit, never between the staleness check and the atomic
replace, so a slow or concurrent judge call can never reopen the
write-modify-write race (`tests/test_concurrency.py` exercises this with a
judge that actively yields mid-call). It also never inherits a *blanket*
`ENGINE_JUDGE=enforcing` set for other sites — only an explicit
`ENGINE_JUDGE_WRITE=enforcing` lets it block there. The curated
`ENGINE_JUDGE=calibrated` profile is the one exception: it sets write to
enforcing itself, since the write gate can only ever block on a detected
hardcoded secret — every other signal (scope creep, deletes unrelated
code, a disabled test, a weak intent match) only ever adds a
`[engine: judge flagged this diff -- ...]` note to the result, never a
refusal. Set `ENGINE_JUDGE_WRITE=advisory` to opt back out under
`calibrated`.

Every judged decision emits a `JudgementMade` event (`tag`, `subject`,
`outcome`, `signals`, `enforced`, `latency_ms`) so a blocked or escalated
action is never an inexplicable refusal — `clients/dummy.py` renders it as
`judge <tag> -> <outcome> (advisory|enforced, <n>ms): <subject>`, pins a
coloured card on the tools panel, and puts the last verdict in the status
line. F7 filters the protocol log to `JudgementMade` only. The engine
process prints `judge: <mode> model=… exec=…` (or `judge: off`) at
startup so you can see whether the key loaded before you attach a client.
When `ENGINE_PUSHGATEWAY_URL` is set, the same session snapshot includes
TypeSafe gauges: requests by tag/result (`ok` / `cache` / `error`),
latency, tokens, reported cost, and decisions by tag/outcome/enforced.

**Scope note on the plan's Phase 8.** The plan proposes TypeSafe as a
subagent *dispatcher* — selecting, ordering, and admission-controlling a
fixed catalogue of subagents in place of the LLM — on the premise that no
subagent system exists yet. That premise doesn't hold here: the
orchestrator/subagent system (`agents/orchestrator.py`, six profiles under
`agents/profiles/`, worktree isolation, settle flows, a tested concurrent
write lock) already works, and the orchestrator LLM already handles
selection and ordering through normal tool calls. Replacing that would
compete with a working system for uncertain benefit, and the plan itself
leaves Phase 8's necessity as an open question. What does port cleanly is
the piece the plan calls more important than the dispatch anyway: the
**merge gate**, scoring a subagent's result (`accomplished_its_brief`,
`worth_parent_context`, `contradicts_siblings`) before it re-enters the
orchestrator's context, so a low-value result gets admitted as one line
instead of its full transcript.

To exercise the live call sites from `clients/dummy.py`:

1. Put a real TypeSafe key in `env.sh` as `TYPESAFE_API_KEY` or
   `TYPESAFE_JEV_API_KEY`, and set `ENGINE_JUDGE=calibrated` (or
   `enforcing`; leave the default `advisory` if you only want events).
2. `python app.py` — confirm the startup line is not `judge: off`.
3. `python -m clients.dummy` and send one of:

| Prompt | Call site | What you should see |
|---|---|---|
| `run ls in the workspace` | `exec_approval` | allow card; command runs |
| `run git push --force origin main` | `exec_approval` | prompt or block card; a prompt or a refused tool result |
| `where is the retry logic in this codebase?` | `search_rerank` | ranked card once ripgrep returns more than 10 hits |
| `read docs/judge.md` | `result_screen` | flag/redact card only if the file is treated as agent-directed |
| ask for an edit (`str_replace` / `apply_patch`) | `call_verify` | allow or block card before the write |

A missing key, a timeout, or a 5xx degrades silently to today's path and
emits one `WarningOccurred` for the first failure of the session.

Ship a call site under `ENGINE_JUDGE=advisory` first to collect signal on
real traffic, then promote it to `enforcing` with its own
`ENGINE_JUDGE_<SITE>` override once the false-positive rate is measured.

The offline test suite never calls the real API: `tests/conftest.py` provides
a `FakeJudge`/`FakeVerdict` pair (mirroring `FakeLsp`) with a scripted
`responses` table and a `calls` list, and every judge-backed code path has a
test asserting that a `None` verdict reproduces pre-integration behaviour
exactly. Tests that do call the real API live under `tests/live/` and carry
the `judge` marker, skipped automatically when `TYPESAFE_API_KEY` is unset.
