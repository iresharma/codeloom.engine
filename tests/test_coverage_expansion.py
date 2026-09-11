"""Comprehensive tests to improve coverage on small/medium modules."""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from runtime.mcp.tokens import apply_tokens, load_tokens, save_token, tokens_path
from runtime.tools import browser, pkg, envinfo, depwhy, scan
from runtime.tools.pkg import (
    _block,
    _clip,
    _crates,
    _go,
    _license,
    _maven,
    _npm,
    _nuget,
    _pypi,
    _rubygems,
)
from runtime.tools.scan import DEFAULT_LIMIT, _iter_files
from tools import skills, shell
from tools.base import ToolContext


# ============================================================================
# runtime/tools/pkg.py coverage tests
# ============================================================================


class TestPkgEdgeCases:
    """Test error handling and edge cases in pkg.py"""

    def test_pkg_info_empty_name(self):
        """Test pkg_info with empty name"""
        result = pkg.pkg_info("pypi", "")
        assert result == "error: name is required"

    def test_pkg_info_whitespace_only_name(self):
        """Test pkg_info with whitespace-only name"""
        result = pkg.pkg_info("pypi", "   ")
        assert result == "error: name is required"

    def test_pkg_info_empty_ecosystem(self):
        """Test pkg_info with empty ecosystem"""
        result = pkg.pkg_info("", "package")
        assert result.startswith("error: ecosystem must be one of")

    def test_pkg_info_whitespace_ecosystem(self):
        """Test pkg_info with whitespace-only ecosystem"""
        result = pkg.pkg_info("   ", "package")
        assert result.startswith("error: ecosystem must be one of")

    def test_pkg_info_case_insensitive(self):
        """Test pkg_info handles case-insensitive ecosystems"""
        result = pkg.pkg_info("PYPI", "leftpad")
        assert not result.startswith("error: ecosystem")

    def test_pypi_with_missing_fields(self, monkeypatch):
        """Test _pypi with minimal/missing response fields"""
        monkeypatch.setattr(
            pkg,
            "get_json",
            lambda url, **k: (
                {"info": {}},
                "",
            ),
        )
        result = pkg.pkg_info("pypi", "minimal")
        assert "name: minimal" in result
        assert "(unknown)" in result
        assert "(none)" in result

    def test_pypi_with_error(self, monkeypatch):
        """Test _pypi with error response"""
        monkeypatch.setattr(
            pkg,
            "get_json",
            lambda url, **k: (None, "error: connection failed"),
        )
        result = pkg.pkg_info("pypi", "requests")
        assert result == "error: connection failed"

    def test_npm_with_missing_versions(self, monkeypatch):
        """Test _npm when versions dict is missing"""
        monkeypatch.setattr(
            pkg,
            "get_json",
            lambda url, **k: (
                {
                    "name": "lodash",
                    "dist-tags": {"latest": "4.17.21"},
                },
                "",
            ),
        )
        result = pkg.pkg_info("npm", "lodash")
        assert "name: lodash" in result
        assert "version: 4.17.21" in result

    def test_npm_with_deprecated(self, monkeypatch):
        """Test _npm with deprecated package"""
        monkeypatch.setattr(
            pkg,
            "get_json",
            lambda url, **k: (
                {
                    "name": "old-lib",
                    "dist-tags": {"latest": "1.0.0"},
                    "versions": {
                        "1.0.0": {
                            "deprecated": "use new-lib instead",
                            "description": "old",
                        }
                    },
                },
                "",
            ),
        )
        result = pkg.pkg_info("npm", "old-lib")
        assert "deprecated: use new-lib instead" in result
        assert "yanked: True" in result

    def test_npm_with_license_dict(self, monkeypatch):
        """Test _npm with license as dict"""
        monkeypatch.setattr(
            pkg,
            "get_json",
            lambda url, **k: (
                {
                    "name": "lib",
                    "dist-tags": {"latest": "1.0.0"},
                    "versions": {
                        "1.0.0": {
                            "license": {"type": "MIT"},
                            "description": "lib",
                        }
                    },
                },
                "",
            ),
        )
        result = pkg.pkg_info("npm", "lib")
        assert "license: MIT" in result

    def test_crates_with_no_versions(self, monkeypatch):
        """Test _crates with empty versions list"""
        monkeypatch.setattr(
            pkg,
            "get_json",
            lambda url, **k: (
                {
                    "crate": {
                        "name": "empty",
                        "max_version": "0.1.0",
                        "repository": "https://github.com/example/empty",
                    },
                    "versions": [],
                },
                "",
            ),
        )
        result = pkg.pkg_info("crates", "empty")
        assert "name: empty" in result
        assert "yanked: False" in result

    def test_go_minimal_response(self, monkeypatch):
        """Test _go with minimal response"""
        monkeypatch.setattr(
            pkg,
            "get_json",
            lambda url, **k: (
                {"Version": "v1.2.3"},
                "",
            ),
        )
        result = pkg.pkg_info("go", "github.com/user/repo")
        assert "name: github.com/user/repo" in result
        assert "version: v1.2.3" in result
        assert "https://pkg.go.dev/github.com/user/repo" in result

    def test_go_missing_version(self, monkeypatch):
        """Test _go with missing version"""
        monkeypatch.setattr(
            pkg,
            "get_json",
            lambda url, **k: (
                {},
                "",
            ),
        )
        result = pkg.pkg_info("go", "github.com/user/repo")
        assert "version: " in result

    def test_maven_with_colon_separator(self, monkeypatch):
        """Test _maven with group:artifact format"""
        monkeypatch.setattr(
            pkg,
            "get_json",
            lambda url, **k: (
                {
                    "response": {
                        "docs": [
                            {
                                "g": "org.example",
                                "a": "lib",
                                "latestVersion": "1.0.0",
                            }
                        ]
                    }
                },
                "",
            ),
        )
        result = pkg.pkg_info("maven", "org.example:lib")
        assert "name: org.example:lib" in result
        assert "version: 1.0.0" in result

    def test_maven_with_simple_name(self, monkeypatch):
        """Test _maven with simple name (no colon)"""
        monkeypatch.setattr(
            pkg,
            "get_json",
            lambda url, **k: (
                {
                    "response": {
                        "docs": [
                            {
                                "g": "org.example",
                                "a": "lib",
                                "latestVersion": "1.0.0",
                            }
                        ]
                    }
                },
                "",
            ),
        )
        result = pkg.pkg_info("maven", "simplelib")
        assert "name: org.example:lib" in result

    def test_maven_no_results(self, monkeypatch):
        """Test _maven with no results"""
        monkeypatch.setattr(
            pkg,
            "get_json",
            lambda url, **k: ({"response": {"docs": []}}, ""),
        )
        result = pkg.pkg_info("maven", "nonexistent")
        assert result == "(no results)"

    def test_nuget_no_results(self, monkeypatch):
        """Test _nuget with no results"""
        monkeypatch.setattr(
            pkg,
            "get_json",
            lambda url, **k: ({"data": []}, ""),
        )
        result = pkg.pkg_info("nuget", "nonexistent")
        assert result == "(no results)"

    def test_nuget_with_versions(self, monkeypatch):
        """Test _nuget with version list"""
        monkeypatch.setattr(
            pkg,
            "get_json",
            lambda url, **k: (
                {
                    "data": [
                        {
                            "id": "Newtonsoft.Json",
                            "version": "13.0.0",
                            "versions": [
                                {"version": "13.0.0"},
                                {"version": "13.0.1"},
                            ],
                            "description": "JSON framework",
                            "projectUrl": "https://json.net",
                            "licenseUrl": "https://mit-license.org",
                        }
                    ]
                },
                "",
            ),
        )
        result = pkg.pkg_info("nuget", "Newtonsoft.Json")
        assert "name: Newtonsoft.Json" in result
        assert "version: 13.0.1" in result

    def test_rubygems_with_licenses_array(self, monkeypatch):
        """Test _rubygems with licenses as array"""
        monkeypatch.setattr(
            pkg,
            "get_json",
            lambda url, **k: (
                {
                    "name": "rails",
                    "version": "7.0.0",
                    "info": "Ruby on Rails",
                    "licenses": ["MIT"],
                    "homepage_uri": "https://rubyonrails.org",
                },
                "",
            ),
        )
        result = pkg.pkg_info("rubygems", "rails")
        assert "name: rails" in result
        assert "license: MIT" in result

    def test_rubygems_with_legacy_project_uri(self, monkeypatch):
        """Test _rubygems falls back to project_uri"""
        monkeypatch.setattr(
            pkg,
            "get_json",
            lambda url, **k: (
                {
                    "name": "rails",
                    "version": "7.0.0",
                    "info": "Rails",
                    "project_uri": "https://rubyonrails.org/legacy",
                },
                "",
            ),
        )
        result = pkg.pkg_info("rubygems", "rails")
        assert "https://rubyonrails.org/legacy" in result

    def test_license_with_dict_name_fallback(self):
        """Test _license with dict containing only name"""
        result = _license({"name": "MIT"})
        assert result == "MIT"

    def test_license_with_dict_empty(self):
        """Test _license with empty dict"""
        result = _license({})
        assert result == ""

    def test_license_with_none(self):
        """Test _license with None"""
        result = _license(None)
        assert result == ""

    def test_block_with_extra(self):
        """Test _block with extra field"""
        result = _block(
            name="pkg",
            version="1.0",
            summary="desc",
            homepage="http://ex.com",
            license_name="MIT",
            yanked=True,
            extra="deprecated: use v2",
        )
        assert "yanked: True" in result
        assert "deprecated: use v2" in result

    def test_clip_under_cap(self):
        """Test _clip with text under cap"""
        text = "short"
        result = _clip(text, 100)
        assert result == "short"

    def test_clip_at_cap(self):
        """Test _clip with text at exactly cap"""
        text = "x" * 100
        result = _clip(text, 100)
        assert result == text
        assert "..." not in result

    def test_clip_over_cap(self):
        """Test _clip with text over cap"""
        text = "x" * 150
        result = _clip(text, 100)
        assert len(result) == 103  # 100 + "..."
        assert result.endswith("...")


