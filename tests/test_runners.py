"""Item 2: structured runner results, and truncation that keeps the tail.

`parse_runner_result` is pure, so it is exercised on real captured runner
output. The truncation tests drive the real executor
(`runtime.tools.shell.run_command`) against a real subprocess -- the
boundary named by the task -- rather than stubbing the function under test.
"""

from __future__ import annotations

import json
import os

import pytest

from runtime.tools.runners import parse_runner_result
from runtime.tools.shell import (
    MODEL_HEAD_CHARS,
    MODEL_TAIL_CHARS,
    STREAM_CAP,
    format_command_result,
    run_command,
)
from tools.base import elide_middle

# --------------------------------------------------------------------------
# pytest

PYTEST_PASS = """\
============================= test session starts ==============================
platform darwin -- Python 3.11.6, pytest-8.3.2
collected 94 items

tests/test_a.py ..................                                       [ 19%]
tests/test_b.py ..........................                               [ 46%]
tests/test_c.py ...........................................             [100%]

======================== 92 passed, 2 skipped in 4.21s =========================
"""

PYTEST_FAIL = """\
============================= test session starts ==============================
collected 94 items

tests/test_a.py .........F........                                       [ 19%]

=================================== FAILURES ===================================
____________________________ test_redirect_cap _________________________________
E   AssertionError: assert 6 == 5
=========================== short test summary info ============================
FAILED tests/test_a.py::test_redirect_cap - AssertionError: assert 6 == 5
=================== 90 passed, 1 failed, 3 skipped in 5.02s ====================
"""

PYTEST_ERRORS = """\
=========================== short test summary info ============================
ERROR tests/test_a.py
================== 1 failed, 2 errors, 10 passed in 0.51s ======================
"""

PYTEST_NO_TESTS = """\
============================= test session starts ==============================
collected 0 items

============================ no tests ran in 0.01s =============================
"""


def test_pytest_pass_counts():
    parsed = parse_runner_result("pytest -q", 0, PYTEST_PASS)
    assert parsed.runner == "pytest"
    assert parsed.exit_code == 0
    assert (parsed.passed, parsed.failed, parsed.skipped) == (92, 0, 2)
    assert parsed.ok is True


def test_pytest_failure_counts():
    parsed = parse_runner_result("pytest", 1, PYTEST_FAIL)
    assert parsed.runner == "pytest"
    assert (parsed.passed, parsed.failed, parsed.skipped) == (90, 1, 3)
    assert parsed.ok is False


def test_pytest_errors_fold_into_failed():
    parsed = parse_runner_result("pytest", 1, PYTEST_ERRORS)
    assert (parsed.passed, parsed.failed) == (10, 3)


def test_pytest_no_tests_ran_is_zeroes_not_none():
    parsed = parse_runner_result("pytest", 5, PYTEST_NO_TESTS)
    assert (parsed.passed, parsed.failed, parsed.skipped) == (0, 0, 0)


def test_pytest_recognized_through_wrappers():
    for command in (
        "python -m pytest -q",
        "uv run pytest",
        "poetry run pytest tests/",
        "cd sub && pytest -q",
        "CI=1 pytest",
    ):
        assert parse_runner_result(command, 0, PYTEST_PASS).runner == "pytest", command


def test_pytest_last_summary_line_wins():
    text = "==== 1 passed in 0.1s ====\nmore output\n==== 7 passed, 1 failed in 2s ===="
    parsed = parse_runner_result("pytest", 1, text)
    assert (parsed.passed, parsed.failed) == (7, 1)


# --------------------------------------------------------------------------
# go

GO_TEST_OK = """\
ok  \tgithub.com/o/r/internal/auth\t0.312s
ok  \tgithub.com/o/r/internal/cache\t0.011s
?   \tgithub.com/o/r/cmd/server\t[no test files]
"""

GO_TEST_FAIL = """\
--- FAIL: TestRedirectCap (0.00s)
    http_test.go:41: got 6 hops, want 5
--- PASS: TestTimeout (0.00s)
--- SKIP: TestSlow (0.00s)
FAIL
FAIL\tgithub.com/o/r/internal/http\t0.108s
ok  \tgithub.com/o/r/internal/auth\t0.312s
FAIL
"""


def test_go_test_packages_ok():
    parsed = parse_runner_result("go test ./...", 0, GO_TEST_OK)
    assert parsed.runner == "go test"
    assert parsed.packages == [
        ("github.com/o/r/internal/auth", "ok"),
        ("github.com/o/r/internal/cache", "ok"),
    ]
    assert (parsed.passed, parsed.failed) == (2, 0)


def test_go_test_failure_counts_and_packages():
    parsed = parse_runner_result("go test -v ./...", 1, GO_TEST_FAIL)
    assert parsed.runner == "go test"
    assert (parsed.passed, parsed.failed, parsed.skipped) == (1, 1, 1)
    assert ("github.com/o/r/internal/http", "FAIL") in parsed.packages
    assert ("github.com/o/r/internal/auth", "ok") in parsed.packages
    assert parsed.ok is False


def test_chained_go_command_reports_on_the_last_leg():
    command = "go build ./... && go vet ./... && go test ./..."
    parsed = parse_runner_result(command, 0, GO_TEST_OK)
    assert parsed.runner == "go test"


def test_go_build_is_exit_code_only():
    parsed = parse_runner_result("go build ./...", 2, "", "pkg/x.go:3:2: undefined: y")
    assert parsed.runner == "go build"
    assert parsed.exit_code == 2
    assert parsed.passed is None and parsed.failed is None
    assert parsed.ok is False


