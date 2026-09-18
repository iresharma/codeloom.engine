#!/usr/bin/env python3
"""A/B two engine versions against the same target repo and prompt.

Run from this checkout, pointing at an empty work directory:

    python scripts/bench_ab.py \\
      --repo https://github.com/org/target \\
      --prompt "Add a failing test for X and make it pass" \\
      --workdir /tmp/bench-empty
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from llm.openrouter import PLACEHOLDERS, load_env_sh  # noqa: E402
from runtime.config import typesafe_api_key_from_env  # noqa: E402
from runtime.store.sqlite import list_sessions, load as load_snapshot  # noqa: E402


class BenchError(RuntimeError):
    pass


# main's dummy_client treats timeout<=0 as 0.1s. Drive with this checkout's
# client instead, and keep a 24h fuse only as a last-resort wall cap.
_CLIENT_UNLIMITED_S = 24 * 60 * 60


def _client_timeout_arg(timeout: float | None) -> str:
    if timeout is None or timeout <= 0:
        return str(_CLIENT_UNLIMITED_S)
    return str(timeout)


def _metrics_job(prefix: str) -> str:
    prefix = "-".join((prefix or "").split()).strip("-")
    if not prefix:
        return "engine"
    return f"{prefix}-engine"


class _Tee:
    def __init__(self, *streams) -> None:
        self._streams = streams

    def write(self, data) -> int:
        for stream in self._streams:
            stream.write(data)
            stream.flush()
        return len(data) if isinstance(data, str) else 0

    def flush(self) -> None:
        for stream in self._streams:
            stream.flush()


@dataclass
class SideResult:
    name: str
    session_id: str = ""
    cost: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0
    requests: int = 0
    turns: int = 0
    tool_calls: int = 0
    elapsed_s: float = 0.0
    agents: list[str] = field(default_factory=list)
    files_changed: int = 0
    diffstat: str = ""
    last_reply: str = ""
    pr_url: str = ""
    error: str = ""


def _git(args: list[str], *, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return (result.stdout or "").strip()


def _git_ok(args: list[str], *, cwd: Path) -> str:
    try:
        return _git(args, cwd=cwd)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def _run(args: list[str], *, cwd: Path | None = None, env: dict | None = None) -> None:
    subprocess.run(args, cwd=cwd, check=True, env=env)


def _require_empty(workdir: Path) -> None:
    if not workdir.exists():
        workdir.mkdir(parents=True)
        return
    if not workdir.is_dir():
        raise BenchError(f"{workdir} is not a directory")
    leftover = [path.name for path in workdir.iterdir()]
    if leftover:
        raise BenchError(
            f"{workdir} is not empty: {', '.join(sorted(leftover)[:8])}"
        )


def _current_ref(repo: Path) -> str:
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo)
    if branch == "HEAD":
        return _git(["rev-parse", "HEAD"], cwd=repo)
    return branch


def _clone(url: str, dest: Path, *, branch: str | None = None) -> None:
    cmd = ["git", "clone"]
    if branch:
        cmd.extend(["--branch", branch])
    cmd.extend([url, str(dest)])
    _run(cmd)


def _point_origin(workspace: Path, url: str) -> None:
    _git(["remote", "set-url", "origin", url], cwd=workspace)


def _venv_python(engine_dir: Path) -> Path:
    return engine_dir / ".venv" / "bin" / "python"


def _setup_venv(engine_dir: Path) -> None:
    python = Path(sys.executable)
    _run([str(python), "-m", "venv", str(engine_dir / ".venv")])
    pip = engine_dir / ".venv" / "bin" / "pip"
    _run(
        [
            str(pip),
            "install",
            "--disable-pip-version-check",
            "-q",
            "-r",
            str(engine_dir / "requirements.txt"),
        ]
    )


def _wait_socket(path: Path, proc: subprocess.Popen, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        code = proc.poll()
        if code is not None:
            raise BenchError(f"engine exited {code} before the socket appeared ({path})")
        time.sleep(0.1)
    raise BenchError(f"timed out waiting for {path}")


def _stop(proc: subprocess.Popen, log) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    log.close()


def _base_env(job: str) -> dict[str, str]:
    env = os.environ.copy()
    env["ENGINE_METRICS_JOB"] = job
    env["ENGINE_MAX_CONTINUES"] = "1"
    env["ENGINE_TRACE_CALLS"] = "1"
    env.pop("ENGINE_TURN_CONTINUE", None)
    return env


def _baseline_env(job: str) -> dict[str, str]:
    env = _base_env(job)
    env["ENGINE_JUDGE"] = "off"
    env["ENGINE_EXEC_APPROVAL"] = "auto"
    env["ENGINE_METRICS_INSTANCE"] = "baseline"
    return env


def _judge_env(job: str) -> dict[str, str]:
    env = _base_env(job)
    env["ENGINE_JUDGE"] = "enforcing"
    env.pop("ENGINE_EXEC_APPROVAL", None)
    env["ENGINE_METRICS_INSTANCE"] = "judge"
    return env


def _start_engine(
    engine_dir: Path, workspace: Path, env: dict[str, str], log_path: Path
) -> tuple[subprocess.Popen, object]:
    log = log_path.open("w")
    python = _venv_python(engine_dir)
    proc = subprocess.Popen(
        [str(python), str(engine_dir / "app.py"), str(workspace)],
        cwd=engine_dir,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    return proc, log


def _run_client(
    engine_dir: Path,
    workspace: Path,
    prompt: str,
    timeout: float | None,
    env: dict[str, str],
    log_path: Path,
    settle: str,
) -> None:
    python = _venv_python(engine_dir)
    cmd = [
        str(python),
        str(ROOT / "dummy_client.py"),
        str(workspace),
        "--message",
        prompt,
        "--auto",
        "--timeout",
        _client_timeout_arg(timeout),
        "--settle",
        settle,
    ]
    client_env = env.copy()
    client_env["PYTHONPATH"] = str(ROOT) + os.pathsep + client_env.get("PYTHONPATH", "")
    with log_path.open("w") as log:
        result = subprocess.run(
            cmd,
            cwd=str(ROOT),
            env=client_env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    if result.returncode != 0:
        raise BenchError(
            f"headless client for {workspace.name} exited {result.returncode} "
            f"(see {log_path})"
        )


_ABORT_REPLY = "(aborted by the user)"


def _last_assistant(snapshot) -> str:
    for message in reversed(list(snapshot.messages or [])):
        role = (message.role or "").split()[0].lower()
        if role != "assistant":
            continue
        text = (message.text or "").strip()
        if not text or text.startswith(_ABORT_REPLY):
            continue
        return text
    return ""


def _git_picture(workspace: Path) -> tuple[int, str]:
    parts: list[str] = []
    porcelain = _git_ok(["status", "--porcelain"], cwd=workspace)
    files = [line for line in porcelain.splitlines() if line.strip()]
    shortstat = _git_ok(["diff", "--shortstat"], cwd=workspace)
    if shortstat:
        parts.append(shortstat)
    trees = workspace / ".engine" / "worktrees"
    if trees.is_dir():
        for dest in sorted(p for p in trees.iterdir() if p.is_dir()):
            extra = _git_ok(["diff", "--shortstat"], cwd=dest)
            if extra:
                parts.append(f"{dest.name}: {extra}")
            extra_files = _git_ok(["status", "--porcelain"], cwd=dest)
            files.extend(line for line in extra_files.splitlines() if line.strip())
    return len(files), "; ".join(parts)


_PR_URL = re.compile(r"https?://[^\s)\]>'\"*]+", re.I)


def _pr_urls(snapshot) -> str:
    found: list[str] = []
    seen: set[str] = set()
    for message in snapshot.messages or []:
        for match in _PR_URL.finditer(message.text or ""):
            url = match.group(0).rstrip(".,;:!?")
            if "/pull/" not in url and "/pulls/" not in url:
                continue
            if url in seen:
                continue
            seen.add(url)
            found.append(url)
    return "; ".join(found)


def _write_transcript(workspace: Path, dest: Path) -> None:
    db = workspace / ".engine" / "session.db"
    sessions = list_sessions(db)
    if not sessions:
        dest.write_text("(no session)\n")
        return
    snapshot = load_snapshot(db, sessions[0].id)
    if snapshot is None:
        dest.write_text(f"(could not load session {sessions[0].id})\n")
        return
    blocks = []
    for message in snapshot.messages or []:
        role = (message.role or "?").strip() or "?"
        text = (message.text or "").rstrip() or "(empty)"
        blocks.append(f"## {role}\n{text}")
    dest.write_text("\n\n".join(blocks) + "\n" if blocks else "(no messages)\n")


def collect_side(name: str, workspace: Path) -> SideResult:
    result = SideResult(name=name)
    db = workspace / ".engine" / "session.db"
    sessions = list_sessions(db)
    if not sessions:
        result.error = "no session in sqlite"
        result.files_changed, result.diffstat = _git_picture(workspace)
        return result
    snapshot = load_snapshot(db, sessions[0].id)
    if snapshot is None:
        result.error = f"could not load session {sessions[0].id}"
        result.files_changed, result.diffstat = _git_picture(workspace)
        return result
    stats = snapshot.stats
    result.session_id = snapshot.session_id or sessions[0].id
    result.cost = float(stats.cost or 0)
    result.prompt_tokens = int(stats.prompt_tokens or 0)
    result.completion_tokens = int(stats.completion_tokens or 0)
    result.cached_tokens = int(stats.cached_tokens or 0)
    result.total_tokens = int(stats.total_tokens or 0)
    result.requests = int(stats.requests or 0)
    result.turns = int(stats.turns or 0)
    result.tool_calls = int(stats.tool_calls or 0)
    result.elapsed_s = float(stats.elapsed_s or 0)
    result.agents = [row.profile for row in (stats.agent_runs or []) if row.profile]
    result.last_reply = _last_assistant(snapshot)
    result.pr_url = _pr_urls(snapshot)
    result.files_changed, result.diffstat = _git_picture(workspace)
    return result


def _cell(left: str, mid: str, right: str, width: int = 16) -> str:
    return f"{left:<14} {mid:>{width}} {right:>{width}}"


def _preview(text: str, limit: int = 800) -> str:
    text = (text or "").strip() or "(none)"
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def format_report(baseline: SideResult, judge: SideResult) -> str:
    rows = [
        _cell("", baseline.name, judge.name),
        _cell("session", baseline.session_id[:12] or "—", judge.session_id[:12] or "—"),
        _cell("cost USD", f"{baseline.cost:.4f}", f"{judge.cost:.4f}"),
        _cell("tokens", str(baseline.total_tokens), str(judge.total_tokens)),
        _cell("  prompt", str(baseline.prompt_tokens), str(judge.prompt_tokens)),
        _cell("  completion", str(baseline.completion_tokens), str(judge.completion_tokens)),
        _cell("  cached", str(baseline.cached_tokens), str(judge.cached_tokens)),
        _cell("LLM requests", str(baseline.requests), str(judge.requests)),
        _cell("turns", str(baseline.turns), str(judge.turns)),
        _cell("tool calls", str(baseline.tool_calls), str(judge.tool_calls)),
        _cell("elapsed s", f"{baseline.elapsed_s:.1f}", f"{judge.elapsed_s:.1f}"),
        _cell("agents", str(len(baseline.agents)), str(len(judge.agents))),
        _cell("  profiles", ",".join(baseline.agents) or "—", ",".join(judge.agents) or "—"),
        _cell("files changed", str(baseline.files_changed), str(judge.files_changed)),
        _cell("diffstat", baseline.diffstat or "—", judge.diffstat or "—"),
    ]
    if baseline.error or judge.error:
        rows.append(_cell("error", baseline.error or "—", judge.error or "—"))
    lines = ["A/B result", *rows, ""]
    lines.append(f"{baseline.name} PR: {baseline.pr_url or '—'}")
    lines.append(f"{judge.name} PR: {judge.pr_url or '—'}")
    lines.append("")
    lines.append(f"{baseline.name} last reply:")
    lines.append(_preview(baseline.last_reply))
    lines.append("")
    lines.append(f"{judge.name} last reply:")
    lines.append(_preview(judge.last_reply))
    cheaper = None
    if baseline.cost or judge.cost:
        if judge.cost < baseline.cost:
            cheaper = f"judge cheaper by ${baseline.cost - judge.cost:.4f}"
        elif baseline.cost < judge.cost:
            cheaper = f"baseline cheaper by ${judge.cost - baseline.cost:.4f}"
        else:
            cheaper = "cost tied"
    fewer = None
    if baseline.total_tokens or judge.total_tokens:
        if judge.total_tokens < baseline.total_tokens:
            fewer = f"judge used {baseline.total_tokens - judge.total_tokens} fewer tokens"
        elif baseline.total_tokens < judge.total_tokens:
            fewer = f"baseline used {judge.total_tokens - baseline.total_tokens} fewer tokens"
        else:
            fewer = "tokens tied"
    if cheaper or fewer:
        lines.append("")
        lines.append("delta: " + "; ".join(part for part in (cheaper, fewer) if part))
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clone main vs this branch, clone a target twice, run the same prompt"
    )
    parser.add_argument("--repo", required=True, help="target git URL or local path")
    parser.add_argument("--prompt", required=True, help="user message submitted to both engines")
    parser.add_argument(
        "--workdir",
        default=".",
        help="empty directory to fill (default: cwd)",
    )
    parser.add_argument(
        "--engine-url",
        default="",
        help="engine git URL or path (default: this checkout, so local commits are included)",
    )
    parser.add_argument("--baseline-ref", default="main", help="engine ref without the judge")
    parser.add_argument(
        "--treatment-ref",
        default="",
        help="engine ref with the judge (default: current branch)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=0.0,
        help="per-side headless wall-clock timeout in seconds; 0 waits until idle",
    )
    parser.add_argument(
        "--job-prefix",
        default="",
        help="prefix for the Grafana/Pushgateway job (job becomes PREFIX-engine)",
    )
    parser.add_argument(
        "--settle",
        choices=("keep", "pr", "merge", "discard"),
        default="pr",
        help="auto-answer for worktree settle (default pr)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workdir = Path(args.workdir).expanduser().resolve()
    try:
        _require_empty(workdir)
    except BenchError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    load_env_sh(ROOT / "env.sh")
    api_key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if not api_key or api_key in PLACEHOLDERS:
        print("set OPENROUTER_API_KEY (env or this checkout's env.sh)", file=sys.stderr)
        return 2
    if not typesafe_api_key_from_env():
        print(
            "set TYPESAFE_API_KEY or TYPESAFE_JEV_API_KEY for the judge side",
            file=sys.stderr,
        )
        return 2
    if not (os.environ.get("ENGINE_PUSHGATEWAY_URL") or "").strip():
        print("warning: ENGINE_PUSHGATEWAY_URL is unset; Grafana will not see these runs", file=sys.stderr)

    logs = workdir / "logs"
    logs.mkdir()
    job = _metrics_job(args.job_prefix)
    run_log = (logs / "run.log").open("w")
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout = _Tee(old_out, run_log)
    sys.stderr = _Tee(old_err, run_log)
    try:
        return _run_bench(args, workdir, logs, job)
    finally:
        sys.stdout = old_out
        sys.stderr = old_err
        run_log.close()


def _run_bench(args, workdir: Path, logs: Path, job: str) -> int:
    engine_url = (args.engine_url or "").strip() or str(ROOT)
    treatment = (args.treatment_ref or "").strip() or _current_ref(ROOT)

    engine_main = workdir / "engine-main"
    engine_judge = workdir / "engine-judge"
    repo_src = workdir / "repo-src"
    ws_baseline = workdir / "ws-baseline"
    ws_judge = workdir / "ws-judge"

    print(f"cloning engine {args.baseline_ref} and {treatment} from {engine_url}")
    _clone(engine_url, engine_main, branch=args.baseline_ref)
    _clone(engine_url, engine_judge, branch=treatment)
    print("installing venvs")
    _setup_venv(engine_main)
    _setup_venv(engine_judge)

    print(f"cloning target {args.repo}")
    _clone(args.repo, repo_src)
    sha = _git(["rev-parse", "HEAD"], cwd=repo_src)
    _clone(str(repo_src), ws_baseline)
    _clone(str(repo_src), ws_judge)
    _point_origin(ws_baseline, args.repo)
    _point_origin(ws_judge, args.repo)
    print(f"target SHA {sha}")
    print(f"Grafana job={job}  instance=baseline | instance=judge")
    print(f"settle={args.settle}")

    baseline_env = _baseline_env(job)
    judge_env = _judge_env(job)
    servers: list[tuple[subprocess.Popen, object, str]] = []
    run_error = ""
    try:
        print("starting engines")
        base_proc, base_log = _start_engine(
            engine_main, ws_baseline, baseline_env, logs / "engine-baseline.log"
        )
        servers.append((base_proc, base_log, "baseline"))
        judge_proc, judge_log = _start_engine(
            engine_judge, ws_judge, judge_env, logs / "engine-judge.log"
        )
        servers.append((judge_proc, judge_log, "judge"))
        _wait_socket(ws_baseline / ".engine" / "engine.sock", base_proc)
        _wait_socket(ws_judge / ".engine" / "engine.sock", judge_proc)
        if args.timeout and args.timeout > 0:
            print(f"submitting prompt to both ({args.timeout:.0f}s timeout)")
        else:
            print("submitting prompt to both (no timeout; wait until idle)")
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {
                pool.submit(
                    _run_client,
                    engine_main,
                    ws_baseline,
                    args.prompt,
                    args.timeout,
                    baseline_env,
                    logs / "client-baseline.log",
                    args.settle,
                ): "baseline",
                pool.submit(
                    _run_client,
                    engine_judge,
                    ws_judge,
                    args.prompt,
                    args.timeout,
                    judge_env,
                    logs / "client-judge.log",
                    args.settle,
                ): "judge",
            }
            errors = []
            for future in as_completed(futures):
                name = futures[future]
                try:
                    future.result()
                    print(f"{name} finished")
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{name}: {exc}")
            if errors:
                run_error = "; ".join(errors)
    finally:
        for proc, log, name in servers:
            print(f"stopping {name}")
            _stop(proc, log)

    baseline = collect_side("baseline", ws_baseline)
    judge = collect_side("judge", ws_judge)
    _write_transcript(ws_baseline, logs / "transcript-baseline.txt")
    _write_transcript(ws_judge, logs / "transcript-judge.txt")
    report = format_report(baseline, judge)
    summary_path = workdir / "summary.txt"
    summary_path.write_text(report + "\n")
    print()
    print(report)
    print()
    print(f"Grafana: job={job}  instance=baseline | instance=judge")
    print(f"workspaces: {ws_baseline}")
    print(f"            {ws_judge}")
    print(f"summary: {summary_path}")
    print(f"logs: {logs}")
    print(f"  run: {logs / 'run.log'}")
    print(f"  events: {logs / 'client-baseline.log'}")
    print(f"          {logs / 'client-judge.log'}")
    print(f"  transcripts: {logs / 'transcript-baseline.txt'}")
    print(f"               {logs / 'transcript-judge.txt'}")
    if run_error:
        raise BenchError(run_error)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(f"command failed: {exc.cmd} (exit {exc.returncode})", file=sys.stderr)
        raise SystemExit(1) from exc
    except BenchError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
