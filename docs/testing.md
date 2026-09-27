# Testing

```bash
pytest                      # everything (judge and lsp live tests skip themselves)
pytest -m "not lsp"         # skip tests that spawn a real language server
pytest -m judge tests/live  # calibration fixtures against the real TypeSafe API
pytest tests/test_apply.py  # one module
```

`pytest.ini` sets `pythonpath = .` and `testpaths = tests`, so no install step
is needed. The default run is capped at 4 pytest-xdist workers (`-n logical
--maxprocesses=4 --dist loadfile`); CI overrides that with `-n 2 --dist
loadscope`. Uncapped `-n auto` on a 12-core machine used to leave several
multi-gigabyte Python processes behind after the suite (or after Ctrl-C).
Pass `-n0` to disable workers entirely for a single-file debug run.

To check coverage locally:

```bash
pytest --cov                       # coverage summary in the terminal
coverage-badge -o coverage.svg -f  # regenerate the badge shown at the top of the README
```

`.coveragerc` scopes coverage to the source packages and excludes `tests/`.
CI (`.github/workflows/tests.yml`) runs `pytest --cov` on every push and pull
request, and on pushes to `main` it regenerates `coverage.svg` and commits it
back to the repo with `[skip ci]` so the badge stays current without
retriggering the workflow.

Tests across 18 modules. The original write-path suite is unchanged; the new
modules cover the runtime foundation (config, streaming, turns, stats, shell,
prompts, compaction):

| Module | Covers |
|---|---|
| `test_primitives.py` | `str_replace`, `replace_lines`, `insert_at_line`, diff formatting |
| `test_syntax.py` | The syntax gate, including that already-broken files stay editable |
| `test_patch.py` | Unified diff parsing, fuzz offsets, all-or-nothing multi-hunk application |
| `test_identity_and_guard.py` | CRLF/BOM/trailing-newline round-trips, the write denylist, symlink refusal, sibling-inherited newlines |
| `test_apply.py` | Staleness detection, create conflicts, undo round-trip, directory pruning, read-before-edit |
| `test_symbols.py` | Tree-sitter symbol replacement and import insertion |
| `test_concurrency.py` | Concurrent edits to the same and different files — no hangs, exactly one winner per conflicting file |
| `test_tools_registry.py` | Tool discovery and workspace edits |
| `test_protocol.py` | Codec round-trips, snapshot and history streaming |
| `test_lsp_write.py` | Integration against real `gopls`: view refresh after writes, diagnostic resync, cross-file rename, undo batches |
| `test_config.py` | `EngineConfig.from_env`, `env.sh` ordering, malformed knobs |
| `test_llm_stream.py` | Streaming chunk assembly, idle timeout, usage extraction |
| `test_subscriber.py` | Bounded queues, delta-drop policy, size-field coverage |
| `test_turn_control.py` | Turn-as-task, queued submits, abort mid-complete and between tools |
| `test_stats.py` | Usage accumulation and stats persistence |
| `test_shell.py` | `run_command` executor: denials, approval, output caps |
| `test_prompts.py` | `PromptBroker` ask/answer/cancel and confirm timeout |
| `test_compaction.py` | Tool-result trim, history invariant, overflow markers |

The `conftest.py` `ctx` fixture builds a `ToolContext` over `tmp_path` with a
real SQLite journal and a fresh `FileTracker`. The `seed()` helper writes a file
and marks it read, satisfying the read-before-edit guard.

`test_lsp_write.py` is marked `lsp` and skipped when `gopls` is absent. `gopls`
was chosen over the npx-based servers because it runs straight from `PATH`
without a package fetch, keeping the suite fast and hermetic. Everything else
runs offline with no external binaries — `test_concurrency.py` uses a `FakeLsp`
stub rather than a real server.

Lint with `ruff check .`; the rule set lives in `pyproject.toml` and CI runs it
before the tests.