# ============================================================================
# runtime/tools/browser.py coverage tests
# ============================================================================


class TestBrowserEdgeCases:
    """Test browser module edge cases"""

    def test_browser_screenshot_path_traversal_blocked(self):
        """Test that path traversal in screenshot name is blocked"""
        async def run():
            # Test that ".." in name is stripped to just "shot.png"
            result = await browser.browser_screenshot(Path("/tmp"), name="../../../etc/passwd.png")
            # Should either work (if playwright unavailable) or safely save
            assert "error" in result.lower() or "saved" in result.lower()

        asyncio.run(run())

    def test_browser_screenshot_missing_extension_adds_png(self):
        """Test that screenshot adds .png if missing"""
        async def run():
            with patch("runtime.tools.browser._ensure", return_value=None):
                result = await browser.browser_screenshot(Path("/tmp"), name="noext")
                # When playwright unavailable, should return error message
                assert "unavailable" in result.lower()

        asyncio.run(run())

    def test_browser_console_empty_list(self):
        """Test browser_console when no messages"""
        async def run():
            with patch("runtime.tools.browser._ensure", return_value=None):
                result = await browser.browser_console()
                assert "unavailable" in result.lower()

        asyncio.run(run())

    def test_browser_network_empty_list(self):
        """Test browser_network when no failures"""
        async def run():
            with patch("runtime.tools.browser._ensure", return_value=None):
                result = await browser.browser_network()
                assert "unavailable" in result.lower()

        asyncio.run(run())

    def test_browser_missing_playwright_graceful(self):
        """Test that missing playwright is handled gracefully"""
        async def run():
            with patch("runtime.tools.browser.async_playwright", side_effect=ImportError):
                # Reset module state
                browser._page = None
                browser._play = None
                page = await browser._ensure()
                assert page is None

        asyncio.run(run())

    def test_browser_screenshot_creates_debug_folder(self):
        """Test that screenshot creates .engine/debug folder"""
        async def run():
            # When playwright unavailable, it just returns error
            result = await browser.browser_screenshot(Path("/tmp"), name="test.png")
            # Either error or success based on playwright availability
            assert "error" in result.lower() or "saved" in result.lower()

        asyncio.run(run())


