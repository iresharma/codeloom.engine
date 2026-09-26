"""Harness-run verification of a writer worktree.

Two of the three PR trials ended with a reviewer that approved without
running anything -- in one case because it has no shell at all and said so,
recommending that "the orchestrator confirm the build before merging". No
stage ever did. Asking an agent to verify is a request; running the command
here is a fact.

The harness picks one command (explicit config, then detection, then the
command the coder last ran successfully), runs it in the worktree, and
hands the *structured* result (runtime/tools/runners.py) to the reviewer's
brief and to the settle gate. Nothing downstream has to trust prose.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from runtime.tools.fs import should_skip_name
from runtime.tools.runners import RunnerResult, parse_runner_result
from runtime.tools.shell import (
    MODEL_HEAD_CHARS,
    MODEL_TAIL_CHARS,
    run_command,
)
from tools.base import elide_middle

__all__ = [
    "VerifyResult",
    "detect_verify_commands",
    "format_verify_block",
    "run_verify",
    "settle_verify_refusal",
    "task_declares_no_tests",
]

# A verify run is a full test suite, not a one-liner. `run_command` caps at
# HARD_MAX_TIMEOUT (600s) anyway; this is the default we ask for.
VERIFY_TIMEOUT_S = 600
# Phrases that count as the task explicitly saying there is nothing to run.
_NO_TESTS = re.compile(
    r"\b(no tests?\b|there (?:are|is) no tests?|without tests?|"
    r"no test suite|skip(?:ping)? tests?|tests? are not required)",
    re.IGNORECASE,
)
_BUILD_ONLY_RUNNERS = frozenset({"go build", "go vet", "tsc", "make"})
# The engine's own control variables must not reach the project under test.
# The trial harness exports ENGINE_TRACE_CALLS=1 for its tracing, and a
# project's test that reads it (codeloom.engine's test_session_trace) failed
# under verify while passing everywhere else.
VERIFY_DROP_ENV = ("ENGINE_",)

_PYTEST_ID = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.MULTILINE)
_GO_FAIL_TEST = re.compile(r"^\s*--- FAIL:\s+(\S+)", re.MULTILINE)
_GO_FAIL_PKG = re.compile(r"^FAIL[ \t]+(\S+)", re.MULTILINE)
# `path/file.go:12:3: message` -- compiler and `go vet` diagnostics. Line and
# column are dropped so an unrelated edit shifting a line does not make an old
# failure look new.
_GO_DIAG = re.compile(r"^(\S+\.go):\d+(?::\d+)?:\s+(.+?)\s*$", re.MULTILINE)


@dataclass
class VerifyResult:
    """One harness verify run. `ok` is exit code 0 and nothing else."""

    command: str
    exit_code: int
    runner: str | None = None
    passed: int | None = None
    failed: int | None = None
    skipped: int | None = None
    output: str = ""
    source: str = ""  # config | detected | coder | none
    reason: str = ""  # why there is no result at all
    # What failed, normalised so two runs can be compared (see
    # `failure_signature`). Empty when the output was not recognisable.
    signature: frozenset[str] = frozenset()
    # Set by `compare_to_baseline` on a failing result.
    preexisting: bool = False
    new_failures: list[str] = field(default_factory=list)
    baseline_failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and bool(self.command)

    @property
    def passes_gate(self) -> bool:
        """True when nothing this change did makes verification worse: a clean
        run, or a failing run whose every failure was already there on the
        base commit."""
        return self.ok or (bool(self.command) and self.preexisting)

    @property
    def is_build(self) -> bool:
        return self.runner in _BUILD_ONLY_RUNNERS

    def as_dict(self) -> dict:
        return {
            "command": self.command,
            "exit_code": self.exit_code,
            "runner": self.runner,
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "source": self.source,
            "preexisting_failures_only": self.preexisting,
        }

    @classmethod
    def unavailable(cls, reason: str) -> VerifyResult:
        return cls(command="", exit_code=-1, source="none", reason=reason)


@dataclass
class VerifyPlan:
    commands: list[str] = field(default_factory=list)
    source: str = "none"


def task_declares_no_tests(text: str) -> bool:
    """True when the task itself says there is nothing to run.

    Only ever relaxes the *test* requirement -- a build, where one exists,
    is still required (see `settle_verify_refusal`).
    """
    return bool(_NO_TESTS.search(text or ""))


def detect_verify_commands(
    workspace: Path, *, config=None, last_command: str = ""
) -> VerifyPlan:
    """Choose the verify command(s) for `workspace`.

    Order: explicit config, then language detection, then the command the
    coder last ran successfully. Returns an empty plan when none of those
    produce anything.
    """
    configured = (getattr(config, "verify_command", "") or "").strip()
    if not configured:
        configured = (os.environ.get("ENGINE_VERIFY_CMD") or "").strip()
    if configured:
        return VerifyPlan([configured], "config")

    workspace = Path(workspace)
    detected = _detect(workspace)
    if detected:
        return VerifyPlan(detected, "detected")

    fallback = (last_command or "").strip()
    if fallback:
        return VerifyPlan([fallback], "coder")
    return VerifyPlan([], "none")


def _detect(workspace: Path) -> list[str]:
    try:
        names = {entry.name for entry in workspace.iterdir()}
    except OSError:
        return []
    if "go.mod" in names:
        return ["go build ./... && go vet ./... && go test ./..."]
    if _is_python(workspace, names):
        return ["pytest -q"]
    script = _npm_test_script(workspace, names)
    if script:
        return [script]
    return []


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def _is_python(workspace: Path, names: set[str]) -> bool:
    if "pytest.ini" in names or "tox.ini" in names:
        return True
    if "pyproject.toml" in names and "pytest" in _read(workspace / "pyproject.toml"):
        return True
    if "setup.cfg" in names and "[tool:pytest]" in _read(workspace / "setup.cfg"):
        return True
    return "tests" in names and (workspace / "tests").is_dir()


def _npm_test_script(workspace: Path, names: set[str]) -> str:
    if "package.json" not in names:
        return ""
    try:
        payload = json.loads((workspace / "package.json").read_text(errors="replace"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    scripts = payload.get("scripts")
    if not isinstance(scripts, dict):
        return ""
    # Only when a real test script exists -- npm's default stub exits 1.
    script = scripts.get("test")
    if not isinstance(script, str) or not script.strip():
        return ""
    if "no test specified" in script:
        return ""
    return "npm test"


async def run_verify(
    workspace: Path,
    plan: VerifyPlan | list[str],
    *,
    source: str = "",
    timeout: int = VERIFY_TIMEOUT_S,
) -> VerifyResult:
    """Run the plan in `workspace`. Stops at the first non-zero exit code.

    `approval="never"` is correct here and only here: this is the harness
    running a command it chose itself, not an agent asking to run one.
    """
    if isinstance(plan, VerifyPlan):
        commands = list(plan.commands)
        source = source or plan.source
    else:
        commands = list(plan or [])
        source = source or "detected"
    if not commands:
        return VerifyResult.unavailable("no verify command could be determined")
    last: VerifyResult | None = None
    for command in commands:
        result = await run_command(
            workspace,
            command,
            timeout=timeout,
            approval="never",
            drop_env_prefixes=VERIFY_DROP_ENV,
        )
        parsed = result.parsed or parse_runner_result(
            command, result.exit_code, result.stdout, result.stderr
        )
        last = _to_verify(command, result, parsed, source)
        if not last.ok:
            return last
    return last if last is not None else VerifyResult.unavailable("no command ran")


def _to_verify(command, result, parsed: RunnerResult, source: str) -> VerifyResult:
    body = (result.stdout or "") + (
        f"\n--- stderr ---\n{result.stderr}" if result.stderr else ""
    )
    return VerifyResult(
        command=command,
        exit_code=result.exit_code,
        runner=parsed.runner,
        passed=parsed.passed,
        failed=parsed.failed,
        skipped=parsed.skipped,
        output=elide_middle(body, MODEL_HEAD_CHARS, MODEL_TAIL_CHARS),
        source=source,
        signature=failure_signature(body),
    )


def failure_signature(output: str) -> frozenset[str]:
    """The failures named in a runner's output, normalised for comparison.

    pytest: the `FAILED`/`ERROR` test ids. go test: failing tests and
    packages. go build / go vet: `file: message` with line numbers removed.
    Anything else yields an empty set, which `compare_to_baseline` treats as
    "cannot tell" -- and therefore as introduced by the change.
    """
    found: set[str] = set()
    found.update(f"pytest:{m}" for m in _PYTEST_ID.findall(output or ""))
    found.update(f"go-test:{m}" for m in _GO_FAIL_TEST.findall(output or ""))
    found.update(f"go-pkg:{m}" for m in _GO_FAIL_PKG.findall(output or ""))
    found.update(f"go:{f}: {msg}" for f, msg in _GO_DIAG.findall(output or ""))
    return frozenset(found)


def compare_to_baseline(change: VerifyResult, baseline: VerifyResult | None) -> VerifyResult:
    """Mark which of `change`'s failures were already failing on the base.

    Conservative on purpose. A failure is only called pre-existing when both
    runs failed, both outputs were recognisable, and the change added nothing
    to the base's set. An unrecognisable output, a base that passed, or a base
    that could not be run all leave the failure counted against the change.
    """
    if change.ok or baseline is None or not baseline.command or baseline.ok:
        return change
    if not change.signature or not baseline.signature:
        return change
    new = sorted(change.signature - baseline.signature)
    change.baseline_failures = sorted(baseline.signature)
    change.new_failures = new
    change.preexisting = not new
    return change


async def run_baseline(
    workspace: Path,
    dest: Path,
    plan: VerifyPlan | list[str],
    *,
    timeout: int = VERIFY_TIMEOUT_S,
) -> VerifyResult | None:
    """Run `plan` on the commit `dest` branched from, in a throwaway checkout.

    Only called when the change's own verify failed, so a healthy run never
    pays for it. Returns None when no checkout could be made (not a repo, no
    merge base): the caller then keeps the failure counted against the change.
    The checkout is detached, outside the workspace, and always removed.
    """
    from runtime.tools.git import exec_cmd, remove_agent_worktree

    workspace = Path(workspace).resolve()
    head = exec_cmd(workspace, ["git", "rev-parse", "HEAD"], timeout=20)
    if head.returncode != 0:
        return None
    base = exec_cmd(
        dest, ["git", "merge-base", head.stdout.strip(), "HEAD"], timeout=20
    ).stdout.strip()
    if not base:
        return None
    tmp = Path(tempfile.mkdtemp(prefix="engine-baseline-"))
    tree = tmp / "tree"
    added = exec_cmd(
        workspace, ["git", "worktree", "add", "--detach", str(tree), base], timeout=60
    )
    if added.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        return None
    try:
        return await run_verify(tree, plan, source="baseline", timeout=timeout)
    finally:
        remove_agent_worktree(workspace, tree)
        shutil.rmtree(tmp, ignore_errors=True)


def format_verify_block(result: VerifyResult) -> str:
    """The verify section injected into the reviewer's brief."""
    if not result.command:
        return (
            "=== HARNESS VERIFY ===\n"
            f"not run: {result.reason or 'unavailable'}\n"
            "Treat the change as unverified. Say so in your verdict."
        )
    if result.ok:
        verdict = "PASSED"
    elif result.preexisting:
        verdict = "FAILED — but only on failures that already fail on the base commit"
    else:
        verdict = "FAILED"
    lines = [
        "=== HARNESS VERIFY (run by the engine, not by an agent) ===",
        f"verdict: {verdict}",
        f"command: {result.command}  (chosen from: {result.source})",
        f"structured: {json.dumps(result.as_dict(), sort_keys=True)}",
    ]
    if result.preexisting:
        lines.append(
            "These failures also occur on the untouched base commit, so this "
            "change did not cause them. Do not treat them as a defect of the "
            "change, and do not widen the scope to fix them:"
        )
        lines.extend(f"  - {item}" for item in result.baseline_failures[:20])
    elif result.new_failures:
        lines.append("Failures this change introduced (absent on the base commit):")
        lines.extend(f"  - {item}" for item in result.new_failures[:20])
    lines += [
        "output:",
        result.output or "(empty)",
        "=== END HARNESS VERIFY ===",
    ]
    return "\n".join(lines)


