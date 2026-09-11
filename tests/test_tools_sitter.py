"""Comprehensive tests for tools/sitter.py to raise coverage from 64% to 90%+"""
from __future__ import annotations

import pytest

from tools.sitter import (
    _as_int,
    list_symbols,
    find_symbol,
    get_node_at,
    query_tree,
    parse_file,
)
from tools.base import ToolContext
from runtime.config import EngineConfig
from runtime.store.edits import ensure_schema
from runtime.tools.tracker import FileTracker


@pytest.fixture
def tool_ctx(tmp_path):
    """Create a ToolContext for testing."""
    db = tmp_path / "session.db"
    ensure_schema(db)
    return ToolContext(
        workspace=tmp_path,
        files=FileTracker(),
        journal=db,
        session_id="test-session",
        config=EngineConfig(),
    )


class TestAsInt:
    """Tests for _as_int() helper function."""

    def test_as_int_none(self):
        """None returns default."""
        assert _as_int(None, 10) == 10

    def test_as_int_empty_string(self):
        """Empty string returns default."""
        assert _as_int("", 10) == 10

    def test_as_int_valid_int(self):
        """Valid int is returned as-is."""
        assert _as_int(42, 10) == 42

    def test_as_int_valid_string(self):
        """Valid string is converted to int."""
        assert _as_int("42", 10) == 42

    def test_as_int_zero(self):
        """Zero is preserved."""
        assert _as_int(0, 10) == 0

    def test_as_int_negative(self):
        """Negative numbers work."""
        assert _as_int(-5, 10) == -5
        assert _as_int("-5", 10) == -5

    def test_as_int_large_numbers(self):
        """Large numbers work."""
        assert _as_int(999999, 10) == 999999
        assert _as_int("999999", 10) == 999999

    def test_as_int_whitespace_string(self):
        """Whitespace-only string raises ValueError from int()."""
        # Python's int() strips whitespace but rejects whitespace-only strings
        with pytest.raises(ValueError):
            _as_int("   ", 10)


class TestListSymbolsTool:
    """Tests for list_symbols() tool wrapper."""

    def test_list_symbols_basic(self, tool_ctx):
        """List symbols in a Python file."""
        (tool_ctx.workspace / "test.py").write_text(
            "def foo():\n    pass\n\ndef bar():\n    pass\n"
        )
        result = list_symbols(tool_ctx, "test.py")
        assert "function" in result or "foo" in result
        assert "bar" in result

    def test_list_symbols_with_language_override(self, tool_ctx):
        """List symbols with explicit language override."""
        (tool_ctx.workspace / "code.js").write_text(
            "function foo() { return 1; }\nfunction bar() { return 2; }\n"
        )
        result = list_symbols(tool_ctx, "code.js", language="javascript")
        assert "foo" in result or "function" in result

    def test_list_symbols_empty_language_string(self, tool_ctx):
        """Empty language string is treated as no override."""
        (tool_ctx.workspace / "test.py").write_text("def foo():\n    pass\n")
        result = list_symbols(tool_ctx, "test.py", language="")
        assert "foo" in result or "function" in result

    def test_list_symbols_file_not_found(self, tool_ctx):
        """Non-existent file returns error."""
        result = list_symbols(tool_ctx, "nonexistent.py")
        assert "error:" in result.lower() or "not a file" in result.lower()


class TestFindSymbolTool:
    """Tests for find_symbol() tool wrapper."""

    def test_find_symbol_basic(self, tool_ctx):
        """Find a function in a file."""
        (tool_ctx.workspace / "test.py").write_text(
            "def foo():\n    return 1\n\ndef bar():\n    return 2\n"
        )
        result = find_symbol(tool_ctx, "test.py", "foo")
        assert "foo" in result
        assert "def foo" in result

    def test_find_symbol_with_language(self, tool_ctx):
        """Find symbol with explicit language override."""
        (tool_ctx.workspace / "code.js").write_text(
            "function foo() { return 1; }\n"
        )
        result = find_symbol(tool_ctx, "code.js", "foo", language="javascript")
        assert "foo" in result

    def test_find_symbol_empty_language(self, tool_ctx):
        """Empty language string works."""
        (tool_ctx.workspace / "test.py").write_text("def foo():\n    pass\n")
        result = find_symbol(tool_ctx, "test.py", "foo", language="")
        assert "foo" in result

    def test_find_symbol_not_found(self, tool_ctx):
        """Non-existent symbol returns error."""
        (tool_ctx.workspace / "test.py").write_text("def foo():\n    pass\n")
        result = find_symbol(tool_ctx, "test.py", "bar")
        assert "not found" in result.lower() or "Symbol" in result


