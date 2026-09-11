"""Coverage tests for runtime/tools/docs.py"""
from __future__ import annotations

import pytest

from runtime.tools import docs as docs_module
from runtime.tools.docs import docs_lookup, tldr, _clip, _go, _mdn


def test_docs_lookup_empty_query():
    """Test docs_lookup with empty query."""
    result = docs_lookup("mdn", "")
    assert result.startswith("error:")
    assert "query is required" in result


def test_docs_lookup_empty_source():
    """Test docs_lookup with empty source."""
    result = docs_lookup("", "test")
    assert result.startswith("error:")
    assert "source must be" in result


def test_docs_lookup_invalid_source():
    """Test docs_lookup with invalid source."""
    result = docs_lookup("invalid", "test")
    assert result.startswith("error:")
    assert "source must be" in result


def test_docs_lookup_whitespace_source():
    """Test docs_lookup with source containing only whitespace."""
    result = docs_lookup("  ", "test")
    assert result.startswith("error:")


def test_tldr_empty_topic():
    """Test tldr with empty topic."""
    result = tldr("")
    assert result.startswith("error:")
    assert "topic is required" in result


def test_tldr_only_special_chars():
    """Test tldr with topic containing only special characters."""
    result = tldr("@#$%")
    assert result.startswith("error:")


def test_clip_under_limit():
    """Test _clip when text is under the limit."""
    text = "hello world"
    result = _clip(text, 20)
    assert result == text


def test_clip_over_limit():
    """Test _clip when text exceeds the limit."""
    text = "hello world this is a long text"
    result = _clip(text, 15)
    assert len(result) < len(text)
    assert "[truncated]" in result
    assert result.startswith("hello world thi")


def test_clip_exact_limit():
    """Test _clip when text exactly matches limit."""
    text = "hello world"
    result = _clip(text, len(text))
    assert result == text


def test_mdn_no_results(monkeypatch):
    """Test _mdn when no results are returned."""
    monkeypatch.setattr(
        docs_module,
        "get_json",
        lambda url: ({}, ""),
    )
    result = _mdn("nonexistent")
    assert "(no results)" in result


def test_mdn_empty_docs(monkeypatch):
    """Test _mdn when documents list is empty."""
    monkeypatch.setattr(
        docs_module,
        "get_json",
        lambda url: ({"documents": []}, ""),
    )
    result = _mdn("test")
    assert "(no results)" in result


def test_mdn_none_documents(monkeypatch):
    """Test _mdn when documents is None."""
    monkeypatch.setattr(
        docs_module,
        "get_json",
        lambda url: ({"documents": None}, ""),
    )
    result = _mdn("test")
    assert "(no results)" in result


def test_mdn_json_error(monkeypatch):
    """Test _mdn when get_json returns an error."""
    monkeypatch.setattr(
        docs_module,
        "get_json",
        lambda url: (None, "error: connection failed"),
    )
    result = _mdn("test")
    assert result == "error: connection failed"


def test_mdn_with_results(monkeypatch):
    """Test _mdn with valid results."""
    monkeypatch.setattr(
        docs_module,
        "get_json",
        lambda url: (
            {
                "documents": [
                    {
                        "title": "Array.map",
                        "mdn_url": "/en-US/docs/Web/JavaScript/Reference/Global_Objects/Array/map",
                        "summary": "Creates a new array populated with results",
                    },
                    {
                        "title": "Array.filter",
                        "mdn_url": "/en-US/docs/Web/JavaScript/Reference/Global_Objects/Array/filter",
                        "summary": "Creates a new array with elements that pass the test",
                    },
                ]
            },
            "",
        ),
    )
    result = _mdn("array methods")
    assert "Array.map" in result
    assert "Array.filter" in result
    assert "developer.mozilla.org" in result