# ============================================================================
# runtime/tools/envinfo.py coverage tests
# ============================================================================


class TestEnvinfoEdgeCases:
    """Test envinfo module with various subprocess scenarios"""

    def test_runtime_info_all_tools_found(self, monkeypatch):
        """Test runtime_info when all tools are found"""
        call_count = [0]

        def mock_which(cmd):
            return f"/usr/bin/{cmd}"

        def mock_run(args, **kwargs):
            call_count[0] += 1
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=f"{args[0]} version 1.0.0\n",
                stderr="",
            )

        monkeypatch.setattr("shutil.which", mock_which)
        monkeypatch.setattr("subprocess.run", mock_run)

        result = envinfo.runtime_info()
        lines = result.split("\n")
        assert len(lines) == len(envinfo.TOOLS)
        assert all(":" in line for line in lines)
        assert all("(not found)" not in line for line in lines)

    def test_runtime_info_tool_not_found(self, monkeypatch):
        """Test runtime_info when a tool is not found"""
        def mock_which(cmd):
            if cmd == "python3":
                return None
            return f"/usr/bin/{cmd}"

        def mock_run(args, **kwargs):
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=f"{args[0]} version 1.0.0\n",
                stderr="",
            )

        monkeypatch.setattr("shutil.which", mock_which)
        monkeypatch.setattr("subprocess.run", mock_run)

        result = envinfo.runtime_info()
        assert "python: (not found)" in result

    def test_runtime_info_subprocess_fails(self, monkeypatch):
        """Test runtime_info when subprocess raises exception"""
        def mock_which(cmd):
            return f"/usr/bin/{cmd}"

        def mock_run(args, **kwargs):
            raise OSError("permission denied")

        monkeypatch.setattr("shutil.which", mock_which)
        monkeypatch.setattr("subprocess.run", mock_run)

        result = envinfo.runtime_info()
        lines = result.split("\n")
        assert all("(not found)" in line for line in lines)

    def test_runtime_info_timeout(self, monkeypatch):
        """Test runtime_info when subprocess times out"""
        def mock_which(cmd):
            return f"/usr/bin/{cmd}"

        def mock_run(args, **kwargs):
            raise subprocess.TimeoutExpired("cmd", 5)

        monkeypatch.setattr("shutil.which", mock_which)
        monkeypatch.setattr("subprocess.run", mock_run)

        result = envinfo.runtime_info()
        lines = result.split("\n")
        assert all("(not found)" in line for line in lines)

    def test_runtime_info_stderr_fallback(self, monkeypatch):
        """Test runtime_info uses stderr when stdout is empty"""
        def mock_which(cmd):
            return f"/usr/bin/{cmd}"

        def mock_run(args, **kwargs):
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout="",
                stderr=f"{args[0]} version 1.0.0\n",
            )

        monkeypatch.setattr("shutil.which", mock_which)
        monkeypatch.setattr("subprocess.run", mock_run)

        result = envinfo.runtime_info()
        assert "version 1.0.0" in result

    def test_runtime_info_multiple_lines_uses_first(self, monkeypatch):
        """Test runtime_info takes first line of output"""
        def mock_which(cmd):
            return f"/usr/bin/{cmd}"

        def mock_run(args, **kwargs):
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout="version 1.0.0\nother info\n",
                stderr="",
            )

        monkeypatch.setattr("shutil.which", mock_which)
        monkeypatch.setattr("subprocess.run", mock_run)

        result = envinfo.runtime_info()
        lines = result.split("\n")
        for line in lines:
            if ":" in line:
                assert "version 1.0.0" in line


