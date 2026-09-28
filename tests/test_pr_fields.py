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


def test_title_uses_what_block_not_paths_label():
    summary = "paths: None changed\nwhat: add a web workspace mock\nverdict: ok\n"
    assert pr_title_from_summary(summary) == "add a web workspace mock"


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


# --------------------------------------------------------------------------
# which part of an orchestrator reply may become the PR text
#
# The three shapes below are the real pre-settle replies from the trial
# re-run, trimmed: a preamble, a summary set apart in some way, and a closing
# question. Nobody answers in a headless run, so the last reply is what the PR
# got -- question included.

from runtime.tools.git import pr_summary_from_reply  # noqa: E402

REPLY_BLOCKQUOTE = """Everything is complete and verified clean.

**PR-ready summary:**

> **Add authentication in front of the collector's UI and API**
>
> The collector's README warned there was no auth. This closes that gap: the UI, query API and static assets use Basic Auth while ingest uses its own bearer token.

Would you like me to **merge** this, **open a PR**, or keep the worktree?"""

REPLY_RULED = """The reviewer approved.

Here is the PR summary, as requested:

---
**Harden `http_request` against real-world HTTP failures, and investigate packaging**

Part A hardens raw_request with timeouts, TLS errors and bounded retries. Part B adds a PyInstaller build script and a packaging doc.

---

Would you like me to merge, open a PR, or keep the worktree?"""

REPLY_STATUS_AND_QUESTION = """The reviewer stopped without completing.

Here's where things stand:
- Feature implemented, build and vet pass.

Would you like me to:
1. Try the review again, or
2. Skip re-review and settle?"""


def test_a_blockquoted_summary_is_extracted_and_the_question_dropped():
    out = pr_summary_from_reply(REPLY_BLOCKQUOTE)
    assert out.startswith("**Add authentication in front of the collector")
    assert "Basic Auth" in out
    assert "Would you like" not in out
    assert "verified clean" not in out


def test_a_ruled_block_is_extracted_and_the_question_dropped():
    out = pr_summary_from_reply(REPLY_RULED)
    assert out.startswith("**Harden `http_request`")
    assert "PyInstaller" in out
    assert "Would you like" not in out
    assert "as requested" not in out


def test_a_status_update_that_asks_the_user_is_not_a_summary():
    assert pr_summary_from_reply(REPLY_STATUS_AND_QUESTION) == ""


def test_a_plain_reply_that_never_addresses_the_user_is_kept_whole():
    text = "Part A and part B are both done. The build passes."
    assert pr_summary_from_reply(text) == text


@pytest.mark.parametrize(
    "reply",
    [
        "Done. Let me know if you want anything changed.",
        "Ready. Shall I open the PR?",
        "Which branch should this target?",
        "Should this be merged\n?",
    ],
)
def test_replies_that_address_the_user_are_not_summaries(reply):
    assert pr_summary_from_reply(reply) == ""


def test_the_longest_set_apart_block_wins():
    reply = (
        "Notes:\n> short aside that is long enough to pass the length floor here\n\n"
        "---\nThe real, longer description of what changed and why, in a full "
        "paragraph that clearly outweighs the aside above.\n---\n"
    )
    assert pr_summary_from_reply(reply).startswith("The real, longer description")


def test_a_set_apart_block_that_itself_asks_a_question_is_skipped():
    reply = "---\nShould I merge this change into the current branch now?\n---\n"
    assert pr_summary_from_reply(reply) == ""


def test_empty_reply_is_empty():
    assert pr_summary_from_reply("") == ""
    assert pr_summary_from_reply("   \n") == ""


def test_a_bold_first_line_is_used_whole_as_the_title():
    summary = "**Harden `http_request` and investigate packaging**\n\nBody text here."
    assert pr_title_from_summary(summary) == "Harden `http_request` and investigate packaging"
    assert pr_title_from_summary("# Cap redirects\n\nBody.") == "Cap redirects"
    # A bold *phrase inside* a sentence is not a title line.
    assert pr_title_from_summary("Added **auth** to the collector. More.") == (
        "Added **auth** to the collector"
    )


@pytest.mark.asyncio
async def test_run_keeps_only_the_summary_from_a_chatty_reply(tmp_path):
    repo = tmp_path / "repo"
    _init_git(repo)
    orch = _orch(repo)
    from llm.provider import LLMResult

    orch._llm = FakeProvider(results=[LLMResult(text=REPLY_BLOCKQUOTE)])
    await orch.run("Add auth to the collector.")
    assert orch.closing_summary().startswith("**Add authentication in front")
    assert "Would you like" not in orch.closing_summary()