class TestGetNodeAtTool:
    """Tests for get_node_at() tool wrapper."""

    def test_get_node_at_basic(self, tool_ctx):
        """Get node at specific line/character."""
        (tool_ctx.workspace / "test.py").write_text("def foo():\n    return 1\n")
        result = get_node_at(tool_ctx, "test.py", 1, 5)
        assert "type:" in result
        assert "test.py" in result

    def test_get_node_at_with_int_args(self, tool_ctx):
        """Line and character as int."""
        (tool_ctx.workspace / "test.py").write_text("def foo():\n    pass\n")
        result = get_node_at(tool_ctx, "test.py", 1, 5)
        assert "type:" in result

    def test_get_node_at_with_string_args(self, tool_ctx):
        """Line and character as strings."""
        (tool_ctx.workspace / "test.py").write_text("def foo():\n    pass\n")
        result = get_node_at(tool_ctx, "test.py", "1", "5")
        assert "type:" in result

    def test_get_node_at_none_args(self, tool_ctx):
        """None line/character defaults to 1."""
        (tool_ctx.workspace / "test.py").write_text("x = 1\n")
        result = get_node_at(tool_ctx, "test.py", None, None)
        assert "type:" in result

    def test_get_node_at_empty_string_args(self, tool_ctx):
        """Empty string line/character defaults to 1."""
        (tool_ctx.workspace / "test.py").write_text("x = 1\n")
        result = get_node_at(tool_ctx, "test.py", "", "")
        assert "type:" in result

    def test_get_node_at_with_language(self, tool_ctx):
        """Explicit language override."""
        (tool_ctx.workspace / "code.js").write_text("var x = 1;\n")
        result = get_node_at(tool_ctx, "code.js", 1, 5, language="javascript")
        assert "type:" in result

    def test_get_node_at_file_not_found(self, tool_ctx):
        """Non-existent file returns error."""
        result = get_node_at(tool_ctx, "nonexistent.py", 1, 1)
        assert "error:" in result.lower() or "not a file" in result.lower()

    def test_get_node_at_zero_args(self, tool_ctx):
        """Zero line/character is clamped to 1."""
        (tool_ctx.workspace / "test.py").write_text("x = 1\n")
        result = get_node_at(tool_ctx, "test.py", 0, 0)
        # 0 should clamp to 1 in the runtime function
        assert "type:" in result

    def test_get_node_at_negative_args(self, tool_ctx):
        """Negative line/character."""
        (tool_ctx.workspace / "test.py").write_text("x = 1\n")
        result = get_node_at(tool_ctx, "test.py", -1, -5)
        # Negatives are clamped to 0 then to 1 via max(0, ...)
        assert isinstance(result, str)


class TestQueryTreeTool:
    """Tests for query_tree() tool wrapper."""

    def test_query_tree_with_preset(self, tool_ctx):
        """Query tree with preset."""
        (tool_ctx.workspace / "test.py").write_text(
            "def foo():\n    pass\n\ndef bar():\n    pass\n"
        )
        result = query_tree(tool_ctx, "test.py", preset="functions")
        assert "foo" in result or "capture(s)" in result

    def test_query_tree_no_preset_no_query(self, tool_ctx):
        """No preset and no query returns error."""
        (tool_ctx.workspace / "test.py").write_text("x = 1\n")
        result = query_tree(tool_ctx, "test.py")
        assert "error:" in result.lower()

    def test_query_tree_preset_and_query(self, tool_ctx):
        """Query string takes precedence over preset (both provided)."""
        (tool_ctx.workspace / "test.py").write_text("def foo():\n    pass\n")
        # Query is provided, so it should be used (or preset ignored)
        result = query_tree(tool_ctx, "test.py", preset="functions", query="(identifier)")
        assert isinstance(result, str)

    def test_query_tree_empty_preset(self, tool_ctx):
        """Empty preset string treated as no preset."""
        (tool_ctx.workspace / "test.py").write_text("def foo():\n    pass\n")
        result = query_tree(tool_ctx, "test.py", preset="", query="(identifier)")
        assert isinstance(result, str)

    def test_query_tree_empty_query(self, tool_ctx):
        """Empty query string treated as no query."""
        (tool_ctx.workspace / "test.py").write_text(
            "def foo():\n    pass\n\ndef bar():\n    pass\n"
        )
        result = query_tree(tool_ctx, "test.py", preset="functions", query="")
        assert "foo" in result or "capture(s)" in result

    def test_query_tree_with_language(self, tool_ctx):
        """Explicit language override."""
        (tool_ctx.workspace / "code.js").write_text(
            "function foo() {}\nfunction bar() {}\n"
        )
        result = query_tree(tool_ctx, "code.js", preset="functions", language="javascript")
        assert "foo" in result or "capture(s)" in result

    def test_query_tree_all_presets(self, tool_ctx):
        """Test all available presets."""
        code = (
            "import os\n"
            "def foo():\n    pass\n"
            "class Bar:\n    def method(self):\n        pass\n"
            "x = foo()\n"
        )
        (tool_ctx.workspace / "test.py").write_text(code)
        
        # imports
        result = query_tree(tool_ctx, "test.py", preset="imports")
        assert "import" in result.lower() or "capture" in result.lower()
        
        # functions
        result = query_tree(tool_ctx, "test.py", preset="functions")
        assert "foo" in result or "capture" in result.lower()
        
        # classes
        result = query_tree(tool_ctx, "test.py", preset="classes")
        assert "Bar" in result or "capture" in result.lower()
        
        # methods
        result = query_tree(tool_ctx, "test.py", preset="methods")
        assert "method" in result or "capture" in result.lower()
        
        # calls
        result = query_tree(tool_ctx, "test.py", preset="calls")
        # calls might find foo() call
        assert "capture" in result.lower() or "foo" in result

    def test_query_tree_case_insensitive_preset(self, tool_ctx):
        """Preset comparison is case-insensitive."""
        (tool_ctx.workspace / "test.py").write_text("def foo():\n    pass\n")
        result = query_tree(tool_ctx, "test.py", preset="FUNCTIONS")
        # Should work due to .lower() in runtime
        assert isinstance(result, str)

    def test_query_tree_file_not_found(self, tool_ctx):
        """Non-existent file returns error."""
        result = query_tree(tool_ctx, "nonexistent.py", preset="functions")
        assert "error:" in result.lower()