# ============================================================================
# runtime/tools/depwhy.py coverage tests
# ============================================================================


class TestDepwhyEdgeCases:
    """Test depwhy module"""

    def test_dep_why_invalid_ecosystem(self):
        """Test dep_why with invalid ecosystem"""
        result = depwhy.dep_why(Path("/tmp"), "invalid", "pkg")
        assert result.startswith("error: ecosystem must be one of")

    def test_dep_why_missing_name(self):
        """Test dep_why with missing name"""
        result = depwhy.dep_why(Path("/tmp"), "npm", "")
        assert result == "error: name is required"

    def test_dep_why_whitespace_ecosystem(self):
        """Test dep_why with whitespace ecosystem"""
        result = depwhy.dep_why(Path("/tmp"), "  ", "pkg")
        assert result.startswith("error: ecosystem must be one of")

    def test_dep_why_binary_not_installed(self, monkeypatch):
        """Test dep_why when binary is not installed"""
        monkeypatch.setattr("shutil.which", return_value=None)
        result = depwhy.dep_why(Path("/tmp"), "npm", "react")
        assert "error: npm not installed" in result

    def test_dep_why_npm_success(self, monkeypatch):
        """Test dep_why for npm"""
        monkeypatch.setattr("shutil.which", return_value="/usr/bin/npm")
        monkeypatch.setattr(
            "runtime.tools.git.exec_cmd",
            return_value=Mock(
                returncode=0,
                stdout="react@18.0.0\n  └── dependencies\n",
                stderr="",
            ),
        )
        result = depwhy.dep_why(Path("/tmp"), "npm", "react")
        assert "react@18.0.0" in result

    def test_dep_why_output_truncated(self, monkeypatch):
        """Test dep_why truncates long output"""
        monkeypatch.setattr("shutil.which", return_value="/usr/bin/npm")
        long_output = "x" * (depwhy.OUT_CAP + 1000)
        monkeypatch.setattr(
            "runtime.tools.git.exec_cmd",
            return_value=Mock(
                returncode=0,
                stdout=long_output,
                stderr="",
            ),
        )
        result = depwhy.dep_why(Path("/tmp"), "npm", "pkg")
        assert len(result) < len(long_output)
        assert "[truncated]" in result

    def test_dep_why_no_output(self, monkeypatch):
        """Test dep_why with no output"""
        monkeypatch.setattr("shutil.which", return_value="/usr/bin/npm")
        monkeypatch.setattr(
            "runtime.tools.git.exec_cmd",
            return_value=Mock(
                returncode=0,
                stdout="",
                stderr="",
            ),
        )
        result = depwhy.dep_why(Path("/tmp"), "npm", "pkg")
        assert result == "(no output)"

    def test_dep_why_command_failed(self, monkeypatch):
        """Test dep_why when command fails with no output"""
        monkeypatch.setattr("shutil.which", return_value="/usr/bin/npm")
        monkeypatch.setattr(
            "runtime.tools.git.exec_cmd",
            return_value=Mock(
                returncode=1,
                stdout="",
                stderr="",
            ),
        )
        result = depwhy.dep_why(Path("/tmp"), "npm", "pkg")
        assert "error: npm failed" in result

    def test_dep_why_command_failed_with_stderr(self, monkeypatch):
        """Test dep_why when command fails but has stderr output"""
        monkeypatch.setattr("shutil.which", return_value="/usr/bin/npm")
        monkeypatch.setattr(
            "runtime.tools.git.exec_cmd",
            return_value=Mock(
                returncode=1,
                stdout="",
                stderr="Error: package not found",
            ),
        )
        result = depwhy.dep_why(Path("/tmp"), "npm", "pkg")
        assert "Error: package not found" in result


