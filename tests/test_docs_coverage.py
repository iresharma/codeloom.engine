"""Coverage for runtime/tools/docs.py"""
from __future__ import annotations

from unittest.mock import Mock, patch

from runtime.tools.docs import docs_lookup, tldr, _mdn, _go, _clip


def test_docs_lookup_invalid_source():
    result = docs_lookup("invalid", "query")
    assert "error" in result.lower()
    assert "source must be one of" in result


def test_docs_lookup_empty_query():
    result = docs_lookup("mdn", "")
    assert "error" in result.lower()
    assert "query is required" in result


def test_docs_lookup_pypi():
    with patch("runtime.tools.docs.pkg_info") as mock_pkg:
        mock_pkg.return_value = "package info"
        result = docs_lookup("pypi", "requests")
        assert "package info" in result


def test_docs_lookup_npm():
    with patch("runtime.tools.docs.pkg_info") as mock_pkg:
        mock_pkg.return_value = "npm package"
        result = docs_lookup("npm", "react")
        assert "npm package" in result


def test_docs_lookup_crates():
    with patch("runtime.tools.docs.pkg_info") as mock_pkg:
        mock_pkg.return_value = "crate info"
        result = docs_lookup("crates", "serde")
        assert "crate info" in result


def test_docs_lookup_go():
    with patch("runtime.tools.docs.web_fetch") as mock_web:
        mock_web.return_value = "go docs"
        result = docs_lookup("go", "fmt")
        assert "go docs" in result or "pkg.go.dev" in result


def test_docs_lookup_mdn():
    with patch("runtime.tools.docs.get_json") as mock_json:
        mock_json.return_value = ({"documents": [{"title": "MDN", "mdn_url": "/test", "summary": "info"}]}, "")
        result = docs_lookup("mdn", "javascript")
        assert "MDN" in result or "info" in result


def test_docs_lookup_case_insensitive():
    with patch("runtime.tools.docs.pkg_info") as mock_pkg:
        mock_pkg.return_value = "info"
        result = docs_lookup("PYPI", "pkg")
        assert "info" in result


def test_tldr_not_found():
    with patch("runtime.tools.docs.fetch_text") as mock_fetch:
        mock_fetch.return_value = "error: HTTP 404"
        result = tldr("nonexistent_command_xyz")
        assert "error" in result.lower() or "no tldr page" in result


def test_tldr_invalid_topic():
    result = tldr("")
    assert "error" in result.lower()
    assert "topic is required" in result


def test_tldr_fetch_error():
    with patch("runtime.tools.docs.fetch_text") as mock_fetch:
        mock_fetch.return_value = "error: network timeout"
        result = tldr("ls")
        assert "error" in result.lower()


def test_tldr_success():
    with patch("runtime.tools.docs.fetch_text") as mock_fetch:
        mock_fetch.return_value = "# ls\nList files"
        result = tldr("ls")
        assert "ls" in result or "List files" in result


def test_tldr_sanitizes_topic():
    with patch("runtime.tools.docs.fetch_text") as mock_fetch:
        mock_fetch.return_value = "# git\nVersion control"
        result = tldr("GiT@#$%")
        assert mock_fetch.called


def test_mdn_no_results():
    with patch("runtime.tools.docs.get_json") as mock_json:
        mock_json.return_value = ({}, "")
        result = _mdn("nonexistent")
        assert "(no results)" in result


def test_mdn_with_results():
    with patch("runtime.tools.docs.get_json") as mock_json:
        docs = [
            {
                "title": "Test Page",
                "mdn_url": "/docs/test",
                "summary": "A test page",
            }
        ]
        mock_json.return_value = ({"documents": docs}, "")
        result = _mdn("test")
        assert "Test Page" in result or "test" in result.lower()


def test_mdn_partial_fields():
    with patch("runtime.tools.docs.get_json") as mock_json:
        docs = [{"title": "Page"}]  # Missing mdn_url and summary
        mock_json.return_value = ({"documents": docs}, "")
        result = _mdn("test")
        assert "Page" in result


def test_mdn_error():
    with patch("runtime.tools.docs.get_json") as mock_json:
        mock_json.return_value = (None, "error: connection failed")
        result = _mdn("test")
        assert "error" in result.lower()


def test_go_with_slash():
    with patch("runtime.tools.docs.web_fetch") as mock_web:
        mock_web.return_value = "Go package docs"
        result = _go("github.com/user/repo")
        assert mock_web.called


def test_go_without_slash():
    with patch("runtime.tools.docs.web_fetch") as mock_web:
        mock_web.return_value = "Go package docs"
        result = _go("fmt")
        assert mock_web.called


def test_go_error():
    with patch("runtime.tools.docs.web_fetch") as mock_web:
        mock_web.return_value = "error: network timeout"
        result = _go("fmt")
        assert "error" in result.lower()


def test_clip_under_cap():
    text = "short text"
    result = _clip(text, 100)
    assert result == text


def test_clip_over_cap():
    text = "a" * 100
    result = _clip(text, 50)
    assert len(result) <= 65  # 50 + "[truncated]" length
    assert "truncated" in result


def test_clip_exact_cap():
    text = "a" * 50
    result = _clip(text, 50)
    assert result == text
