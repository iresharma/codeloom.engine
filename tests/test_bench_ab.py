from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load():
    path = Path(__file__).resolve().parents[1] / "scripts" / "bench_ab.py"
    spec = importlib.util.spec_from_file_location("bench_ab", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_require_empty_accepts_missing_and_blank(tmp_path):
    mod = _load()
    missing = tmp_path / "fresh"
    mod._require_empty(missing)
    assert missing.is_dir()
    mod._require_empty(missing)


def test_require_empty_rejects_files(tmp_path):
    mod = _load()
    (tmp_path / "leftover").write_text("x\n")
    with pytest.raises(mod.BenchError, match="not empty"):
        mod._require_empty(tmp_path)


def test_collect_side_without_session(tmp_path):
    mod = _load()
    result = mod.collect_side("baseline", tmp_path)
    assert result.error == "no session in sqlite"
    assert result.cost == 0
    assert result.name == "baseline"


def test_format_report_compares_sides():
    mod = _load()
    baseline = mod.SideResult(
        name="baseline",
        session_id="aaaaaaaaaaaa",
        cost=0.20,
        total_tokens=1000,
        prompt_tokens=800,
        completion_tokens=200,
        cached_tokens=10,
        requests=4,
        turns=6,
        tool_calls=12,
        elapsed_s=30.0,
        agents=["coder"],
        files_changed=2,
        diffstat="2 files changed, 10 insertions(+)",
        last_reply="I added the test.",
        pr_url="https://github.com/org/r/pull/1",
    )
    judge = mod.SideResult(
        name="judge",
        session_id="bbbbbbbbbbbb",
        cost=0.10,
        total_tokens=700,
        prompt_tokens=500,
        completion_tokens=200,
        cached_tokens=20,
        requests=3,
        turns=4,
        tool_calls=8,
        elapsed_s=22.5,
        agents=["coder", "tester"],
        files_changed=1,
        diffstat="1 file changed, 4 insertions(+)",
        last_reply="Done.",
        pr_url="https://github.com/org/r/pull/2",
    )
    text = mod.format_report(baseline, judge)
    assert "A/B result" in text
    assert "0.2000" in text and "0.1000" in text
    assert "judge cheaper" in text
    assert "300 fewer tokens" in text
    assert "I added the test." in text
    assert "coder,tester" in text
    assert "https://github.com/org/r/pull/1" in text
    assert "https://github.com/org/r/pull/2" in text
    assert "baseline PR:" in text
    assert "judge PR:" in text


def test_timeout_defaults_to_unlimited(monkeypatch):
    mod = _load()
    monkeypatch.setattr(
        sys,
        "argv",
        ["bench_ab.py", "--repo", "git@example.com/x.git", "--prompt", "do it"],
    )
    args = mod.parse_args()
    assert args.timeout == 0
    assert args.settle == "pr"
    assert args.job_prefix == ""


def test_job_prefix_and_settle_flags(monkeypatch):
    mod = _load()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bench_ab.py",
            "--repo",
            "git@example.com/x.git",
            "--prompt",
            "do it",
            "--job-prefix",
            "tui settings",
            "--settle",
            "keep",
        ],
    )
    args = mod.parse_args()
    assert args.settle == "keep"
    assert mod._metrics_job(args.job_prefix) == "tui-settings-engine"


def test_metrics_job_prefix():
    mod = _load()
    assert mod._metrics_job("") == "engine"
    assert mod._metrics_job("ab") == "ab-engine"
    assert mod._metrics_job("  tui-run  ") == "tui-run-engine"


def test_pr_urls_from_engine_messages():
    mod = _load()

    class Msg:
        def __init__(self, text):
            self.text = text

    class Snap:
        messages = [
            Msg(
                "[worktree coder 0c578a61 pr]\n"
                "opened pull request for engine/coder/abc: "
                "https://github.com/org/r/pull/12"
            ),
            Msg("Pull request opened: **https://github.com/org/r/pull/12**"),
        ]

    assert mod._pr_urls(Snap()) == "https://github.com/org/r/pull/12"


def test_last_assistant_skips_abort_reply():
    mod = _load()

    class Msg:
        def __init__(self, role, text):
            self.role = role
            self.text = text

    class Snap:
        messages = [
            Msg("assistant", "Reviewer asked for two small fixes, opening a PR."),
            Msg("assistant", "(aborted by the user)"),
        ]

    assert mod._last_assistant(Snap()) == (
        "Reviewer asked for two small fixes, opening a PR."
    )


def test_client_timeout_arg_uses_long_fuse_when_unlimited():
    mod = _load()
    assert mod._client_timeout_arg(0) == str(24 * 60 * 60)
    assert mod._client_timeout_arg(None) == str(24 * 60 * 60)
    assert mod._client_timeout_arg(1800.0) == "1800.0"