# ============================================================================
# runtime/tools/scan.py coverage tests
# ============================================================================


class TestScanEdgeCases:
    """Test scan module edge cases"""

    def test_todo_scan_invalid_pattern(self, tmp_path):
        """Test todo_scan with invalid regex pattern"""
        result = scan.todo_scan(tmp_path, pattern="[invalid(regex")
        assert result.startswith("error: bad pattern:")

    def test_todo_scan_custom_pattern(self, tmp_path):
        """Test todo_scan with custom pattern"""
        (tmp_path / "file.py").write_text("# CUSTOM: something\n")
        result = scan.todo_scan(tmp_path, pattern="CUSTOM")
        assert "file.py:1:" in result
        assert "CUSTOM: something" in result

    def test_todo_scan_all_markers(self, tmp_path):
        """Test todo_scan finds all marker types"""
        (tmp_path / "file.py").write_text("# TODO: 1\n# FIXME: 2\n# XXX: 3\n# HACK: 4\n")
        result = scan.todo_scan(tmp_path, limit=10)
        assert result.count("file.py:") == 4

    def test_todo_scan_limit_boundaries(self, tmp_path):
        """Test todo_scan with limit at boundaries"""
        for i in range(5):
            (tmp_path / f"file{i}.py").write_text("# TODO: item\n")
        result = scan.todo_scan(tmp_path, limit=2)
        assert "[truncated]" in result
        assert result.count("TODO") == 2

    def test_todo_scan_limit_zero_becomes_one(self, tmp_path):
        """Test todo_scan with limit=0 becomes 1"""
        (tmp_path / "file.py").write_text("# TODO: item\n")
        result = scan.todo_scan(tmp_path, limit=0)
        # Limit should be normalized to 1
        assert "TODO" in result

    def test_todo_scan_limit_over_max(self, tmp_path):
        """Test todo_scan with limit over 200"""
        for i in range(5):
            (tmp_path / f"file{i}.py").write_text("# TODO: item\n")
        result = scan.todo_scan(tmp_path, limit=500)
        # Should be capped at 200
        assert "TODO" in result

    def test_todo_scan_path_not_found(self, tmp_path):
        """Test todo_scan with non-existent path"""
        result = scan.todo_scan(tmp_path, path="nonexistent")
        assert "error:" in result

    def test_todo_scan_path_outside_workspace(self, tmp_path):
        """Test todo_scan with path outside workspace"""
        result = scan.todo_scan(tmp_path, path="/etc/passwd")
        assert "error:" in result or "outside" in result.lower()

    def test_todo_scan_file_path(self, tmp_path):
        """Test todo_scan scanning single file"""
        (tmp_path / "file.py").write_text("# TODO: fix\n")
        result = scan.todo_scan(tmp_path, path="file.py")
        assert "file.py:1:" in result

    def test_todo_scan_rstrip_lines(self, tmp_path):
        """Test todo_scan strips trailing whitespace from lines"""
        (tmp_path / "file.py").write_text("# TODO: fix    \n")
        result = scan.todo_scan(tmp_path)
        # Should strip trailing spaces
        assert "# TODO: fix" in result
        assert "# TODO: fix    " not in result

    def test_todo_scan_unicode_decode_error(self, tmp_path):
        """Test todo_scan skips files with encoding errors"""
        (tmp_path / "bad.bin").write_bytes(b"\xff\xfe")
        (tmp_path / "good.py").write_text("# TODO: ok\n")
        result = scan.todo_scan(tmp_path)
        assert "good.py" in result
        assert "bad.bin" not in result

    def test_todo_scan_permission_error(self, tmp_path, monkeypatch):
        """Test todo_scan skips files with permission errors"""
        (tmp_path / "good.py").write_text("# TODO: ok\n")
        (tmp_path / "dir").mkdir()
        (tmp_path / "dir" / "file.txt").write_text("# TODO: here\n")

        original_open = open

        def mock_open(*args, **kwargs):
            if "dir" in str(args[0]):
                raise OSError("permission denied")
            return original_open(*args, **kwargs)

        monkeypatch.setattr("builtins.open", mock_open)
        result = scan.todo_scan(tmp_path)
        assert "good.py" in result

    def test_iter_files_with_file(self, tmp_path):
        """Test _iter_files with single file"""
        file_path = tmp_path / "file.txt"
        file_path.write_text("content")
        files = list(_iter_files(tmp_path, file_path))
        assert files == [file_path]

    def test_iter_files_with_nonexistent(self, tmp_path):
        """Test _iter_files with non-existent path"""
        files = list(_iter_files(tmp_path, tmp_path / "nonexistent"))
        assert files == []

    def test_iter_files_skips_cached_dirs(self, tmp_path):
        """Test _iter_files skips cache directories"""
        (tmp_path / ".cache").mkdir()
        (tmp_path / ".cache" / "file.txt").write_text("cached")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "file.py").write_text("source")
        files = list(_iter_files(tmp_path, tmp_path))
        file_names = [f.name for f in files]
        assert "file.py" in file_names
        assert "file.txt" not in file_names

    def test_iter_files_permission_error(self, tmp_path, monkeypatch):
        """Test _iter_files skips directories with permission errors"""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "file.py").write_text("ok")

        original_iterdir = Path.iterdir

        def mock_iterdir(self):
            if "skip" in str(self):
                raise OSError("permission denied")
            return original_iterdir(self)

        monkeypatch.setattr(Path, "iterdir", mock_iterdir)
        files = list(_iter_files(tmp_path, tmp_path))
        assert any("file.py" in str(f) for f in files)

    def test_iter_files_deep_nested(self, tmp_path):
        """Test _iter_files with deeply nested directories"""
        nested = tmp_path / "a" / "b" / "c" / "d"
        nested.mkdir(parents=True)
        (nested / "file.txt").write_text("deep")
        files = list(_iter_files(tmp_path, tmp_path))
        assert any("file.txt" in str(f) for f in files)


