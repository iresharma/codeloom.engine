from __future__ import annotations

from runtime.tools import pkg
from runtime.tools.pkg import pkg_info


def test_pkg_unknown_ecosystem():
    result = pkg_info("bogus", "leftpad")
    assert result.startswith("error:")
    assert "pypi" in result


def test_pkg_pypi_mocked(monkeypatch):
    monkeypatch.setattr(
        pkg,
        "get_json",
        lambda url, **k: (
            {
                "info": {
                    "name": "requests",
                    "version": "2.32.0",
                    "summary": "HTTP for Humans",
                    "home_page": "https://requests.readthedocs.io",
                    "license": "Apache-2.0",
                    "yanked": False,
                }
            },
            "",
        ),
    )
    text = pkg_info("pypi", "requests")
    assert "name: requests" in text
    assert "2.32.0" in text
    assert "HTTP for Humans" in text
