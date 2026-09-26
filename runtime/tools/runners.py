"""Structured results for common test and build runners.

`run_command` hands the model prose. A coder that cannot see the
`N passed in Xs` line -- because truncation dropped the tail, or because
the line scrolled past a 400-char trim -- will happily invent the counts,
and everything downstream (the PR draft, the reviewer) inherits the
invention. Parsing the numbers once, here, gives every stage the same
machine-readable answer: `exit_code`, `passed`, `failed`, `skipped`,
`runner`.

An unrecognized command returns a result with `runner=None` and no counts.
That is not an error: `exit_code` alone is still a fact worth reporting.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

__all__ = [
    "RunnerResult",
    "parse_runner_result",
]


@dataclass
class RunnerResult:
    runner: str | None
    exit_code: int
    passed: int | None = None
    failed: int | None = None
    skipped: int | None = None
    # Per-package verdicts for `go test ./...`: [("pkg/path", "ok"|"FAIL"), ...]
    packages: list[tuple[str, str]] | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def as_dict(self) -> dict:
        out = asdict(self)
        if self.packages is not None:
            out["packages"] = [list(item) for item in self.packages]
        return out


# pytest's terminal summary line, e.g.
#   "=== 92 passed, 2 failed, 1 skipped, 3 warnings in 4.21s ==="
# Counts are named, order varies, and any subset may be absent.
_PYTEST_SUMMARY = re.compile(
    r"^=+\s*(?P<body>[^=].*?)\s*(?:in\s[\d.]+s.*)?=+\s*$", re.MULTILINE
)
_PYTEST_COUNT = re.compile(r"(\d+)\s+(passed|failed|error|errors|skipped|xfailed|xpassed)")
_PYTEST_NO_TESTS = re.compile(r"^=+\s*no tests ran", re.MULTILINE | re.IGNORECASE)

# `[ \t]+`, not `\s+`: go prints a bare "FAIL" line before the per-package
# line, and a newline-crossing separator made the next "FAIL" the package name.
_GO_PKG = re.compile(
    r"^(?P<verdict>ok|FAIL|\?)[ \t]+(?P<pkg>[^\s]+)", re.MULTILINE
)
_GO_TEST_PASS = re.compile(r"^\s*--- PASS: ", re.MULTILINE)
_GO_TEST_FAIL = re.compile(r"^\s*--- FAIL: ", re.MULTILINE)
_GO_TEST_SKIP = re.compile(r"^\s*--- SKIP: ", re.MULTILINE)

# Commands whose only useful signal is the exit code.
_EXIT_ONLY_PREFIXES: tuple[tuple[str, str], ...] = (
    ("go build", "go build"),
    ("go vet", "go vet"),
    ("gofmt", "gofmt"),
    ("make", "make"),
    ("tsc", "tsc"),
    ("ruff", "ruff"),
    ("mypy", "mypy"),
    ("cargo build", "cargo build"),
)


def parse_runner_result(
    command: str, exit_code: int, stdout: str = "", stderr: str = ""
) -> RunnerResult:
    """Identify the runner behind `command` and pull its counts out of the
    output. Always returns a RunnerResult; `runner` is None when nothing
    recognized the command."""
    text = f"{stdout or ''}\n{stderr or ''}"
    runner = _runner_for(command)
    if runner == "pytest":
        return _parse_pytest(exit_code, text)
    if runner == "go test":
        return _parse_go_test(exit_code, text)
    if runner == "npm test":
        # `npm test` delegates; try the suites we know before giving up.
        for fallback in (_parse_pytest, _parse_go_test):
            parsed = fallback(exit_code, text)
            if parsed.passed is not None or parsed.failed:
                parsed.runner = "npm test"
                return parsed
        return RunnerResult(runner="npm test", exit_code=exit_code)
    if runner is not None:
        return RunnerResult(runner=runner, exit_code=exit_code)
    return RunnerResult(runner=None, exit_code=exit_code)


def _runner_for(command: str) -> str | None:
    raw = " ".join((command or "").strip().split())
    if not raw:
        return None
    # Look past a leading `cd x &&`, `env FOO=1`, or a `uv run`/`poetry run`
    # wrapper, and treat a chained `a && b` as its last recognizable leg so
    # `go build ./... && go test ./...` reports on the tests.
    legs = [leg.strip() for leg in re.split(r"&&|;", raw) if leg.strip()]
    for leg in reversed(legs):
        found = _runner_for_leg(leg)
        if found is not None:
            return found
    return None


def _runner_for_leg(leg: str) -> str | None:
    tokens = leg.split()
    while tokens and (
        "=" in tokens[0]
        or tokens[0] in {"env", "sudo", "time", "nice", "exec"}
        or (tokens[0] in {"uv", "poetry", "pipenv", "pdm", "rye", "hatch"} and len(tokens) > 1)
        or (tokens[0] == "npx" and len(tokens) > 1)
    ):
        tokens = tokens[1:]
        if tokens and tokens[0] == "run":
            tokens = tokens[1:]
    if not tokens:
        return None
    head = tokens[0]
    rest = " ".join(tokens)
    if head in {"pytest", "py.test"}:
        return "pytest"
    if head in {"python", "python3"} and "pytest" in rest:
        return "pytest"
    if rest.startswith("go test"):
        return "go test"
    if rest.startswith(("npm test", "npm run test", "yarn test", "pnpm test")):
        return "npm test"
    for prefix, name in _EXIT_ONLY_PREFIXES:
        if rest == prefix or rest.startswith(prefix + " "):
            return name
    return None


def _parse_pytest(exit_code: int, text: str) -> RunnerResult:
    result = RunnerResult(runner="pytest", exit_code=exit_code)
    if _PYTEST_NO_TESTS.search(text):
        result.passed = 0
        result.failed = 0
        result.skipped = 0
        return result
    # The last summary line wins: a rerun or a subprocess suite can print
    # more than one, and the final one is this command's own verdict.
    bodies = _PYTEST_SUMMARY.findall(text)
    if not bodies:
        return result
    counts: dict[str, int] = {}
    for name, value in ((m[1], int(m[0])) for m in _PYTEST_COUNT.findall(bodies[-1])):
        key = "failed" if name in {"error", "errors"} else name
        counts[key] = counts.get(key, 0) + value
    if not counts:
        return result
    result.passed = counts.get("passed", 0) + counts.get("xpassed", 0)
    result.failed = counts.get("failed", 0)
    result.skipped = counts.get("skipped", 0) + counts.get("xfailed", 0)
    return result


def _parse_go_test(exit_code: int, text: str) -> RunnerResult:
    packages: list[tuple[str, str]] = []
    for match in _GO_PKG.finditer(text):
        verdict = match.group("verdict")
        pkg = match.group("pkg")
        if verdict == "?":
            continue
        if not any(pkg == name for name, _ in packages):
            packages.append((pkg, verdict))
    result = RunnerResult(
        runner="go test",
        exit_code=exit_code,
        packages=packages or None,
    )
    passed = len(_GO_TEST_PASS.findall(text))
    failed = len(_GO_TEST_FAIL.findall(text))
    skipped = len(_GO_TEST_SKIP.findall(text))
    if passed or failed or skipped:
        result.passed = passed
        result.failed = failed
        result.skipped = skipped
    elif packages:
        # Without -v go prints only per-package lines; a package verdict is
        # still a real pass/fail signal, so report it at package grain.
        result.failed = sum(1 for _, verdict in packages if verdict == "FAIL")
        result.passed = sum(1 for _, verdict in packages if verdict == "ok")
        result.skipped = 0
    return result