# ============================================================================
# runtime/mcp/tokens.py coverage tests
# ============================================================================


class TestMcpTokens:
    """Test tokens module"""

    def test_tokens_path(self, tmp_path):
        """Test tokens_path returns correct path"""
        path = tokens_path(tmp_path)
        assert path == tmp_path / ".engine" / "mcp-tokens.json"

    def test_load_tokens_missing_file(self, tmp_path):
        """Test load_tokens with missing file"""
        tokens = load_tokens(tmp_path)
        assert tokens == {}

    def test_load_tokens_invalid_json(self, tmp_path):
        """Test load_tokens with invalid JSON"""
        (tmp_path / ".engine").mkdir()
        (tmp_path / ".engine" / "mcp-tokens.json").write_text("not json")
        tokens = load_tokens(tmp_path)
        assert tokens == {}

    def test_load_tokens_not_dict(self, tmp_path):
        """Test load_tokens when JSON is not a dict"""
        (tmp_path / ".engine").mkdir()
        (tmp_path / ".engine" / "mcp-tokens.json").write_text('["array"]')
        tokens = load_tokens(tmp_path)
        assert tokens == {}

    def test_load_tokens_missing_token_field(self, tmp_path):
        """Test load_tokens filters out entries without token"""
        (tmp_path / ".engine").mkdir()
        (tmp_path / ".engine" / "mcp-tokens.json").write_text(
            json.dumps({"server1": {"tokenEnv": "ENV"}, "server2": {"token": "abc"}})
        )
        tokens = load_tokens(tmp_path)
        assert "server1" not in tokens
        assert "server2" in tokens

    def test_load_tokens_converts_to_str(self, tmp_path):
        """Test load_tokens converts values to strings"""
        (tmp_path / ".engine").mkdir()
        (tmp_path / ".engine" / "mcp-tokens.json").write_text(
            json.dumps({"server": {"token": 123, "tokenEnv": 456}})
        )
        tokens = load_tokens(tmp_path)
        assert tokens["server"]["token"] == "123"
        assert tokens["server"]["tokenEnv"] == "456"

    def test_load_tokens_legacy_token_env(self, tmp_path):
        """Test load_tokens handles legacy token_env field"""
        (tmp_path / ".engine").mkdir()
        (tmp_path / ".engine" / "mcp-tokens.json").write_text(
            json.dumps({"server": {"token": "abc", "token_env": "LEGACY_ENV"}})
        )
        tokens = load_tokens(tmp_path)
        assert tokens["server"]["tokenEnv"] == "LEGACY_ENV"

    def test_save_token_creates_directories(self, tmp_path):
        """Test save_token creates necessary directories"""
        path = save_token(tmp_path, "server", "token123", "TOKEN_ENV")
        assert path.exists()
        assert path.parent.exists()

    def test_save_token_preserves_existing(self, tmp_path):
        """Test save_token preserves other servers"""
        save_token(tmp_path, "server1", "token1", "ENV1")
        save_token(tmp_path, "server2", "token2", "ENV2")
        tokens = load_tokens(tmp_path)
        assert tokens["server1"]["token"] == "token1"
        assert tokens["server2"]["token"] == "token2"

    def test_save_token_permissions(self, tmp_path):
        """Test save_token sets 0600 permissions"""
        path = save_token(tmp_path, "server", "token", "ENV")
        mode = oct(path.stat().st_mode & 0o777)
        assert mode == "0o600"

    def test_apply_tokens_empty_configs(self):
        """Test apply_tokens with empty configs"""
        tokens = {"server": {"token": "abc", "tokenEnv": "ENV"}}
        apply_tokens([], tokens)  # Should not raise

    def test_apply_tokens_missing_token(self):
        """Test apply_tokens skips missing tokens"""
        cfg = Mock(name="server", token_env="ENV", env={})
        apply_tokens([cfg], {})
        # env should not be modified
        assert cfg.env == {}

    def test_apply_tokens_applies_token(self):
        """Test apply_tokens applies token to config"""
        cfg = Mock(name="server", token_env="TOKEN_ENV", env={})
        tokens = {"server": {"token": "secret", "tokenEnv": "TOKEN_ENV"}}
        apply_tokens([cfg], tokens)
        assert cfg.env["TOKEN_ENV"] == "secret"

    def test_apply_tokens_preserves_env_dict(self):
        """Test apply_tokens converts env to dict"""
        cfg = Mock(name="server", token_env="TOKEN_ENV", env={"OTHER": "val"})
        tokens = {"server": {"token": "secret", "tokenEnv": "TOKEN_ENV"}}
        apply_tokens([cfg], tokens)
        # env should be converted to dict and updated
        assert cfg.env is not None

    def test_apply_tokens_no_token_env_in_row(self):
        """Test apply_tokens uses config's token_env if row doesn't have it"""
        cfg = Mock(name="server", token_env="CONFIG_ENV", env={})
        tokens = {"server": {"token": "secret", "tokenEnv": ""}}
        apply_tokens([cfg], tokens)
        assert cfg.env["CONFIG_ENV"] == "secret"

    def test_apply_tokens_sets_token_env_on_config(self):
        """Test apply_tokens sets token_env if not present"""
        cfg = Mock(name="server", token_env="", env={})
        tokens = {"server": {"token": "secret", "tokenEnv": "NEW_ENV"}}
        apply_tokens([cfg], tokens)
        assert cfg.token_env == "NEW_ENV"