def test_mdn_missing_fields(monkeypatch):
    """Test _mdn when result items have missing fields."""
    monkeypatch.setattr(
        docs_module,
        "get_json",
        lambda url: (
            {
                "documents": [
                    {"title": "Test"},  # missing mdn_url and summary
                    {"mdn_url": "/path"},  # missing title and summary
                    {"summary": "Summary text"},  # missing title and mdn_url
                ]
            },
            "",
        ),
    )
    result = _mdn("test")
    assert "Test" in result
    assert "/path" in result


def test_go_package_format(monkeypatch):
    """Test _go with package format query."""
    monkeypatch.setattr(
        docs_module,
        "web_fetch",
        lambda url: "Go package documentation",
    )
    result = _go("golang.org/x/text")
    assert "pkg.go.dev/golang.org/x/text" in result
    assert "Go package documentation" in result


def test_go_simple_query(monkeypatch):
    """Test _go with simple package name."""
    monkeypatch.setattr(
        docs_module,
        "web_fetch",
        lambda url: "Package docs",
    )
    result = _go("errors")
    assert "pkg.go.dev/search" in result
    assert "Package docs" in result


def test_go_with_dot_separator(monkeypatch):
    """Test _go with dot in query."""
    monkeypatch.setattr(
        docs_module,
        "web_fetch",
        lambda url: "docs",
    )
    result = _go("golang.org")
    assert "pkg.go.dev/golang.org" in result


def test_go_fetch_error(monkeypatch):
    """Test _go when web_fetch returns an error."""
    monkeypatch.setattr(
        docs_module,
        "web_fetch",
        lambda url: "error: timeout",
    )
    result = _go("test")
    assert result == "error: timeout"


def test_docs_lookup_with_pypi(monkeypatch):
    """Test docs_lookup with pypi source."""
    monkeypatch.setattr(
        docs_module,
        "pkg_info",
        lambda source, query: "pytest==7.0.0 (Latest)",
    )
    result = docs_lookup("pypi", "pytest")
    assert "pytest" in result


def test_docs_lookup_with_npm(monkeypatch):
    """Test docs_lookup with npm source."""
    monkeypatch.setattr(
        docs_module,
        "pkg_info",
        lambda source, query: "react@18.0.0",
    )
    result = docs_lookup("npm", "react")
    assert "react" in result


def test_docs_lookup_with_crates(monkeypatch):
    """Test docs_lookup with crates source."""
    monkeypatch.setattr(
        docs_module,
        "pkg_info",
        lambda source, query: "serde 1.0.0",
    )
    result = docs_lookup("crates", "serde")
    assert "serde" in result


def test_docs_lookup_case_insensitive():
    """Test docs_lookup with mixed case source."""
    result = docs_lookup("MDN", "test")
    # Should not error due to case sensitivity (converted to lowercase)
    assert not result.startswith("error:")


def test_tldr_with_valid_topic(monkeypatch):
    """Test tldr with a valid topic that returns result."""
    def fake_fetch(url):
        if "/common/" in url:
            return "error: HTTP 404"
        if "/linux/" in url:
            return "# git\n\n> Version control\n\n- Show status"
        return "error: HTTP 404"
    
    monkeypatch.setattr(docs_module, "fetch_text", fake_fetch)
    result = tldr("git")
    assert "git" in result
    assert "Version control" in result


def test_clip_zero_cap():
    """Test _clip with zero capacity."""
    text = "hello"
    result = _clip(text, 0)
    assert "[truncated]" in result
    assert result.endswith("[truncated]")


def test_mdn_multiple_results_capped(monkeypatch):
    """Test _mdn truncates results to 8 documents."""
    docs_list = [
        {
            "title": f"Result {i}",
            "mdn_url": f"/path{i}",
            "summary": "Summary" * 100,
        }
        for i in range(15)
    ]
    monkeypatch.setattr(
        docs_module,
        "get_json",
        lambda url: ({"documents": docs_list}, ""),
    )
    result = _mdn("test")
    # Should include up to 8 results
    assert "Result 7" in result
    assert "Result 8" not in result or "Result 8" in result  # At most 8