def settle_verify_refusal(
    result: VerifyResult | None, *, task: str = ""
) -> str:
    """Why settle must not open a PR, or "" when it may.

    A passing harness verify is the only clean path. The one carve-out is a
    task that explicitly says there are no tests -- and even then a build,
    where the project has one, must still have succeeded.
    """
    if result is not None and result.passes_gate:
        return ""
    no_tests = task_declares_no_tests(task)
    if result is None or not result.command:
        if no_tests:
            return ""
        return (
            "no harness verify result for this worktree; refusing to open a "
            "pull request. Run the project's tests (or say in the task that "
            "there are none) and settle again."
        )
    if no_tests and result.is_build:
        return (
            f"the task says there are no tests, but the build still failed: "
            f"`{result.command}` exited {result.exit_code}"
        )
    if no_tests:
        return ""
    counts = ""
    if result.failed:
        counts = f" ({result.failed} failed)"
    return (
        f"harness verify failed{counts}: `{result.command}` exited "
        f"{result.exit_code}; refusing to open a pull request"
    )


# --------------------------------------------------------------------------
# the reviewer's one allowlisted tool runs here, on a throwaway copy


class DisposableWorktree:
    """A copy of a worktree that the reviewer may run (and mutate) freely.

    Made outside the workspace so no path inside it can reach the real
    worktree, and removed on exit whatever happens. Caches, venvs and .git
    are skipped (`should_skip_name`) so the copy stays cheap.
    """

    def __init__(self, source: Path):
        self._source = Path(source).resolve()
        self.path: Path | None = None
        self._tmp: str | None = None

    def __enter__(self) -> Path:
        self._tmp = tempfile.mkdtemp(prefix="engine-verify-")
        dest = Path(self._tmp) / "tree"
        shutil.copytree(
            self._source,
            dest,
            ignore=lambda _dir, names: [n for n in names if should_skip_name(n)],
            symlinks=True,
        )
        self.path = dest
        return dest

    def __exit__(self, *exc) -> None:
        if self._tmp:
            shutil.rmtree(self._tmp, ignore_errors=True)
        self._tmp = None
        self.path = None