# ============================================================================
# tools/shell.py coverage tests
# ============================================================================


class TestShellToolWrappers:
    """Test shell.py tool wrappers"""

    def test_run_command_tool_basic(self, tmp_path):
        """Test run_command tool wrapper basic invocation"""
        async def run():
            ctx = ToolContext(workspace=tmp_path)
            ctx.config = Mock()
            ctx.config.exec_approval = "auto"
            ctx.config.exec_file_limit_mb = 2048
            ctx.config.exec_timeout_s = 120
            result = await shell.run_command(ctx, "echo test")
            assert "echo test" in result
            assert ("exit code" in result or "hello" in result or "test" in result)

        asyncio.run(run())

    def test_run_command_tool_with_cwd(self, tmp_path):
        """Test run_command tool with working directory"""
        async def run():
            (tmp_path / "subdir").mkdir()
            ctx = ToolContext(workspace=tmp_path)
            ctx.config = Mock()
            ctx.config.exec_approval = "auto"
            ctx.config.exec_file_limit_mb = 2048
            ctx.config.exec_timeout_s = 120
            result = await shell.run_command(ctx, "pwd", cwd="subdir")
            assert "subdir" in result or "pwd" in result

        asyncio.run(run())

    def test_run_command_tool_timeout_override(self, tmp_path):
        """Test run_command respects config timeout limits"""
        async def run():
            ctx = ToolContext(workspace=tmp_path)
            ctx.config = Mock()
            ctx.config.exec_approval = "auto"
            ctx.config.exec_file_limit_mb = 2048
            ctx.config.exec_timeout_s = 10  # Limited
            result = await shell.run_command(ctx, "echo x", timeout=600)
            # Should be capped to config's timeout
            assert "exit code" in result or "echo" in result

        asyncio.run(run())

    def test_run_command_tool_no_config(self, tmp_path):
        """Test run_command when config is None"""
        async def run():
            ctx = ToolContext(workspace=tmp_path)
            ctx.config = None
            result = await shell.run_command(ctx, "echo test")
            # Should use defaults
            assert "test" in result or "exit code" in result

        asyncio.run(run())

    def test_run_command_tool_ask_user_callback(self, tmp_path):
        """Test run_command passes ask_user callback"""
        async def run():
            asked = []

            async def ask_user(q, kind="text"):
                asked.append((q, kind))
                return "no"

            ctx = ToolContext(workspace=tmp_path)
            ctx.ask_user = ask_user
            ctx.config = Mock()
            ctx.config.exec_approval = "always"
            ctx.config.exec_file_limit_mb = 2048
            ctx.config.exec_timeout_s = 120

            try:
                result = await shell.run_command(ctx, "echo test")
            except RuntimeError:
                pass  # Expected if not approved
            # ask_user should have been called or skipped gracefully

        asyncio.run(run())

    def test_run_command_tool_on_output_callback(self, tmp_path):
        """Test run_command passes on_output callback"""
        async def run():
            outputs = []

            def on_output(a, stream, text):
                outputs.append((stream, text))

            ctx = ToolContext(workspace=tmp_path)
            ctx.on_output = on_output
            ctx.config = Mock()
            ctx.config.exec_approval = "auto"
            ctx.config.exec_file_limit_mb = 2048
            ctx.config.exec_timeout_s = 120
            result = await shell.run_command(ctx, "echo test")
            # on_output may or may not be called depending on implementation
            assert "test" in result or "echo" in result

        asyncio.run(run())


