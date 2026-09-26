"""Item 8: LSP tools answer immediately for a file no language server handles.

The LSP manager is a strict stand-in that raises on *any* attribute access, so
a test only passes if the tool never touched it.
"""

from __future__ import annotations

import pytest

from runtime.tools.lsp import LSPManager, unsupported_extension_note
from tools.base import ToolContext
from tools.lsp import (
    document_symbols,
    find_references,
    get_diagnostics,
    goto_definition,
    hover,
    rename_symbol,
)
from tools.registry import discover_tools


class _ExplodingLSP:
    """Any use at all is a failure: the whole point is not to reach a server."""

    def __getattr__(self, name):  # noqa: ANN001
        raise AssertionError(f"the LSP manager was touched: .{name}")


UNSUPPORTED = [
    "deploy.sh",
    "README.md",
    ".gitignore",
    "Dockerfile",
    "Makefile",
    "config.yaml",
    "data.json",
    "pyproject.toml",
    "notes.txt",
    "schema.sql",
    "mystery.xyz",
]
SUPPORTED = ["app.py", "types.pyi", "main.go", "index.js", "page.tsx", "mod.mts"]


# --------------------------------------------------------------------------
# the note itself


@pytest.mark.parametrize("path", UNSUPPORTED)
def test_unsupported_paths_get_a_note(path):
    note = unsupported_extension_note(path)
    assert note is not None
    assert "LSP tools only apply to python, go, javascript/typescript" in note
    assert "Do not retry an LSP tool on this file" in note


@pytest.mark.parametrize("path", SUPPORTED)
def test_supported_paths_get_no_note(path):
    assert unsupported_extension_note(path) is None


def test_every_configured_extension_is_treated_as_supported():
    for cfg in LSPManager.SERVER_CONFIGS.values():
        for ext in cfg.extensions:
            assert unsupported_extension_note(f"file{ext}") is None, ext


def test_shell_scripts_are_pointed_at_bash_n():
    note = unsupported_extension_note("scripts/build.sh")
    assert "bash -n" in note
    assert "shellcheck" in note


def test_documents_are_told_there_is_nothing_to_run():
    for path in ("README.md", "notes.txt", "guide.rst"):
        note = unsupported_extension_note(path)
        assert "nothing" in note, path


def test_json_and_yaml_get_a_parser_suggestion():
    assert "json.tool" in unsupported_extension_note("package.json")
    assert "yaml.safe_load" in unsupported_extension_note("ci.yaml")


def test_an_empty_path_has_no_opinion():
    assert unsupported_extension_note("") is None


# --------------------------------------------------------------------------
# the tools short-circuit without touching the manager


@pytest.mark.asyncio
@pytest.mark.parametrize("path", UNSUPPORTED)
async def test_get_diagnostics_returns_immediately(tmp_path, path):
    ctx = ToolContext(workspace=tmp_path, lsp=_ExplodingLSP())
    out = await get_diagnostics(ctx, path)
    assert "LSP tools only apply to" in out
    assert "No LSP server configured for extension" not in out


@pytest.mark.asyncio
async def test_every_lsp_tool_short_circuits(tmp_path):
    ctx = ToolContext(workspace=tmp_path, lsp=_ExplodingLSP())
    assert "LSP tools only apply to" in await get_diagnostics(ctx, "a.sh")
    assert "LSP tools only apply to" in await document_symbols(ctx, "a.sh")
    assert "LSP tools only apply to" in await goto_definition(ctx, "a.sh", 1, 1)
    assert "LSP tools only apply to" in await find_references(ctx, "a.sh", 1, 1)
    assert "LSP tools only apply to" in await hover(ctx, "a.sh", 1, 1)
    assert "LSP tools only apply to" in await rename_symbol(ctx, "a.sh", 1, 1, "x")


@pytest.mark.asyncio
async def test_a_supported_extension_still_reaches_the_manager(tmp_path):
    """The guard must not swallow real calls: a .py file goes through."""
    calls: list[str] = []

    class _Recording:
        def open_file_and_get_diagnostics(self, path):
            calls.append(path)
            return []

    ctx = ToolContext(workspace=tmp_path, lsp=_Recording())
    (tmp_path / "a.py").write_text("x = 1\n")
    out = await get_diagnostics(ctx, "a.py")
    assert calls == ["a.py"]
    assert "clean" in out


@pytest.mark.asyncio
async def test_the_extension_check_precedes_the_missing_manager_message(tmp_path):
    """With no LSP at all, an unsupported file still gets the useful note
    rather than the generic "LSP is not available"."""
    ctx = ToolContext(workspace=tmp_path, lsp=None)
    out = await get_diagnostics(ctx, "README.md")
    assert "LSP tools only apply to" in out
    assert "LSP is not available for this workspace" not in out
    # A supported file with no manager still gets the generic message.
    assert "LSP is not available" in await get_diagnostics(ctx, "a.py")


# --------------------------------------------------------------------------
# the model is told, too


@pytest.mark.parametrize(
    "name",
    [
        "goto_definition",
        "find_references",
        "hover",
        "get_diagnostics",
        "document_symbols",
        "rename_symbol",
    ],
)
def test_tool_descriptions_state_the_supported_languages(name):
    description = discover_tools().get(name).description
    assert "python, go, and javascript/typescript" in description


def test_get_diagnostics_description_names_the_shell_alternative():
    description = discover_tools().get("get_diagnostics").description
    assert "bash -n" in description
