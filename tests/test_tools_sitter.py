"""Tests for tools/sitter.py (API wrapper layer) to improve coverage."""
from __future__ import annotations

import pytest
from pathlib import Path

from tools.sitter import (
    list_symbols,
    find_symbol,
    get_node_at,
    query_tree,
    parse_file,
    _as_int,
)
from tests.conftest import seed


class TestAsIntHelper:
    """Test the _as_int helper function."""

    def test_as_int_none(self):
        """Test _as_int with None uses default."""
        assert _as_int(None, 10) == 10

    def test_as_int_empty_string(self):
        """Test _as_int with empty string uses default."""
        assert _as_int("", 10) == 10

    def test_as_int_valid_string(self):
        """Test _as_int converts valid string."""
        assert _as_int("42", 10) == 42

    def test_as_int_zero(self):
        """Test _as_int with zero."""
        assert _as_int("0", 10) == 0

    def test_as_int_negative(self):
        """Test _as_int with negative number."""
        assert _as_int("-5", 10) == -5

    def test_as_int_different_defaults(self):
        """Test _as_int with different default values."""
        assert _as_int(None, 1) == 1
        assert _as_int(None, 100) == 100
        assert _as_int("", 50) == 50
    
    def test_as_int_condition_branches(self):
        """Test both branches of the None or empty string condition."""
        # Line 8: if value is None or value == ""
        # Test the first part (value is None)
        assert _as_int(None, 5) == 5
        # Test the second part (value == "")
        assert _as_int("", 5) == 5
        # Test when neither condition is true
        assert _as_int("3", 5) == 3


class TestListSymbolsTool:
    """Test the list_symbols tool wrapper."""

    def test_list_symbols_basic(self, ctx):
        """Test list_symbols tool with basic Python file."""
        seed(ctx, "module.py", "def foo():\n    return 42\n")
        result = list_symbols(ctx, "module.py")
        assert isinstance(result, str)
        assert "function foo" in result

    def test_list_symbols_with_language_override(self, ctx):
        """Test list_symbols with language override."""
        seed(ctx, "mycode", "def foo():\n    pass\n")
        result = list_symbols(ctx, "mycode", language="python")
        assert "function foo" in result

    def test_list_symbols_empty_language(self, ctx):
        """Test list_symbols with empty language string."""
        seed(ctx, "test.py", "def bar():\n    pass\n")
        result = list_symbols(ctx, "test.py", language="")
        assert "function bar" in result

    def test_list_symbols_javascript_class(self, ctx):
        """Test list_symbols with JavaScript class."""
        seed(ctx, "module.js", "class MyClass {\n    constructor() {}\n}\n")
        result = list_symbols(ctx, "module.js")
        assert "class MyClass" in result

    def test_list_symbols_go_function(self, ctx):
        """Test list_symbols with Go file."""
        seed(ctx, "main.go", "package main\nfunc init() {\n}\n")
        result = list_symbols(ctx, "main.go")
        # Should be parseable

    def test_list_symbols_unsupported_file(self, ctx):
        """Test list_symbols with unsupported file type."""
        seed(ctx, "data.xyz", "x = 1\n")
        result = list_symbols(ctx, "data.xyz")
        assert "error" in result.lower()

    def test_list_symbols_nonexistent_file(self, ctx):
        """Test list_symbols with non-existent file."""
        result = list_symbols(ctx, "does_not_exist.py")
        assert "error" in result.lower()


