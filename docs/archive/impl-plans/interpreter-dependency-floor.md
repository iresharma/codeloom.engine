---
name: Interpreter and dependency floor
overview: "Fix a broken install and an end-of-life interpreter before any new runtime work lands: requirements.txt pins openrouter>=1.0, that package requires Python >=3.10, and the project runs on 3.9.6 with 0.10.8 installed. Move to a pinned venv on a supported interpreter, confirm the existing 51 tests pass, and delete the eval-based 3.9 annotation fallback."
todos:
  - id: verify
    content: "Reproduce the defect: confirm a clean `pip install -r requirements.txt` on 3.9 cannot resolve openrouter>=1.0, and record the currently-installed versions of every dependency for comparison after the move."
    status: pending
  - id: venv
    content: "Create .venv on /opt/homebrew/bin/python3.14, install requirements, and confirm every dependency resolved to a binary wheel with no source build. Fall back to brew python@3.12 if any wheel is missing."
    status: pending
  - id: pin
    content: "Pin every dependency to an exact version in requirements.txt (openrouter, tree-sitter, the four grammars, pytest). Add .venv to .gitignore if absent."
    status: pending
  - id: sdk-surface
    content: "Re-verify the openrouter 1.x chat surface against the installed package: chat.send_async, stream=True return type, chunk.choices[].delta.{content,reasoning,tool_calls}, chunk.usage, chunk.error, retries=RetryConfig, timeout_ms. Record the findings for the agent-runtime-foundation plan to code against."
    status: pending
  - id: tests
    content: "Run the existing 51-test suite on the new interpreter; run `pytest -m 'not lsp'` too and confirm the gopls-marked module still skips cleanly when the binary is absent."
    status: pending
  - id: annotations
    content: "Delete the 3.9 annotation fallback in protocol/message.py (_type_hints's except TypeError branch and _resolve_annotation) as its own commit, and confirm test_protocol.py still passes."
    status: pending
  - id: docs
    content: "Update the README requirements row, the install snippet, and the note in protocol/ about the 3.9 fallback."
    status: pending
isProject: false
---

# Interpreter and dependency floor

## Why this is a plan and not a footnote

This started as a review question on a different plan — "is Python 3.9 genuinely fixed, or is it a constraint worth lifting?" — and the answer turned out to be worse than a stale interpreter.

**The install described in the README does not work.** [requirements.txt](../../requirements.txt) pins `openrouter>=1.0`. That package declares `requires_python >= 3.10`. The project runs on `/usr/bin/python3` = **3.9.6**. So no version satisfying the pin can be installed on the interpreter in use, and the version actually present is **0.10.8** — *below* the pin it is supposed to satisfy. This is invisible to anyone whose environment already works, which is why it has survived.

Everything else is downstream of that:

- Python 3.9 reached **end of life in October 2025** and receives no security patches.
- There is **no venv in the repo**; dependencies live in `~/Library/Python/3.9/lib/python/site-packages`, so "what is installed" is a property of the machine rather than of the project.
- `tree-sitter` 0.26.0 publishes **cp311, cp312, cp313, cp314** wheels and **no cp39**, so `tree-sitter>=0.23` on 3.9 silently resolves backwards to an older grammar ABI. The pinned-floor style of the whole file is what allows this to differ per machine.
- Homebrew **3.14.6** is already installed at `/opt/homebrew/bin/python3.14`, with none of the dependencies.

So the dependency graph already requires 3.10+. Staying on 3.9 is not "keeping a constraint", it is keeping a contradiction.

## Wheel availability, checked

Queried from PyPI rather than assumed, because a missing wheel is the one thing that would turn this from an afternoon into a project:

- `tree-sitter` 0.26.0 — cp311, cp312, cp313, **cp314**, plus sdist.
- `tree-sitter-python` 0.25.0, `tree-sitter-javascript` 0.25.0, `tree-sitter-go` 0.25.0 — **abi3** wheels, which are forward-compatible across CPython versions, so they install on 3.14 without a build.
- `tree-sitter-typescript` 0.23.2 — abi3 as well.
- `openrouter` 1.1.106 — pure Python (`py3-none-any`).

Nothing here needs a compiler on 3.14. That is the finding that makes 3.14 a reasonable target rather than an adventurous one.