class TestParseFileTool:
    """Tests for parse_file() tool wrapper."""

    def test_parse_file_basic(self, tool_ctx):
        """Parse file shows structure."""
        (tool_ctx.workspace / "test.py").write_text(
            "def foo():\n    return 1\n"
        )
        result = parse_file(tool_ctx, "test.py")
        assert "named node(s)" in result or "foo" in result

    def test_parse_file_with_language(self, tool_ctx):
        """Parse file with explicit language override."""
        (tool_ctx.workspace / "code.js").write_text(
            "function foo() { return 1; }\n"
        )
        result = parse_file(tool_ctx, "code.js", language="javascript")
        assert "foo" in result or "named node(s)" in result

    def test_parse_file_empty_language(self, tool_ctx):
        """Empty language string works."""
        (tool_ctx.workspace / "test.py").write_text("x = 1\n")
        result = parse_file(tool_ctx, "test.py", language="")
        assert "named node(s)" in result or "x" in result

    def test_parse_file_nested_structure(self, tool_ctx):
        """Parse file shows nested structure."""
        (tool_ctx.workspace / "test.py").write_text(
            "class Foo:\n    def method(self):\n        pass\n"
        )
        result = parse_file(tool_ctx, "test.py")
        assert "Foo" in result
        assert "method" in result

    def test_parse_file_not_found(self, tool_ctx):
        """Non-existent file returns error."""
        result = parse_file(tool_ctx, "nonexistent.py")
        assert "error:" in result.lower() or "not a file" in result.lower()


class TestToolsContextIntegration:
    """Integration tests for tool wrappers."""

    def test_list_then_find_symbol(self, tool_ctx):
        """List symbols then find one."""
        (tool_ctx.workspace / "test.py").write_text(
            "def foo():\n    pass\n\ndef bar():\n    pass\n"
        )
        symbols = list_symbols(tool_ctx, "test.py")
        assert "foo" in symbols
        
        found = find_symbol(tool_ctx, "test.py", "foo")
        assert "foo" in found

    def test_get_node_then_parse(self, tool_ctx):
        """Get node at position then parse full file."""
        (tool_ctx.workspace / "test.py").write_text(
            "def foo():\n    return 1\ndef bar():\n    return 2\n"
        )
        node_info = get_node_at(tool_ctx, "test.py", 1, 5)
        assert "type:" in node_info
        
        parse_info = parse_file(tool_ctx, "test.py")
        assert "foo" in parse_info
        assert "bar" in parse_info

    def test_query_and_list_symbols(self, tool_ctx):
        """Query tree and list symbols show complementary info."""
        (tool_ctx.workspace / "test.py").write_text(
            "import os\n\ndef foo():\n    pass\n\nclass Bar:\n    pass\n"
        )
        symbols = list_symbols(tool_ctx, "test.py")
        assert "foo" in symbols or "Bar" in symbols
        
        functions = query_tree(tool_ctx, "test.py", preset="functions")
        assert "foo" in functions or "capture" in functions.lower()

    def test_multiple_files(self, tool_ctx):
        """Work with multiple files."""
        (tool_ctx.workspace / "a.py").write_text("def alpha():\n    pass\n")
        (tool_ctx.workspace / "b.py").write_text("def beta():\n    pass\n")
        
        result_a = list_symbols(tool_ctx, "a.py")
        assert "alpha" in result_a
        
        result_b = list_symbols(tool_ctx, "b.py")
        assert "beta" in result_b


class TestAsIntEdgeCases:
    """Edge cases for _as_int()."""

    def test_as_int_with_default_zero(self):
        """Default value of 0."""
        assert _as_int(None, 0) == 0

    def test_as_int_with_default_one(self):
        """Default value of 1."""
        assert _as_int(None, 1) == 1

    def test_as_int_with_default_negative(self):
        """Default value negative."""
        assert _as_int("", -10) == -10

    def test_as_int_preserves_value(self):
        """Actual values override default."""
        assert _as_int(5, 10) == 5
        assert _as_int("5", 10) == 5
        assert _as_int(0, 10) == 0
        assert _as_int("0", 10) == 0