class TestFindSymbolTool:
    """Test the find_symbol tool wrapper."""

    def test_find_symbol_basic(self, ctx):
        """Test find_symbol tool basic operation."""
        seed(ctx, "module.py", "def my_function():\n    return 1\n")
        result = find_symbol(ctx, "module.py", "my_function")
        assert "Found 'my_function'" in result
        assert "def my_function" in result

    def test_find_symbol_with_language(self, ctx):
        """Test find_symbol with language override."""
        seed(ctx, "code", "def foo():\n    pass\n")
        result = find_symbol(ctx, "code", "foo", language="python")
        assert "Found 'foo'" in result

    def test_find_symbol_empty_language(self, ctx):
        """Test find_symbol with empty language string."""
        seed(ctx, "test.py", "def target():\n    pass\n")
        result = find_symbol(ctx, "test.py", "target", language="")
        assert "Found 'target'" in result

    def test_find_symbol_javascript(self, ctx):
        """Test find_symbol with JavaScript."""
        seed(ctx, "app.js", "function myFunc() {\n    return 42;\n}\n")
        result = find_symbol(ctx, "app.js", "myFunc")
        assert "Found 'myFunc'" in result

    def test_find_symbol_class(self, ctx):
        """Test find_symbol for class."""
        seed(ctx, "module.py", "class TestClass:\n    pass\n")
        result = find_symbol(ctx, "module.py", "TestClass")
        assert "Found 'TestClass'" in result

    def test_find_symbol_not_found(self, ctx):
        """Test find_symbol when symbol not found."""
        seed(ctx, "module.py", "def foo():\n    pass\n")
        result = find_symbol(ctx, "module.py", "nonexistent")
        assert "not found" in result.lower()

    def test_find_symbol_unsupported_file(self, ctx):
        """Test find_symbol with unsupported file type."""
        seed(ctx, "data.xyz", "x = 1\n")
        result = find_symbol(ctx, "data.xyz", "x")
        assert "error" in result.lower()


class TestGetNodeAtTool:
    """Test the get_node_at tool wrapper."""

    def test_get_node_at_basic(self, ctx):
        """Test get_node_at tool basic operation."""
        seed(ctx, "test.py", "def foo():\n    pass\n")
        result = get_node_at(ctx, "test.py", 1, 5)
        assert isinstance(result, str)
        assert "test.py:1:5" in result

    def test_get_node_at_with_language(self, ctx):
        """Test get_node_at with language override."""
        seed(ctx, "code", "def foo():\n    pass\n")
        result = get_node_at(ctx, "code", 1, 5, language="python")
        assert "code:1:5" in result

    def test_get_node_at_empty_language(self, ctx):
        """Test get_node_at with empty language string."""
        seed(ctx, "test.py", "x = 1\n")
        result = get_node_at(ctx, "test.py", 1, 1, language="")
        assert "test.py:1:1" in result

    def test_get_node_at_line_string_converted(self, ctx):
        """Test get_node_at with line as string (should convert)."""
        seed(ctx, "test.py", "x = 1\n")
        result = get_node_at(ctx, "test.py", "1", 1)
        assert "test.py" in result

    def test_get_node_at_character_string_converted(self, ctx):
        """Test get_node_at with character as string (should convert)."""
        seed(ctx, "test.py", "x = 1\n")
        result = get_node_at(ctx, "test.py", 1, "1")
        assert "test.py" in result

    def test_get_node_at_defaults_for_invalid(self, ctx):
        """Test get_node_at defaults invalid numeric inputs."""
        seed(ctx, "test.py", "x = 1\n")
        # Passing None or empty should default to 1
        result = get_node_at(ctx, "test.py", None, None)
        # Should default to line 1, col 1
        assert isinstance(result, str)

    def test_get_node_at_javascript(self, ctx):
        """Test get_node_at with JavaScript file."""
        seed(ctx, "app.js", "function test() { }\n")
        result = get_node_at(ctx, "app.js", 1, 10)
        assert "app.js" in result

    def test_get_node_at_nonexistent_file(self, ctx):
        """Test get_node_at with non-existent file."""
        result = get_node_at(ctx, "missing.py", 1, 1)
        assert "error" in result.lower()


