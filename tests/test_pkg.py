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


# ============================================================================
# Tests for runtime/tools/pkg.py
# ============================================================================

def test_pkg_info_pypi(monkeypatch):
    """Test PyPI package lookup."""
    monkeypatch.setattr(
        "runtime.tools.pkg.get_json",
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
    result = pkg_info("pypi", "requests")
    assert "name: requests" in result
    assert "2.32.0" in result
    assert "HTTP for Humans" in result


def test_pkg_info_npm(monkeypatch):
    """Test NPM package lookup."""
    monkeypatch.setattr(
        "runtime.tools.pkg.get_json",
        lambda url, **k: (
            {
                "dist-tags": {"latest": "1.0.0"},
                "versions": {
                    "1.0.0": {
                        "name": "lodash",
                        "description": "Utility library",
                    }
                },
            },
            "",
        ),
    )
    result = pkg_info("npm", "lodash")
    assert "name: lodash" in result or "Utility library" in result


def test_pkg_info_invalid_ecosystem():
    """Test invalid ecosystem error."""
    result = pkg_info("bogus", "package")
    assert "error:" in result
    assert "pypi" in result


def test_pkg_info_no_name():
    """Test missing package name error."""
    result = pkg_info("pypi", "")
    assert "error:" in result
    assert "required" in result