@pytest.mark.asyncio
async def test_a_reply_that_asks_the_user_leaves_no_closing_summary(tmp_path):
    repo = tmp_path / "repo"
    _init_git(repo)
    orch = _orch(repo)
    from llm.provider import LLMResult

    orch._llm = FakeProvider(results=[LLMResult(text=REPLY_STATUS_AND_QUESTION)])
    await orch.run("Add redis caching for the kanban endpoints.")
    assert orch.closing_summary() == ""


@pytest.mark.asyncio
async def test_pr_falls_back_to_the_task_when_the_reply_only_asked_a_question(tmp_path):
    """The reach-auth-proxy shape end to end: the PR must not carry the
    orchestrator's question."""
    repo = tmp_path / "repo"
    _init_git(repo)
    orch = _orch(repo)
    from llm.provider import LLMResult

    dest, branch, err = add_agent_worktree(repo, "q1", "coder")
    assert not err, err
    work = Path(dest)
    (work / "kanban.go").write_text("package routes\n")
    commit_if_dirty(work, "work")

    orch._llm = FakeProvider(results=[LLMResult(text=REPLY_STATUS_AND_QUESTION)])
    await orch.run("Add redis caching for the kanban read endpoints.")
    title, body = orch._pr_fields(work, "coder said something else")
    assert title == "Add redis caching for the kanban read endpoints"
    assert "Would you like" not in body
    assert "reviewer stopped" not in body
    assert "Add redis caching for the kanban read endpoints." in body
    assert "kanban.go" in body


@pytest.mark.asyncio
async def test_pr_uses_the_extracted_summary_end_to_end(tmp_path):
    repo = tmp_path / "repo"
    _init_git(repo)
    orch = _orch(repo)
    from llm.provider import LLMResult

    dest, branch, err = add_agent_worktree(repo, "q2", "coder")
    assert not err, err
    work = Path(dest)
    (work / "auth.go").write_text("package collector\n")
    commit_if_dirty(work, "work")

    orch._llm = FakeProvider(results=[LLMResult(text=REPLY_RULED)])
    await orch.run("Harden http_request and investigate packaging.")
    title, body = orch._pr_fields(work, "coder said something else")
    assert title.startswith("Harden `http_request` against real-world HTTP failures")
    assert len(title) <= PR_TITLE_MAX
    assert "PyInstaller" in body
    assert "Would you like" not in body


# --------------------------------------------------------------------------
# generated PR text: one model call writes title and body from the facts

from runtime.tools.git import parse_generated_pr  # noqa: E402


def test_parse_generated_accepts_bare_json():
    out = parse_generated_pr('{"title": "Cap redirects at five hops", "body": "Adds a cap."}')
    assert out == ("Cap redirects at five hops", "Adds a cap.")


def test_parse_generated_accepts_fenced_json_and_a_wrapping_sentence():
    fenced = 'Here you go:\n```json\n{"title": "Add auth", "body": "Basic auth."}\n```'
    assert parse_generated_pr(fenced) == ("Add auth", "Basic auth.")


def test_parse_generated_cuts_a_long_title_on_a_word_boundary():
    title = "Add a redirect-loop cap to the HTTP client and pin pyinstaller so the packaging is reproducible"
    out = parse_generated_pr(json.dumps({"title": title, "body": "Body."}))
    assert out is not None
    assert len(out[0]) <= PR_TITLE_MAX
    assert title.startswith(out[0])
    assert out[0].endswith("the")


@pytest.mark.parametrize(
    "reply",
    [
        "",
        "no json here",
        "{not json}",
        '{"title": "T"}',
        '{"body": "B"}',
        '{"title": "", "body": "B"}',
        '["title", "body"]',
        '{"title": "T", "body": "Would you like me to merge this?"}',
        '{"title": "T", "body": "Done.\\n\\nShould I open the PR?"}',
    ],
)
def test_parse_generated_rejects_unusable_replies(reply):
    assert parse_generated_pr(reply) is None


import json  # noqa: E402


class _PRWriter(FakeProvider):
    """Answers the PR-text call, and records what it was shown."""

    def __init__(self, reply: str | Exception):
        super().__init__()
        self._reply = reply
        self.prompts: list[list[dict]] = []

    async def complete(self, messages, tools=None, *, on_delta=None, **kwargs):
        from llm.provider import LLMResult

        self.calls += 1
        self.prompts.append(messages)
        if isinstance(self._reply, Exception):
            raise self._reply
        return LLMResult(text=self._reply)


def _settled_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_git(repo)
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True, capture_output=True
    )
    record = tmp_path / "gh.json"
    _gh_shim(tmp_path / "bin", record)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    return repo, record