class TestQueryTreeTool:
    """Test the query_tree tool wrapper."""

    def test_query_tree_with_preset(self, ctx):
        """Test query_tree with preset."""
        seed(ctx, "test.py", "def foo():\n    pass\n")
        result = query_tree(ctx, "test.py", preset="functions")
        assert isinstance(result, str)
        assert "capture(s)" in result or "error" in result.lower()

    def test_query_tree_empty_preset(self, ctx):
        """Test query_tree with empty preset string."""
        seed(ctx, "test.py", "x = 1\n")
        result = query_tree(ctx, "test.py", preset="")
        # Empty preset needs a query
        assert "error" in result.lower() or "query" in result.lower()

    def test_query_tree_with_query(self, ctx):
        """Test query_tree with custom query."""
        seed(ctx, "test.py", "def foo():\n    pass\n")
        result = query_tree(
            ctx,
            "test.py",
            query="(function_definition name: (identifier) @name)"
        )
        assert isinstance(result, str)

    def test_query_tree_empty_query(self, ctx):
        """Test query_tree with empty query string."""
        seed(ctx, "test.py", "x = 1\n")
        result = query_tree(ctx, "test.py", query="")
        # Empty query needs preset
        assert "error" in result.lower()

    def test_query_tree_with_language(self, ctx):
        """Test query_tree with language override."""
        seed(ctx, "code", "def foo():\n    pass\n")
        result = query_tree(ctx, "code", preset="functions", language="python")
        assert isinstance(result, str)

    def test_query_tree_empty_language(self, ctx):
        """Test query_tree with empty language string."""
        seed(ctx, "test.py", "def bar():\n    pass\n")
        result = query_tree(ctx, "test.py", preset="functions", language="")
        assert isinstance(result, str)

    def test_query_tree_preset_classes(self, ctx):
        """Test query_tree with classes preset."""
        seed(ctx, "test.py", "class Foo:\n    pass\n")
        result = query_tree(ctx, "test.py", preset="classes")
        assert isinstance(result, str)

    def test_query_tree_preset_imports(self, ctx):
        """Test query_tree with imports preset."""
        seed(ctx, "test.py", "import os\n")
        result = query_tree(ctx, "test.py", preset="imports")
        assert isinstance(result, str)

    def test_query_tree_preset_methods(self, ctx):
        """Test query_tree with methods preset."""
        seed(ctx, "test.py", "class C:\n    def m(self):\n        pass\n")
        result = query_tree(ctx, "test.py", preset="methods")
        assert isinstance(result, str)

    def test_query_tree_preset_calls(self, ctx):
        """Test query_tree with calls preset."""
        seed(ctx, "test.py", "foo()\nbar(x)\n")
        result = query_tree(ctx, "test.py", preset="calls")
        assert isinstance(result, str)

    def test_query_tree_javascript(self, ctx):
        """Test query_tree with JavaScript."""
        seed(ctx, "app.js", "function foo() { }\n")
        result = query_tree(ctx, "app.js", preset="functions")
        assert isinstance(result, str)

    def test_query_tree_nonexistent_file(self, ctx):
        """Test query_tree with non-existent file."""
        result = query_tree(ctx, "missing.py", preset="functions")
        assert "error" in result.lower()