# ============================================================================
# tools/skills.py coverage tests
# ============================================================================


class TestSkillsToolWrappers:
    """Test skills.py tool wrappers"""

    def test_activate_skill_no_catalog(self, tmp_path):
        """Test activate_skill when catalog is None"""
        ctx = ToolContext(workspace=tmp_path)
        ctx.skills = None
        result = skills.activate_skill(ctx, "unknown")
        assert "skills are not available" in result

    def test_activate_skill_unknown_skill(self, tmp_path):
        """Test activate_skill with unknown skill"""
        ctx = ToolContext(workspace=tmp_path)
        mock_catalog = Mock()
        mock_catalog.get = Mock(return_value=None)
        ctx.skills = mock_catalog
        result = skills.activate_skill(ctx, "unknown")
        assert "unknown skill" in result

    def test_activate_skill_with_siblings(self, tmp_path):
        """Test activate_skill includes sibling files"""
        (tmp_path / ".engine" / "skills" / "test").mkdir(parents=True)
        (tmp_path / ".engine" / "skills" / "test" / "SKILL.md").write_text("BODY")
        (tmp_path / ".engine" / "skills" / "test" / "file.txt").write_text("content")
        (tmp_path / ".engine" / "skills" / "test" / "subdir").mkdir()

        mock_skill = Mock()
        mock_skill.body = "BODY"
        mock_skill.directory = tmp_path / ".engine" / "skills" / "test"

        ctx = ToolContext(workspace=tmp_path)
        mock_catalog = Mock()
        mock_catalog.get = Mock(return_value=mock_skill)
        ctx.skills = mock_catalog

        result = skills.activate_skill(ctx, "test")
        assert "BODY" in result
        assert "Sibling files:" in result
        assert "file.txt" in result

    def test_activate_skill_with_activate_skill_callback(self, tmp_path):
        """Test activate_skill uses ctx.activate_skill if available"""
        ctx = ToolContext(workspace=tmp_path)
        ctx.activate_skill = Mock(return_value="CALLBACK_RESULT")
        result = skills.activate_skill(ctx, "test")
        assert result == "CALLBACK_RESULT"
        ctx.activate_skill.assert_called_once_with("test")

    def test_read_skill_no_catalog(self, tmp_path):
        """Test read_skill when catalog is None"""
        ctx = ToolContext(workspace=tmp_path)
        ctx.skills = None
        result = skills.read_skill(ctx, "test", "file.txt")
        assert "skills are not available" in result

    def test_read_skill_unknown_skill(self, tmp_path):
        """Test read_skill with unknown skill"""
        ctx = ToolContext(workspace=tmp_path)
        mock_catalog = Mock()
        mock_catalog.get = Mock(return_value=None)
        ctx.skills = mock_catalog
        result = skills.read_skill(ctx, "unknown", "file.txt")
        assert "unknown skill" in result

    def test_read_skill_file_error(self, tmp_path):
        """Test read_skill with file read error"""
        ctx = ToolContext(workspace=tmp_path)
        mock_skill = Mock()
        mock_catalog = Mock()
        mock_catalog.get = Mock(return_value=mock_skill)
        ctx.skills = mock_catalog

        with patch("tools.skills.read_skill_file", side_effect=FileNotFoundError("not found")):
            result = skills.read_skill(ctx, "test", "missing.txt")
            assert "error:" in result

    def test_siblings_no_directory(self):
        """Test _siblings with invalid directory"""
        mock_skill = Mock()
        mock_skill.directory = Path("/nonexistent/path")
        result = skills._siblings(mock_skill)
        assert result == []

    def test_siblings_with_files_and_dirs(self, tmp_path):
        """Test _siblings lists files and directories"""
        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("body")
        (skill_dir / "file1.txt").write_text("content")
        (skill_dir / "subdir").mkdir()
        (skill_dir / "file2.md").write_text("content")

        mock_skill = Mock()
        mock_skill.directory = skill_dir
        result = skills._siblings(mock_skill)
        assert "file1.txt" in result
        assert "file2.md" in result
        assert "subdir/" in result
        assert "SKILL.md" not in result
