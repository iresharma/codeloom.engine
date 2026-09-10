from __future__ import annotations

from runtime.tools import docs
from runtime.tools.docs import docs_lookup, tldr


def test_docs_unknown_source():
    result = docs_lookup("stackoverflow", "fetch")
    assert result.startswith("error:")
    assert "mdn" in result


def test_docs_mdn_mocked(monkeypatch):
    monkeypatch.setattr(
        docs,
        "get_json",
        lambda url, **k: (
            {
                "documents": [
                    {
                        "title": "fetch()",
                        "mdn_url": "/en-US/docs/Web/API/fetch",
                        "summary": "The fetch() method.",
                    }
                ]
            },
            "",
        ),
    )
    text = docs_lookup("mdn", "fetch")
    assert "fetch()" in text
    assert "developer.mozilla.org/en-US/docs/Web/API/fetch" in text


def test_tldr_falls_through_404(monkeypatch):
    calls = []

    def fake_fetch(url):
        calls.append(url)
        if "/common/" in url:
            return "error: HTTP 404"
        return "# git\n> example"

    monkeypatch.setattr(docs, "fetch_text", fake_fetch)
    text = tldr("git")
    assert "# git" in text
    assert any("/common/git.md" in url for url in calls)
    assert any("/linux/git.md" in url for url in calls)