def _tree(repo: Path, orch: Orchestrator, agent_id: str) -> Path:
    dest, branch, err = add_agent_worktree(repo, agent_id, "coder")
    assert not err, err
    work = Path(dest)
    (work / "tools").mkdir()
    (work / "tools" / "http.py").write_text("cap = 5\n")
    (work / "scripts").mkdir()
    (work / "scripts" / "build.sh").write_text("pyinstaller==6.6.0\n")
    commit_if_dirty(work, "both parts")
    orch._remember_worktree(agent_id, work, branch, "coder", "batch")
    return work


def _gh_args(record: Path) -> tuple[str, str]:
    args = json.loads(record.read_text())
    return args[args.index("--title") + 1], args[args.index("--body") + 1]


@pytest.mark.asyncio
async def test_settle_uses_the_generated_title_and_body(tmp_path, monkeypatch):
    repo, record = _settled_repo(tmp_path, monkeypatch)
    writer = _PRWriter(
        json.dumps(
            {
                "title": "Cap HTTP redirects and pin the packaging toolchain",
                "body": "Caps redirects at five hops in tools/http.py and pins "
                "pyinstaller in scripts/build.sh so the build is reproducible.",
            }
        )
    )
    orch = Orchestrator(
        writer, all_tools=discover_tools(), profiles=discover_profiles(),
        workspace=repo, config=EngineConfig(),
    )
    work = _tree(repo, orch, "p1")
    orch._user_task = "Harden the HTTP client and make packaging reproducible."
    # The orchestrator's own reply is chatter that asks a question: unusable.
    orch._closing_summary = ""
    orch._writer_reports[str(work.resolve())] = ["coder (ok): capped redirects; pinned pyinstaller"]

    reply = await orch.apply_named_worktree("pr", agent_id="p1")
    assert reply.startswith("ok pr "), reply
    title, body = _gh_args(record)
    assert title == "Cap HTTP redirects and pin the packaging toolchain"
    assert "Caps redirects at five hops" in body
    assert "Would you like" not in body


@pytest.mark.asyncio
async def test_the_generation_prompt_carries_the_facts(tmp_path, monkeypatch):
    repo, record = _settled_repo(tmp_path, monkeypatch)
    writer = _PRWriter('{"title": "T", "body": "B"}')
    orch = Orchestrator(
        writer, all_tools=discover_tools(), profiles=discover_profiles(),
        workspace=repo, config=EngineConfig(),
    )
    work = _tree(repo, orch, "p2")
    orch._user_task = "Harden the HTTP client. Also make packaging reproducible."
    orch._writer_reports[str(work.resolve())] = ["coder (ok): capped redirects"]
    await orch.apply_named_worktree("pr", agent_id="p2")

    assert len(writer.prompts) == 1
    system, user = writer.prompts[0][0]["content"], writer.prompts[0][1]["content"]
    assert "no questions" in system
    assert "Harden the HTTP client. Also make packaging reproducible." in user
    assert "tools/http.py" in user and "scripts/build.sh" in user  # the diff stat
    assert "coder (ok): capped redirects" in user
    assert "VERIFICATION" not in user


@pytest.mark.asyncio
async def test_a_generated_body_that_omits_a_changed_path_still_gets_files_changed(
    tmp_path, monkeypatch
):
    repo, record = _settled_repo(tmp_path, monkeypatch)
    writer = _PRWriter('{"title": "Cap redirects", "body": "Caps redirects in tools/http.py."}')
    orch = Orchestrator(
        writer, all_tools=discover_tools(), profiles=discover_profiles(),
        workspace=repo, config=EngineConfig(),
    )
    _tree(repo, orch, "p3")
    await orch.apply_named_worktree("pr", agent_id="p3")
    _title, body = _gh_args(record)
    assert "## Files changed" in body
    assert "scripts" in body.split("## Files changed", 1)[1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [
        RuntimeError("provider down"),
        "sorry, I cannot do that",
        '{"title": "T", "body": "Would you like me to open the PR?"}',
    ],
)
async def test_a_failed_or_unusable_generation_falls_back_to_the_deterministic_text(
    tmp_path, monkeypatch, reply
):
    repo, record = _settled_repo(tmp_path, monkeypatch)
    orch = Orchestrator(
        _PRWriter(reply), all_tools=discover_tools(), profiles=discover_profiles(),
        workspace=repo, config=EngineConfig(),
    )
    _tree(repo, orch, "p4")
    orch._user_task = "Harden the HTTP client and make packaging reproducible."
    orch._closing_summary = ""

    result = await orch.apply_named_worktree("pr", agent_id="p4")
    assert result.startswith("ok pr "), result  # a bad reply never fails a settle
    title, body = _gh_args(record)
    assert title == "Harden the HTTP client and make packaging reproducible"
    assert "Task:" in body and "tools/http.py" in body