def test_go_vet_and_make_are_exit_code_only():
    assert parse_runner_result("go vet ./...", 0, "").runner == "go vet"
    assert parse_runner_result("make test", 0, "").runner == "make"


# --------------------------------------------------------------------------
# unrecognized


@pytest.mark.parametrize(
    "command", ["ls -la", "git status", "echo hi", "", "   ", "./weird-script.sh"]
)
def test_unrecognized_command_returns_runner_none(command):
    parsed = parse_runner_result(command, 0, "whatever")
    assert parsed.runner is None
    assert parsed.exit_code == 0
    assert parsed.passed is None
    assert parsed.as_dict()["runner"] is None


# --------------------------------------------------------------------------
# truncation keeps the tail


def test_elide_middle_keeps_both_ends_and_names_the_gap():
    text = "H" * 100 + "M" * 1000 + "T" * 100
    out = elide_middle(text, 100, 100)
    assert out.startswith("H" * 100)
    assert out.endswith("T" * 100)
    assert "[1000 chars elided]" in out


def test_elide_middle_leaves_short_text_alone():
    assert elide_middle("short", 100, 100) == "short"


def _pytest_shim(tmp_path, body: str) -> None:
    """A `pytest` on PATH that prints `body`. The command string the agent
    runs is a real `pytest ...`, so the runner detection under test is the
    same code path a coder hits."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "pytest"
    script.write_text(body)
    script.chmod(0o755)


@pytest.mark.asyncio
async def test_twenty_k_pytest_output_keeps_its_summary_line(tmp_path, monkeypatch):
    # A real subprocess prints 20k of per-test lines, then the summary.
    filler = "\n".join(f"tests/test_{i}.py ..........  [{i}%]" for i in range(600))
    payload = tmp_path / "payload.py"
    payload.write_text(
        "import sys\n"
        f"sys.stdout.write({filler!r})\n"
        "sys.stdout.write('\\n======== 92 passed, 2 failed, 1 skipped in 4.21s ========\\n')\n"
        "sys.exit(1)\n"
    )
    _pytest_shim(tmp_path, f"#!/bin/sh\nexec python3 {payload}\n")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")

    result = await run_command(tmp_path, "pytest -q", approval="never", timeout=60)
    assert result.exit_code == 1
    # Sanity: the raw output really is over the model budget.
    assert len(result.stdout) > MODEL_HEAD_CHARS + MODEL_TAIL_CHARS

    text = format_command_result(result)
    assert "92 passed, 2 failed, 1 skipped in 4.21s" in text
    assert "chars elided" in text
    # And the structured verdict is there without reading the prose.
    block = text.split("--- result ---\n", 1)[1].splitlines()[0]
    assert json.loads(block) == {
        "runner": "pytest",
        "exit_code": 1,
        "passed": 92,
        "failed": 2,
        "skipped": 1,
        "packages": None,
    }


@pytest.mark.asyncio
async def test_executor_capture_keeps_the_tail_past_the_stream_cap(tmp_path):
    # Well past STREAM_CAP: the old sink stopped recording at STREAM_CAP*4
    # and the real end of the stream never made it into the result at all.
    line = "x" * 99 + "\n"
    count = (STREAM_CAP * 5) // 100
    script = tmp_path / "noisy.py"
    script.write_text(
        "import sys\n"
        f"for _ in range({count}):\n"
        f"    sys.stdout.write({line!r})\n"
        "sys.stdout.write('FINAL-MARKER 7 passed in 1.0s\\n')\n"
    )
    result = await run_command(
        tmp_path, f"python3 {script.name}", approval="never", timeout=60
    )
    assert "FINAL-MARKER" in result.stdout
    assert len(result.stdout) <= STREAM_CAP + 64
    assert result.stdout.startswith("x" * 99)


@pytest.mark.asyncio
async def test_unrecognized_command_result_has_no_result_block(tmp_path):
    result = await run_command(tmp_path, "echo hello", approval="never", timeout=30)
    text = format_command_result(result)
    assert "--- result ---" not in text
    assert "hello" in text
    assert result.parsed is not None and result.parsed.runner is None


# --------------------------------------------------------------------------
# the other two caps that used to be head-only


def test_registry_result_cap_keeps_the_tail():
    from tools.registry import MAX_RESULT, RESULT_HEAD, RESULT_TAIL

    text = "H" * 10 + "m" * (MAX_RESULT * 2) + "92 passed in 4.2s"
    out = elide_middle(text, RESULT_HEAD, RESULT_TAIL)
    assert out.startswith("H" * 10)
    assert out.endswith("92 passed in 4.2s")


def test_compaction_trim_keeps_the_tail_within_the_old_budget():
    from agents.compactor import (
        TRIM_KEEP,
        TRIM_KEEP_HEAD,
        TRIM_KEEP_TAIL,
        TRIM_NOTICE,
        trim_tool_results,
    )

    body = "head-of-run " + "x" * 20_000 + " 92 passed, 2 failed in 4.2s"
    messages = [
        {"role": "assistant", "tool_calls": [{"id": "1"}]},
        {"role": "tool", "tool_call_id": "1", "content": body},
        {"role": "assistant", "tool_calls": [{"id": "2"}]},
        {"role": "tool", "tool_call_id": "2", "content": "recent"},
    ]
    out, saved = trim_tool_results(messages, keep=1)
    trimmed = out[1]["content"]
    assert saved > 0
    assert trimmed.startswith("head-of-run ")
    assert "92 passed, 2 failed in 4.2s" in trimmed
    # No budget creep: still inside the head-only cut's footprint.
    assert TRIM_KEEP_HEAD + TRIM_KEEP_TAIL < TRIM_KEEP
    assert len(trimmed) <= TRIM_KEEP + len(TRIM_NOTICE)