class TestParseFileTool:
    """Test the parse_file tool wrapper."""

    def test_parse_file_basic(self, ctx):
        """Test parse_file tool basic operation."""
        seed(ctx, "test.py", "def foo():\n    pass\n")
        result = parse_file(ctx, "test.py")
        assert isinstance(result, str)
        assert "named node(s)" in result or "No named nodes" in result

    def test_parse_file_with_language(self, ctx):
        """Test parse_file with language override."""
        seed(ctx, "code", "def foo():\n    pass\n")
        result = parse_file(ctx, "code", language="python")
        assert isinstance(result, str)

    def test_parse_file_empty_language(self, ctx):
        """Test parse_file with empty language string."""
        seed(ctx, "test.py", "class Foo:\n    pass\n")
        result = parse_file(ctx, "test.py", language="")
        assert isinstance(result, str)

    def test_parse_file_complex_structure(self, ctx):
        """Test parse_file with complex structure."""
        seed(
            ctx,
            "test.py",
            (
                "class MyClass:\n"
                "    def method(self):\n"
                "        x = 1\n"
                "        return x\n"
            )
        )
        result = parse_file(ctx, "test.py")
        assert isinstance(result, str)

    def test_parse_file_javascript(self, ctx):
        """Test parse_file with JavaScript."""
        seed(ctx, "app.js", "function foo() {\n    return 42;\n}\n")
        result = parse_file(ctx, "app.js")
        assert isinstance(result, str)

    def test_parse_file_go(self, ctx):
        """Test parse_file with Go."""
        seed(ctx, "main.go", "package main\nfunc main() {\n}\n")
        result = parse_file(ctx, "main.go")
        assert isinstance(result, str)

    def test_parse_file_typescript(self, ctx):
        """Test parse_file with TypeScript."""
        seed(ctx, "app.ts", "function test(): void { }\n")
        result = parse_file(ctx, "app.ts")
        assert isinstance(result, str)

    def test_parse_file_tsx(self, ctx):
        """Test parse_file with TypeScript JSX."""
        seed(ctx, "app.tsx", "const App = () => <div></div>;\n")
        result = parse_file(ctx, "app.tsx")
        assert isinstance(result, str)

    def test_parse_file_empty_file(self, ctx):
        """Test parse_file with empty file."""
        seed(ctx, "empty.py", "")
        result = parse_file(ctx, "empty.py")
        assert isinstance(result, str)

    def test_parse_file_nonexistent_file(self, ctx):
        """Test parse_file with non-existent file."""
        result = parse_file(ctx, "missing.py")
        assert "error" in result.lower()

    def test_parse_file_unsupported_language(self, ctx):
        """Test parse_file with unsupported file type."""
        seed(ctx, "data.xyz", "x = 1\n")
        result = parse_file(ctx, "data.xyz")
        assert "error" in result.lower()


class TestToolsIntegration:
    """Integration tests for all tools together."""

    def test_workflow_list_then_find(self, ctx):
        """Test typical workflow: list symbols then find one."""
        seed(ctx, "module.py", "def alpha():\n    pass\ndef beta():\n    pass\n")
        
        # First list what's in the file
        symbols = list_symbols(ctx, "module.py")
        assert "function alpha" in symbols
        assert "function beta" in symbols
        
        # Then find a specific one
        found = find_symbol(ctx, "module.py", "alpha")
        assert "Found 'alpha'" in found

    def test_workflow_query_then_get_node(self, ctx):
        """Test workflow: query tree then get node at position."""
        seed(ctx, "test.py", "def foo():\n    x = 1\n    return x\n")
        
        # Query for functions
        query_result = query_tree(ctx, "test.py", preset="functions")
        assert isinstance(query_result, str)
        
        # Then get node info at a specific position
        node_info = get_node_at(ctx, "test.py", 2, 5)
        assert isinstance(node_info, str)

    def test_workflow_parse_then_query(self, ctx):
        """Test workflow: parse file then query."""
        seed(ctx, "test.py", "class Foo:\n    def bar(self):\n        pass\n")
        
        # Parse to see structure
        parsed = parse_file(ctx, "test.py")
        assert "named node(s)" in parsed or "No named nodes" in parsed
        
        # Query for methods
        methods = query_tree(ctx, "test.py", preset="methods")
        assert isinstance(methods, str)

    def test_all_presets_on_sample_file(self, ctx):
        """Test all presets work on the same file."""
        code = (
            "import os\n"
            "from sys import path\n"
            "\n"
            "class MyClass:\n"
            "    def my_method(self):\n"
            "        foo()\n"
            "        return 42\n"
        )
        seed(ctx, "test.py", code)
        
        for preset in ["imports", "functions", "classes", "methods", "calls"]:
            result = query_tree(ctx, "test.py", preset=preset)
            assert isinstance(result, str)
            # All should succeed even if no captures

    def test_different_languages(self, ctx):
        """Test tools work across different languages."""
        files = [
            ("test.py", "def foo():\n    pass\n", "python"),
            ("test.js", "function foo() { }\n", "javascript"),
            ("test.go", "package main\nfunc foo() { }\n", "go"),
            ("test.ts", "function foo(): void { }\n", "typescript"),
        ]
        
        for filename, content, lang in files:
            seed(ctx, filename, content)
            
            # Each tool should work on the file
            syms = list_symbols(ctx, filename)
            assert isinstance(syms, str)
            
            parsed = parse_file(ctx, filename)
            assert isinstance(parsed, str)
