from __future__ import annotations

from runtime.tools import osv
from runtime.tools.osv import osv_query


def test_osv_unknown_ecosystem():
    result = osv_query("homebrew", "wget")
    assert result.startswith("error:")
    assert "npm" in result


def test_osv_formats_vulns(monkeypatch):
    monkeypatch.setattr(
        osv,
        "post_json",
        lambda url, payload, **k: (
            {
                "vulns": [
                    {
                        "id": "GHSA-xxxx",
                        "summary": "Prototype pollution",
                        "aliases": ["CVE-2020-8203"],
                        "severity": [{"score": "HIGH"}],
                        "affected": [
                            {
                                "package": {"name": "lodash"},
                                "ranges": [
                                    {
                                        "events": [
                                            {"introduced": "0"},
                                            {"fixed": "4.17.21"},
                                        ]
                                    }
                                ],
                            }
                        ],
                    }
                ]
            },
            "",
        ),
    )
    text = osv_query("npm", "lodash", version="4.17.20")
    assert "GHSA-xxxx" in text
    assert "CVE-2020-8203" in text
    assert "4.17.21" in text


def test_osv_none(monkeypatch):
    monkeypatch.setattr(osv, "post_json", lambda *a, **k: ({"vulns": []}, ""))
    assert osv_query("npm", "leftpad") == "(no advisories)"
