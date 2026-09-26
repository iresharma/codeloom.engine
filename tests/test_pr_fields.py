"""Item 1: the PR title and body come from the orchestrator's closing
summary, not from whichever child happened to finish last.

Boundaries mocked: the LLM provider (FakeProvider) and the `gh` binary (a
PATH shim). `git` is real, against real repos under tmp_path, and `git
push` goes to a real local bare remote -- the settle path under test is
exercised end to end.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from agents.orchestrator import Orchestrator
from agents.profile import discover_profiles
from runtime.config import EngineConfig
from runtime.tools.git import (
    PR_TITLE_MAX,
    add_agent_worktree,
    build_pr_body,
    commit_if_dirty,
    pr_title_from_summary,
    top_level_paths,
    uncovered_paths,
    worktree_diff_stat,
)
from tests.fakes import FakeProvider
from tools.registry import discover_tools

# --------------------------------------------------------------------------
# title: first sentence, word boundary, never a raw slice


def test_title_short_summary_kept_whole():
    assert pr_title_from_summary("Cap HTTP redirects at 5") == "Cap HTTP redirects at 5"


def test_title_takes_only_the_first_sentence():
    summary = (
        "Cap HTTP redirects at five hops. Also pinned pyinstaller in the "
        "packaging script."
    )
    assert pr_title_from_summary(summary) == "Cap HTTP redirects at five hops"


def test_title_cuts_on_a_word_boundary_not_mid_word():
    summary = (
        "Add a redirect-loop cap to the HTTP client and pin pyinstaller so "
        "the packaging script is reproducible across machines"
    )
    title = pr_title_from_summary(summary)
    assert len(title) <= PR_TITLE_MAX
    # The regression this replaces: a raw slice lands inside a word.
    sliced = summary[:PR_TITLE_MAX]
    assert sliced == (
        "Add a redirect-loop cap to the HTTP client and pin pyinstaller so the pa"
    )
    assert summary[PR_TITLE_MAX] != " " and not sliced.endswith(" ")
    # Every word in the title is a whole word from the summary.
    words = summary.split()
    assert title.split() == words[: len(title.split())]
    # And the cut happened at a boundary: the next word did not fit.
    consumed = len(title.split())
    assert len(title) + 1 + len(words[consumed]) > PR_TITLE_MAX


def test_title_keeps_a_single_overlong_word_whole():
    word = "x" * 100
    assert pr_title_from_summary(word) == word


def test_title_strips_a_leading_bullet():
    assert pr_title_from_summary("- Cap redirects at 5") == "Cap redirects at 5"
    assert pr_title_from_summary("1. Cap redirects at 5") == "Cap redirects at 5"


def test_title_empty_summary_is_empty():
    assert pr_title_from_summary("") == ""
    assert pr_title_from_summary("   \n  ") == ""


def test_title_multiline_summary_collapses_whitespace():
    assert pr_title_from_summary("Cap\n  redirects\tat 5") == "Cap redirects at 5"


# --------------------------------------------------------------------------
# body: coverage check against the diff stat


def test_top_level_paths_dedupes_in_order():
    assert top_level_paths(["tools/http.py", "tools/web.py", "scripts/build.sh"]) == [
        "tools",
        "scripts",
    ]


def test_uncovered_paths_flags_only_what_the_body_misses():
    body = "Reworked the tools/http.py redirect cap."
    assert uncovered_paths(body, ["tools/http.py", "scripts/build.sh"]) == ["scripts"]


def test_body_appends_files_changed_when_a_path_is_unmentioned():
    body = build_pr_body(
        "Part B: pinned pyinstaller.",
        stat_text=" tools/http.py | 12 ++\n scripts/build.sh | 2 +-",
        changed_paths=["tools/http.py", "scripts/build.sh"],
    )
    assert "Part B: pinned pyinstaller." in body
    assert "## Files changed" in body
    assert "tools" in body.split("## Files changed", 1)[1]


def test_body_omits_files_changed_when_every_path_is_mentioned():
    body = build_pr_body(
        "Changed tools/http.py and scripts/build.sh.",
        stat_text=" tools/http.py | 12 ++",
        changed_paths=["tools/http.py", "scripts/build.sh"],
    )
    assert "## Files changed" not in body


def test_body_falls_back_to_task_plus_stat_without_a_summary():
    body = build_pr_body(
        "",
        task="Harden the HTTP client and make packaging reproducible.",
        stat_text=" tools/http.py | 12 ++",
        changed_paths=["tools/http.py"],
    )
    assert "Harden the HTTP client" in body
    assert "tools/http.py | 12 ++" in body


def test_body_lists_followups_section():
    body = build_pr_body(
        "Did the thing.",
        followups=["Split the retry helper out of http.py", ""],
    )
    assert "## Follow-ups" in body
    assert "- Split the retry helper out of http.py" in body
    assert body.count("- ") == 1


# --------------------------------------------------------------------------
# real git: the diff stat a settle builds coverage from


def _init_git(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, capture_output=True)
    for key, value in (("user.email", "t@t.t"), ("user.name", "t")):
        subprocess.run(
            ["git", "config", key, value], cwd=path, check=True, capture_output=True
        )
    (path / "README").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"], cwd=path, check=True, capture_output=True
    )


def test_worktree_diff_stat_reports_both_parts(tmp_path):
    repo = tmp_path / "repo"
    _init_git(repo)
    dest, branch, err = add_agent_worktree(repo, "a1", "coder")
    assert not err, err
    work = Path(dest)
    (work / "tools").mkdir()
    (work / "tools" / "http.py").write_text("cap = 5\n")
    commit_if_dirty(work, "part A")
    (work / "scripts").mkdir()
    (work / "scripts" / "build.sh").write_text("pyinstaller==6.6.0\n")
    commit_if_dirty(work, "part B")

    stat_text, paths = worktree_diff_stat(repo, work)
    assert "tools/http.py" in stat_text
    assert "scripts/build.sh" in stat_text
    assert sorted(paths) == ["scripts/build.sh", "tools/http.py"]


def test_worktree_diff_stat_empty_outside_a_repo(tmp_path):
    assert worktree_diff_stat(tmp_path, tmp_path / "nope") == ("", [])


# --------------------------------------------------------------------------
# end to end: two children, one PR


def _gh_shim(bin_dir: Path, record: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "gh"
    script.write_text(
        "#!/bin/sh\n"
        f'python3 -c "import json,sys;open(sys.argv[1],\'w\').write(json.dumps(sys.argv[2:]))" '
        f'"{record}" "$@"\n'
        "echo https://github.com/o/r/pull/7\n"
    )
    script.chmod(0o755)


def _orch(repo: Path) -> Orchestrator:
    return Orchestrator(
        FakeProvider(),
        all_tools=discover_tools(),
        profiles=discover_profiles(),
        workspace=repo,
        config=EngineConfig(),
    )


def _seed_passing_verify(orch: Orchestrator, dest: Path) -> None:
    """Settle refuses a PR on an unverified tree (item 3). These cases are
    about *which text* ends up in the PR, so they start from the state the
    real pipeline reaches: a harness verify that already passed."""
    from runtime.verify import VerifyResult

    orch._verify_by_tree[str(dest.resolve())] = VerifyResult(
        command="pytest -q",
        exit_code=0,
        runner="pytest",
        passed=12,
        failed=0,
        skipped=0,
        source="detected",
    )


@pytest.mark.asyncio
async def test_settle_pr_body_covers_both_children(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_git(repo)
    remote = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(remote)],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    record = tmp_path / "gh_args.json"
    _gh_shim(tmp_path / "bin", record)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("ENGINE_METRICS_INSTANCE", raising=False)

    orch = _orch(repo)
    dest, branch, err = add_agent_worktree(repo, "a1", "coder")
    assert not err, err
    work = Path(dest)
    (work / "tools").mkdir()
    (work / "tools" / "http.py").write_text("cap = 5\n")
    commit_if_dirty(work, "part A")
    (work / "scripts").mkdir()
    (work / "scripts" / "build.sh").write_text("pyinstaller==6.6.0\n")
    commit_if_dirty(work, "part B")
    orch._remember_worktree("a1", work, branch, "coder", "batch")
    _seed_passing_verify(orch, work)

    # The orchestrator's own closing summary after both coders finished.
    orch._user_task = "Harden the HTTP client and make packaging reproducible."
    orch._closing_summary = (
        "Part A: capped HTTP redirects at five hops in tools/http.py and added a "
        "depth counter. Part B: pinned pyinstaller in scripts/build.sh so the "
        "packaging script is reproducible."
    )
    # What the LAST child reported -- part B only. This must not become the PR.
    last_child_report = "pinned pyinstaller in the packaging script"
    orch._worktree_summaries["a1"] = last_child_report

    reply = await orch.apply_named_worktree("pr", agent_id="a1")
    assert reply.startswith("ok pr "), reply

    import json

    args = json.loads(record.read_text())
    title = args[args.index("--title") + 1]
    body = args[args.index("--body") + 1]

    assert "Part A" in body and "Part B" in body
    assert body != last_child_report
    assert last_child_report not in title
    # Title: a word-boundary prefix of the orchestrator's first sentence.
    assert len(title) <= PR_TITLE_MAX
    words = orch._closing_summary.split(". ", 1)[0].split()
    assert title.split() == words[: len(title.split())]
    assert len(title) + 1 + len(words[len(title.split())]) > PR_TITLE_MAX


@pytest.mark.asyncio
async def test_settle_pr_appends_files_changed_when_summary_omits_one(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    _init_git(repo)
    remote = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(remote)],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    record = tmp_path / "gh_args.json"
    _gh_shim(tmp_path / "bin", record)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("ENGINE_METRICS_INSTANCE", raising=False)

    orch = _orch(repo)
    dest, branch, err = add_agent_worktree(repo, "a2", "coder")
    assert not err, err
    work = Path(dest)
    (work / "tools").mkdir()
    (work / "tools" / "http.py").write_text("cap = 5\n")
    (work / "scripts").mkdir()
    (work / "scripts" / "build.sh").write_text("pyinstaller==6.6.0\n")
    commit_if_dirty(work, "both")
    orch._remember_worktree("a2", work, branch, "coder", "batch")
    _seed_passing_verify(orch, work)

    orch._closing_summary = "Capped HTTP redirects at five hops in tools/http.py."
    reply = await orch.apply_named_worktree("pr", agent_id="a2")
    assert reply.startswith("ok pr "), reply

    import json

    body = json.loads(record.read_text())
    body = body[body.index("--body") + 1]
    assert "## Files changed" in body
    tail = body.split("## Files changed", 1)[1]
    assert "scripts" in tail
    assert "build.sh" in tail


@pytest.mark.asyncio
async def test_pr_fields_fall_back_to_the_user_task_not_the_child_report(tmp_path):
    repo = tmp_path / "repo"
    _init_git(repo)
    orch = _orch(repo)
    dest, branch, err = add_agent_worktree(repo, "a3", "coder")
    assert not err, err
    work = Path(dest)
    (work / "tools").mkdir()
    (work / "tools" / "http.py").write_text("cap = 5\n")
    commit_if_dirty(work, "work")

    orch._user_task = "Cap HTTP redirects and pin the packaging toolchain."
    orch._closing_summary = ""
    title, body = orch._pr_fields(work, "child said something else entirely")
    assert title == "Cap HTTP redirects and pin the packaging toolchain"
    assert "Cap HTTP redirects and pin the packaging toolchain" in body
    assert "child said something else entirely" not in body
    assert "tools/http.py" in body


@pytest.mark.asyncio
async def test_run_records_the_closing_summary_and_the_user_task(tmp_path):
    repo = tmp_path / "repo"
    _init_git(repo)
    orch = _orch(repo)

    from llm.provider import LLMResult

    orch._llm = FakeProvider(
        results=[
            LLMResult(text="Part A done."),
            LLMResult(text="Part A and part B are both done."),
        ]
    )
    await orch.run("do two things")
    assert orch._user_task == "do two things"
    assert orch.closing_summary() == "Part A done."

    # The inbox turn after the last child must NOT overwrite the user task,
    # but it is the turn whose reply settles the PR.
    await orch.run("[agent coder abcd1234 finished]\npinned pyinstaller")
    assert orch._user_task == "do two things"
    assert orch.closing_summary() == "Part A and part B are both done."
