from __future__ import annotations

import asyncio
from pathlib import Path


async def run_git(workspace: Path, *args: str) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(workspace),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    return (
        process.returncode or 0,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


async def collect_git_state(workspace: Path) -> dict[str, object]:
    branch_code, branch_out, _ = await run_git(workspace, "rev-parse", "--abbrev-ref", "HEAD")
    if branch_code != 0:
        return {
            "branch": "",
            "dirty": False,
            "staged_diff": "",
            "unstaged_diff": "",
            "status_lines": [],
        }
    _, status_out, _ = await run_git(workspace, "status", "--porcelain")
    _, staged, _ = await run_git(workspace, "diff", "--cached")
    _, unstaged, _ = await run_git(workspace, "diff")
    lines = [line for line in status_out.splitlines() if line.strip()]
    return {
        "branch": branch_out.strip(),
        "dirty": bool(lines),
        "staged_diff": staged,
        "unstaged_diff": unstaged,
        "status_lines": lines,
    }
