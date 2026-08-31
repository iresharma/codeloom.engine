from __future__ import annotations

import pytest

from runtime.tools.edits import (
    EditError,
    format_unified_diff,
    insert_at_line,
    replace_lines,
    str_replace,
)


def test_str_replace_unique():
    text = "alpha\nbeta\ngamma\n"
    assert str_replace(text, "beta", "BETA") == "alpha\nBETA\ngamma\n"


def test_str_replace_zero_matches():
    with pytest.raises(EditError, match="not found"):
        str_replace("alpha\nbeta\n", "nope", "x")


def test_str_replace_multiple_matches():
    with pytest.raises(EditError, match="2 times"):
        str_replace("foo\nfoo\n", "foo", "bar")


def test_str_replace_whitespace_drift_reports_line_and_does_not_apply():
    text = "def foo():\n    return 1\n"
    with pytest.raises(EditError, match="line 2") as caught:
        str_replace(text, "    return 1  ", "    return 2")
    assert "whitespace differences" in str(caught.value)
    assert "return 2" not in text


def test_replace_lines_and_insert():
    text = "a\nb\nc\n"
    assert replace_lines(text, 2, 2, "B\n") == "a\nB\nc\n"
    assert insert_at_line(text, 2, "x\n") == "a\nx\nb\nc\n"
    assert insert_at_line(text, 4, "d\n") == "a\nb\nc\nd\n"


def test_autojunk_false_small_hunk_on_repetitive_file():
    lines = ["return\n"] * 250
    lines[99] = "x = 1\n"
    old = "".join(lines)
    lines[99] = "x = 2\n"
    new = "".join(lines)
    diff = format_unified_diff(old, new, "mod.py")
    assert "x = 1" in diff
    assert "x = 2" in diff
    assert diff.count("\n@@") == 1
    assert "-x = 1" in diff
    assert "+x = 2" in diff
    assert diff.count("-return") < 20
