from __future__ import annotations

from agents.diagnostics import (
    CLEAN,
    ERRORS,
    FAILED,
    classify_diagnostics_output,
    diagnostics_gaps,
    format_diagnostics_gaps,
    normalize_rel_path,
)


def test_normalize_rel_path_strips_dot_slash():
    assert normalize_rel_path("./src/app.tsx") == "src/app.tsx"
    assert normalize_rel_path("src/app.tsx") == "src/app.tsx"
    assert normalize_rel_path("  ././a.py") == "a.py"


def test_classify_clean_empty_list():
    assert classify_diagnostics_output("No diagnostics for 'a.py' (clean).") == CLEAN


def test_classify_warnings_are_clean():
    text = "src/app.tsx:1:2 Warning: unused\nsrc/app.tsx:3:1 Hint: prefer const"
    assert classify_diagnostics_output(text) == CLEAN


def test_classify_error_severity_is_dirty():
    text = "src/app.tsx:4:9 Error: Unexpected token"
    assert classify_diagnostics_output(text) == ERRORS


def test_classify_mixed_error_and_warning_is_dirty():
    text = (
        "src/app.tsx:1:1 Warning: unused\n"
        "src/app.tsx:4:9 Error: Unexpected token"
    )
    assert classify_diagnostics_output(text) == ERRORS


def test_classify_tool_failure():
    assert classify_diagnostics_output(
        "error: LSP is not available for this workspace "
        "(need python, go, or javascript/typescript)"
    ) == FAILED
    assert classify_diagnostics_output("") == FAILED
    assert classify_diagnostics_output("garbage") == FAILED


def test_gaps_skip_non_code_and_clean_files():
    gaps = diagnostics_gaps(
        ["README.md", "src/app.tsx", "src/ok.py"],
        {"src/ok.py": CLEAN},
    )
    assert gaps == [("src/app.tsx", "not checked after last edit")]


def test_gaps_report_errors_and_failures():
    gaps = diagnostics_gaps(
        ["a.py", "b.py", "./a.py"],
        {"a.py": ERRORS, "b.py": FAILED},
    )
    assert gaps == [
        ("a.py", "Error diagnostics"),
        ("b.py", "get_diagnostics failed"),
    ]
    assert "a.py (Error diagnostics)" in format_diagnostics_gaps(gaps)
