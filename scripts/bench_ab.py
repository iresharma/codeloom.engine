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
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from llm.openrouter import PLACEHOLDERS, load_env_sh  # noqa: E402
from runtime.config import typesafe_api_key_from_env  # noqa: E402


class BenchError(RuntimeError):
    pass


def _git(args: list[str], *, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return (result.stdout or "").strip()


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


def _base_env() -> dict[str, str]:
    env = os.environ.copy()
    env["ENGINE_METRICS_JOB"] = env.get("ENGINE_METRICS_JOB") or "engine"
    env["ENGINE_MAX_CONTINUES"] = "1"
    env.pop("ENGINE_TURN_CONTINUE", None)
    return env


def _baseline_env() -> dict[str, str]:
    env = _base_env()
    env["ENGINE_JUDGE"] = "off"
    env["ENGINE_EXEC_APPROVAL"] = "auto"
    env["ENGINE_METRICS_INSTANCE"] = "baseline"
    return env


def _judge_env() -> dict[str, str]:
    env = _base_env()
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
    timeout: float,
    env: dict[str, str],
    log_path: Path,
) -> None:
    python = _venv_python(engine_dir)
    with log_path.open("w") as log:
        result = subprocess.run(
            [
                str(python),
                str(engine_dir / "dummy_client.py"),
                str(workspace),
                "--message",
                prompt,
                "--auto",
                "--timeout",
                str(timeout),
            ],
            cwd=engine_dir,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    if result.returncode != 0:
        raise BenchError(
            f"headless client for {workspace.name} exited {result.returncode} "
            f"(see {log_path})"
        )


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
        default=1800.0,
        help="per-side headless idle timeout in seconds",
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

    engine_url = (args.engine_url or "").strip() or str(ROOT)
    treatment = (args.treatment_ref or "").strip() or _current_ref(ROOT)

    engine_main = workdir / "engine-main"
    engine_judge = workdir / "engine-judge"
    repo_src = workdir / "repo-src"
    ws_baseline = workdir / "ws-baseline"
    ws_judge = workdir / "ws-judge"
    logs = workdir / "logs"
    logs.mkdir()

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
    print(f"target SHA {sha}")

    baseline_env = _baseline_env()
    judge_env = _judge_env()
    servers: list[tuple[subprocess.Popen, object, str]] = []
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
        print(f"submitting prompt to both ({args.timeout:.0f}s timeout)")
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
                ): "baseline",
                pool.submit(
                    _run_client,
                    engine_judge,
                    ws_judge,
                    args.prompt,
                    args.timeout,
                    judge_env,
                    logs / "client-judge.log",
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
                raise BenchError("; ".join(errors))
    finally:
        for proc, log, name in servers:
            print(f"stopping {name}")
            _stop(proc, log)

    print()
    print("Grafana: job=engine  instance=baseline | instance=judge")
    print(f"workspaces: {ws_baseline}")
    print(f"            {ws_judge}")
    print("inspect quality with git diff in each clone and under .engine/worktrees/")
    print(f"logs: {logs}")
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