## Steps

1. **Reproduce the defect first.** `python3.9 -m pip install --dry-run -r requirements.txt` and capture the resolver error, plus `pip freeze` of the current working set. Without this, there is no before-picture to compare against and no evidence the change fixed anything.
2. **`python3.14 -m venv .venv`**, `pip install -r requirements.txt`. Confirm from the install log that **every** dependency came from a wheel. A source build for a tree-sitter grammar is the signal to stop and fall back.
3. **Pin exact versions.** `openrouter==1.x.y`, `tree-sitter==0.26.0`, each of the four grammars, `pytest==8.x.y`. The floor-only pins are the mechanism by which this defect stayed hidden; replacing them is the actual fix, and the interpreter bump is what makes the pins satisfiable. Add `.venv/` to [.gitignore](../../.gitignore) if it is not already covered.
4. **Re-verify the `openrouter` 1.x chat surface.** This is the step the dependent plan is waiting on. The following were verified against the *installed 0.10.8* and must be confirmed against 1.x before anything codes to them:
   - `chat.send_async(stream=True)` returns an async-iterable event stream that is also an async context manager.
   - `chunk.choices[i].delta` carries `content`, `reasoning`, and `tool_calls`.
   - `chunk.choices[i].delta.tool_calls[j]` carries `index` (required), `id`, `function.name`, `function.arguments` (fragments).
   - `chunk.usage` carries `prompt_tokens`, `completion_tokens`, `total_tokens`, `cost`, `completion_tokens_details.reasoning_tokens`, `prompt_tokens_details.cached_tokens`, with `UNSET` sentinels rather than `None` for absent optionals.
   - `chunk.error` carries `code` and `message`.
   - `send_async` accepts `retries: RetryConfig`, `timeout_ms: int`.
   - `stream_options.include_usage` is deprecated and a no-op.

   The 1.x README shows the same Speakeasy-generated shape (`chat.send(..., stream=True)` iterated for events, same `components.*` pydantic models), so the expectation is that this is a confirmation rather than a rewrite. Write the findings down; the dependent plan's stream parser reads these fields via `getattr` precisely so a rename costs one line instead of an import error.
5. **Run the suite.** All 51 tests on the new interpreter, plus `pytest -m "not lsp"` to confirm the `gopls`-marked module still skips cleanly.
6. **Delete the 3.9 annotation fallback**, as its own commit so it reverts independently: [protocol/message.py](../../protocol/message.py) `_type_hints`'s `except TypeError` branch and the whole `_resolve_annotation` helper — roughly 35 lines that string-`eval` annotations and **silently degrade a field to `Any`** when the eval fails, which on the wire means a nested dataclass arriving as a raw dict. On 3.10+, `get_type_hints` resolves `X | None` natively. This is the only concrete code win from the bump, and it removes an `eval` from the decode path.
7. **Docs.** README requirements row (`Python 3.9+` → the new floor), the install snippet, and the "explicit fallback for 3.9's lack of runtime PEP 604 unions" claim in the protocol description, which stops being true at step 6.

## Fallback ladder

Ordered, so this cannot become open-ended:

- A grammar wheel is missing on 3.14 → `brew install python@3.12` and retry. 3.12 has the widest wheel coverage of the currently-supported line.
- The `openrouter` 1.x surface differs materially from step 4 → still proceed with the interpreter and the pins, but record the differences prominently, because the dependent plan's Phase 1 is written against the old surface.
- Anything else fights back → **abandon the migration**, and do the one thing that is not optional: **fix the pin downward** to `openrouter>=0.10.8,<1` so that a fresh install works at all. A broken documented install is a worse defect than an old interpreter.

## What depends on this

[Agent runtime foundation](../../agent_runtime_foundation_97c382d7.plan.md) — streaming, cancellation, usage accounting, the executor, and compaction. That plan needs to know which SDK version it is coding against (step 4) and benefits from, but does not require, the interpreter bump: for wrapping a single awaitable, `asyncio.wait_for` and `asyncio.timeout` are equivalent, so nothing there is built on the absence of a 3.11 API. If this plan is abandoned per the fallback ladder, that one proceeds unchanged on 3.9 with a corrected pin.
