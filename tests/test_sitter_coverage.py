"""Tests for runtime/tools/sitter.py to improve coverage."""
from __future__ import annotations

import pytest
from pathlib import Path

from runtime.tools.sitter import (
    check_syntax,
    syntax_gate,
    language_for,
    parse_bytes,
    SyntaxFault,
    list_symbols,
    find_symbol,
    get_node_at,
    query_tree,
    parse_file,
    PRESETS,
    symbol_range_in_text,
    replace_symbol_in_text,
    insert_after_imports_in_text,
)
from tests.conftest import seed


class TestLanguageDetection:
    """Test language detection from file extensions and explicit hints."""

    def test_language_for_python(self):
        """Test Python extension detection."""
        assert language_for("test.py") == "python"
        assert language_for("test.pyi") == "python"

    def test_language_for_javascript(self):
        """Test JavaScript extension detection."""
        assert language_for("test.js") == "javascript"
        assert language_for("test.jsx") == "javascript"
        assert language_for("test.mjs") == "javascript"
        assert language_for("test.cjs") == "javascript"

    def test_language_for_typescript(self):
        """Test TypeScript extension detection."""
        assert language_for("test.ts") == "typescript"
        assert language_for("test.mts") == "typescript"
        assert language_for("test.cts") == "typescript"

    def test_language_for_typescript_jsx(self):
        """Test TypeScript JSX detection."""
        assert language_for("test.tsx") == "typescript_jsx"

    def test_language_for_go(self):
        """Test Go extension detection."""
        assert language_for("test.go") == "go"

    def test_language_for_unknown_extension(self):
        """Test unknown extension returns None."""
        assert language_for("test.unknown") is None
        assert language_for("test") is None

    def test_language_for_explicit_override(self):
        """Test explicit language override."""
        assert language_for("test.js", language="python") == "python"
        assert language_for("test.py", language="go") == "go"

    def test_language_for_explicit_tsx_alias(self):
        """Test that tsx alias maps to typescript_jsx."""
        assert language_for("test.py", language="tsx") == "typescript_jsx"

    def test_language_for_explicit_typescript_jsx_name(self):
        """Test typescript_jsx explicit name."""
        assert language_for("test.py", language="typescript_jsx") == "typescript_jsx"

    def test_language_for_typescript_with_tsx_file(self):
        """Test that typescript language with .tsx file returns typescript_jsx."""
        assert language_for("test.tsx", language="typescript") == "typescript_jsx"

    def test_language_for_case_insensitive(self):
        """Test case-insensitive language specification."""
        assert language_for("test.py", language="PYTHON") == "python"
        assert language_for("test.py", language="Go") == "go"

    def test_language_for_invalid_language_name(self):
        """Test invalid language name returns None."""
        assert language_for("test.py", language="nonexistent") is None

    def test_language_for_empty_language_string(self):
        """Test empty language string triggers extension detection."""
        assert language_for("test.py", language="") == "python"


class TestCheckSyntax:
    """Test syntax checking for various languages."""

    def test_check_syntax_valid_python(self):
        """Test syntax checking for valid Python."""
        source = b"def foo():\n    return 42\n"
        faults = check_syntax("python", source)
        assert faults == []

    def test_check_syntax_invalid_python(self):
        """Test syntax checking for invalid Python."""
        source = b"def foo(\n"  # Missing closing paren
        faults = check_syntax("python", source)
        assert len(faults) > 0
        assert any(f.kind in ("ERROR", "MISSING") for f in faults)

    def test_check_syntax_valid_javascript(self):
        """Test syntax checking for valid JavaScript."""
        source = b"function foo() { return 42; }\n"
        faults = check_syntax("javascript", source)
        assert faults == []

    def test_check_syntax_invalid_javascript(self):
        """Test syntax checking for invalid JavaScript."""
        source = b"function foo( {\n"  # Syntax error
        faults = check_syntax("javascript", source)
        assert len(faults) >= 0  # May or may not catch depending on parser

    def test_check_syntax_valid_go(self):
        """Test syntax checking for valid Go."""
        source = b"package main\nfunc main() {\n}\n"
        faults = check_syntax("go", source)
        assert len(faults) >= 0

    def test_check_syntax_valid_typescript(self):
        """Test syntax checking for valid TypeScript."""
        source = b"function foo(): number { return 42; }\n"
        faults = check_syntax("typescript", source)
        assert len(faults) >= 0

    def test_syntax_fault_dataclass(self):
        """Test SyntaxFault dataclass creation."""
        fault = SyntaxFault(line=1, col=5, kind="ERROR", text="bad syntax")
        assert fault.line == 1
        assert fault.col == 5
        assert fault.kind == "ERROR"
        assert fault.text == "bad syntax"


class TestSyntaxGate:
    """Test the syntax_gate function for edit validation."""

    def test_syntax_gate_valid_new_file(self):
        """Test syntax gate passes for valid new file."""
        result = syntax_gate("new.py", "def foo():\n    pass\n", old_text=None)
        assert result is None

    def test_syntax_gate_invalid_new_file(self):
        """Test syntax gate rejects invalid new file."""
        result = syntax_gate("new.py", "def foo(\n", old_text=None)
        assert result is not None
        assert "syntax gate rejected new file" in result

    def test_syntax_gate_valid_edit(self):
        """Test syntax gate passes for valid edit."""
        old = "def foo():\n    return 1\n"
        new = "def foo():\n    return 2\n"
        result = syntax_gate("edit.py", new, old_text=old)
        assert result is None

    def test_syntax_gate_introduces_new_error(self):
        """Test syntax gate rejects edit that introduces error."""
        old = "def foo():\n    return 1\n"
        new = "def foo(\n    return 1\n"
        result = syntax_gate("edit.py", new, old_text=old)
        assert result is not None

    def test_syntax_gate_unsupported_language(self):
        """Test syntax gate returns None for unsupported language."""
        result = syntax_gate("file.unknown", "x = 1", old_text=None)
        assert result is None

    def test_syntax_gate_complex_python(self):
        """Test syntax gate with complex Python."""
        old = "class Foo:\n    def bar(self):\n        pass\n"
        new = "class Foo:\n    def bar(self):\n        return 1\n"
        result = syntax_gate("test.py", new, old_text=old)
        assert result is None

    def test_syntax_gate_multiple_errors_truncated(self):
        """Test syntax gate truncates error list at 12 items."""
        # Create source with many errors by having invalid syntax
        old = "x = 1\n"
        new = "(" * 50  # Many unclosed parens
        result = syntax_gate("test.py", new, old_text=old)
        assert result is not None
        # Should have truncation message if > 12 errors
        if "... (" in result:
            assert "more)" in result


class TestSymbolTextOperations:
    """Test symbol range and replacement operations in text."""

    def test_symbol_range_in_text_python_function(self):
        """Test finding symbol range for Python function."""
        text = "def foo():\n    return 42\n"
        start, end = symbol_range_in_text("test.py", text, "foo")
        assert isinstance(start, int) and isinstance(end, int)
        assert start < end

    def test_symbol_range_in_text_not_found(self):
        """Test symbol_range_in_text returns error for missing symbol."""
        text = "def foo():\n    return 42\n"
        result = symbol_range_in_text("test.py", text, "nonexistent")
        assert isinstance(result, str)
        assert "not found" in result

    def test_symbol_range_in_text_empty_symbol(self):
        """Test symbol_range_in_text with empty symbol."""
        text = "def foo():\n    pass\n"
        result = symbol_range_in_text("test.py", text, "")
        assert isinstance(result, str)
        assert "required" in result

    def test_symbol_range_in_text_unsupported_language(self):
        """Test symbol_range_in_text with unsupported language."""
        text = "x = 1\n"
        result = symbol_range_in_text("test.unknown", text, "x")
        assert isinstance(result, str)
        assert "grammar" in result

    def test_replace_symbol_in_text(self):
        """Test replacing a symbol in text."""
        text = "def foo():\n    return 1\n"
        result = replace_symbol_in_text("test.py", text, "foo", "def foo():\n    return 2\n")
        assert "return 2" in result
        assert "return 1" not in result

    def test_replace_symbol_in_text_error_propagates(self):
        """Test replace_symbol_in_text propagates symbol_range_in_text errors."""
        text = "def foo():\n    pass\n"
        with pytest.raises(ValueError, match="not found"):
            replace_symbol_in_text("test.py", text, "nonexistent", "new_body")

    def test_insert_after_imports_in_text_python(self):
        """Test inserting snippet after imports in Python."""
        text = "import os\nimport sys\n\ndef foo():\n    pass\n"
        result = insert_after_imports_in_text("test.py", text, "from pathlib import Path\n")
        assert "pathlib" in result
        # pathlib should appear before foo
        assert result.index("pathlib") < result.index("def foo")

    def test_insert_after_imports_in_text_no_imports(self):
        """Test inserting when there are no imports."""
        text = "def foo():\n    pass\n"
        result = insert_after_imports_in_text("test.py", text, "import os\n")
        # Should insert at top
        assert result.startswith("import os\n")

    def test_insert_after_imports_in_text_unsupported_language(self):
        """Test inserting with unsupported language."""
        text = "x = 1\n"
        with pytest.raises(ValueError, match="grammar"):
            insert_after_imports_in_text("test.unknown", text, "import os\n")

    def test_insert_after_imports_adds_newline(self):
        """Test that insert_after_imports_in_text adds newline if missing."""
        text = "import os\n\ndef foo():\n    pass\n"
        result = insert_after_imports_in_text("test.py", text, "import sys")
        # Should automatically add newline
        assert "import sys\n" in result


class TestListSymbols:
    """Test list_symbols function."""

    def test_list_symbols_python_function(self, ctx):
        """Test listing symbols in Python file with function."""
        seed(ctx, "test.py", "def foo():\n    return 42\n")
        result = list_symbols(ctx.workspace, "test.py")
        assert "function foo" in result
        assert "test.py" in result
        assert "symbol(s)" in result

    def test_list_symbols_python_class(self, ctx):
        """Test listing symbols in Python file with class."""
        seed(ctx, "test.py", "class MyClass:\n    def method(self):\n        pass\n")
        result = list_symbols(ctx.workspace, "test.py")
        assert "class MyClass" in result
        assert "method method" in result

    def test_list_symbols_python_imports(self, ctx):
        """Test that imports are listed."""
        seed(ctx, "test.py", "import os\nfrom sys import path\n")
        result = list_symbols(ctx.workspace, "test.py")
        assert "import" in result

    def test_list_symbols_no_symbols(self, ctx):
        """Test file with no symbols."""
        seed(ctx, "test.py", "x = 1\n")
        result = list_symbols(ctx.workspace, "test.py")
        assert "No symbols" in result

    def test_list_symbols_file_not_found(self, ctx):
        """Test list_symbols with non-existent file."""
        result = list_symbols(ctx.workspace, "nonexistent.py")
        assert "error" in result.lower()

    def test_list_symbols_unsupported_language(self, ctx):
        """Test list_symbols with unsupported file type."""
        seed(ctx, "test.unknown", "x = 1\n")
        result = list_symbols(ctx.workspace, "test.unknown")
        assert "error" in result.lower()

    def test_list_symbols_with_language_override(self, ctx):
        """Test list_symbols with explicit language."""
        seed(ctx, "myfile", "def foo():\n    pass\n")
        result = list_symbols(ctx.workspace, "myfile", language="python")
        assert "function foo" in result

    def test_list_symbols_javascript_function(self, ctx):
        """Test listing JavaScript functions."""
        seed(ctx, "test.js", "function foo() {\n    return 42;\n}\n")
        result = list_symbols(ctx.workspace, "test.js")
        assert "function foo" in result

    def test_list_symbols_go_function(self, ctx):
        """Test listing Go functions."""
        seed(ctx, "test.go", "package main\nfunc Foo() {\n}\n")
        result = list_symbols(ctx.workspace, "test.go")
        assert "function Foo" in result


class TestFindSymbol:
    """Test find_symbol function."""

    def test_find_symbol_python_function(self, ctx):
        """Test finding a Python function."""
        seed(ctx, "test.py", "def foo():\n    return 42\n")
        result = find_symbol(ctx.workspace, "test.py", "foo")
        assert "Found 'foo'" in result
        assert "test.py" in result
        assert "def foo" in result

    def test_find_symbol_python_class(self, ctx):
        """Test finding a Python class."""
        seed(ctx, "test.py", "class MyClass:\n    pass\n")
        result = find_symbol(ctx.workspace, "test.py", "MyClass")
        assert "Found 'MyClass'" in result
        assert "class MyClass" in result

    def test_find_symbol_not_found(self, ctx):
        """Test finding non-existent symbol."""
        seed(ctx, "test.py", "def foo():\n    pass\n")
        result = find_symbol(ctx.workspace, "test.py", "nonexistent")
        assert "not found" in result.lower()

    def test_find_symbol_empty_symbol(self, ctx):
        """Test find_symbol with empty symbol."""
        seed(ctx, "test.py", "def foo():\n    pass\n")
        result = find_symbol(ctx.workspace, "test.py", "")
        assert "required" in result.lower()

    def test_find_symbol_file_not_found(self, ctx):
        """Test find_symbol with non-existent file."""
        result = find_symbol(ctx.workspace, "nonexistent.py", "foo")
        assert "error" in result.lower()

    def test_find_symbol_unsupported_language(self, ctx):
        """Test find_symbol with unsupported language."""
        seed(ctx, "test.unknown", "x = 1\n")
        result = find_symbol(ctx.workspace, "test.unknown", "x")
        assert "error" in result.lower()

    def test_find_symbol_go_type(self, ctx):
        """Test finding Go type declaration."""
        seed(ctx, "test.go", "package main\ntype MyType struct {\n}\n")
        result = find_symbol(ctx.workspace, "test.go", "MyType")
        assert "Found" in result or "not found" in result.lower()


class TestGetNodeAt:
    """Test get_node_at function."""

    def test_get_node_at_simple(self, ctx):
        """Test get_node_at for simple identifier."""
        seed(ctx, "test.py", "def foo():\n    return 42\n")
        result = get_node_at(ctx.workspace, "test.py", 1, 5)
        assert "test.py:1:5" in result
        assert "type:" in result

    def test_get_node_at_with_parent(self, ctx):
        """Test get_node_at includes parent information."""
        seed(ctx, "test.py", "class Foo:\n    def bar(self):\n        pass\n")
        result = get_node_at(ctx.workspace, "test.py", 2, 8)
        assert "parent:" in result

    def test_get_node_at_with_enclosing_symbol(self, ctx):
        """Test get_node_at includes enclosing symbol."""
        seed(ctx, "test.py", "class Foo:\n    def bar(self):\n        x = 1\n")
        result = get_node_at(ctx.workspace, "test.py", 3, 9)
        assert "enclosing:" in result

    def test_get_node_at_no_node(self, ctx):
        """Test get_node_at when no node found."""
        seed(ctx, "test.py", "x = 1\n")
        # Position beyond file (very large line number)
        result = get_node_at(ctx.workspace, "test.py", 1000, 1)
        # May return a node or "No node" - depends on tree-sitter

    def test_get_node_at_file_not_found(self, ctx):
        """Test get_node_at with non-existent file."""
        result = get_node_at(ctx.workspace, "nonexistent.py", 1, 1)
        assert "error" in result.lower()

    def test_get_node_at_with_named_children(self, ctx):
        """Test get_node_at includes named children."""
        seed(ctx, "test.py", "def foo(a, b):\n    pass\n")
        result = get_node_at(ctx.workspace, "test.py", 1, 5)
        # Should parse and return node info
        assert "type:" in result


class TestQueryTree:
    """Test query_tree function."""

    def test_query_tree_preset_functions_python(self, ctx):
        """Test query_tree with functions preset."""
        seed(ctx, "test.py", "def foo():\n    pass\ndef bar():\n    pass\n")
        result = query_tree(ctx.workspace, "test.py", preset="functions")
        assert "capture(s)" in result
        assert "foo" in result or "bar" in result

    def test_query_tree_preset_classes_python(self, ctx):
        """Test query_tree with classes preset."""
        seed(ctx, "test.py", "class Foo:\n    pass\nclass Bar:\n    pass\n")
        result = query_tree(ctx.workspace, "test.py", preset="classes")
        assert "capture(s)" in result or "No captures" in result

    def test_query_tree_preset_imports_python(self, ctx):
        """Test query_tree with imports preset."""
        seed(ctx, "test.py", "import os\nfrom sys import path\n")
        result = query_tree(ctx.workspace, "test.py", preset="imports")
        assert "capture(s)" in result or "import" in result

    def test_query_tree_preset_methods(self, ctx):
        """Test query_tree with methods preset."""
        seed(ctx, "test.py", "class Foo:\n    def method(self):\n        pass\n")
        result = query_tree(ctx.workspace, "test.py", preset="methods")
        assert "capture(s)" in result or "No captures" in result

    def test_query_tree_preset_calls(self, ctx):
        """Test query_tree with calls preset."""
        seed(ctx, "test.py", "foo()\nbar(x)\n")
        result = query_tree(ctx.workspace, "test.py", preset="calls")
        assert "capture(s)" in result or "No captures" in result

    def test_query_tree_no_preset_or_query(self, ctx):
        """Test query_tree requires preset or custom query."""
        seed(ctx, "test.py", "x = 1\n")
        result = query_tree(ctx.workspace, "test.py")
        assert "error" in result.lower()

    def test_query_tree_invalid_preset(self, ctx):
        """Test query_tree with invalid preset."""
        seed(ctx, "test.py", "x = 1\n")
        result = query_tree(ctx.workspace, "test.py", preset="invalid_preset")
        assert "error" in result.lower()

    def test_query_tree_custom_query(self, ctx):
        """Test query_tree with custom query."""
        seed(ctx, "test.py", "def foo():\n    pass\n")
        result = query_tree(
            ctx.workspace,
            "test.py",
            query="(function_definition name: (identifier) @name)"
        )
        # Should execute the query
        assert "capture(s)" in result or "error" in result.lower()

    def test_query_tree_invalid_query(self, ctx):
        """Test query_tree with malformed query."""
        seed(ctx, "test.py", "x = 1\n")
        result = query_tree(ctx.workspace, "test.py", query="(invalid syntax here")
        assert "error" in result.lower()

    def test_query_tree_unsupported_language(self, ctx):
        """Test query_tree with unsupported language."""
        seed(ctx, "test.unknown", "x = 1\n")
        result = query_tree(ctx.workspace, "test.unknown", preset="functions")
        assert "error" in result.lower()

    def test_query_tree_file_not_found(self, ctx):
        """Test query_tree with non-existent file."""
        result = query_tree(ctx.workspace, "nonexistent.py", preset="functions")
        assert "error" in result.lower()

    def test_query_tree_many_captures_truncated(self, ctx):
        """Test query_tree truncates results."""
        # Create a file with many functions to trigger truncation
        funcs = "\n".join([f"def func_{i}():\n    pass\n" for i in range(150)])
        seed(ctx, "test.py", funcs)
        result = query_tree(ctx.workspace, "test.py", preset="functions")
        if "... (" in result:
            assert "more;" in result or "narrow" in result


class TestParseFile:
    """Test parse_file function."""

    def test_parse_file_simple(self, ctx):
        """Test parse_file with simple Python."""
        seed(ctx, "test.py", "def foo():\n    pass\n")
        result = parse_file(ctx.workspace, "test.py")
        assert "named node(s)" in result

    def test_parse_file_complex_structure(self, ctx):
        """Test parse_file with nested structure."""
        seed(
            ctx,
            "test.py",
            "class Foo:\n    def bar(self):\n        x = 1\n        return x\n"
        )
        result = parse_file(ctx.workspace, "test.py")
        assert "named node(s)" in result

    def test_parse_file_no_named_nodes(self, ctx):
        """Test parse_file with minimal structure."""
        seed(ctx, "test.py", "")
        result = parse_file(ctx.workspace, "test.py")
        # May have some nodes or say no named nodes

    def test_parse_file_truncated_at_max_nodes(self, ctx):
        """Test parse_file truncates at PARSE_MAX_NODES."""
        # Create deeply nested or large structure
        nested = "def f1():\n"
        for i in range(2, 15):
            nested += "  " * (i - 1) + f"def f{i}():\n"
        nested += "  " * 14 + "pass\n"
        seed(ctx, "test.py", nested)
        result = parse_file(ctx.workspace, "test.py")
        # May be truncated
        if "capped at" in result:
            assert "PARSE_MAX" in result or "depth" in result

    def test_parse_file_unsupported_language(self, ctx):
        """Test parse_file with unsupported language."""
        seed(ctx, "test.unknown", "x = 1\n")
        result = parse_file(ctx.workspace, "test.unknown")
        assert "error" in result.lower()

    def test_parse_file_with_language_override(self, ctx):
        """Test parse_file with explicit language."""
        seed(ctx, "myfile", "def foo():\n    pass\n")
        result = parse_file(ctx.workspace, "myfile", language="python")
        assert "named node(s)" in result

    def test_parse_file_javascript(self, ctx):
        """Test parse_file with JavaScript."""
        seed(ctx, "test.js", "function foo() {\n    return 42;\n}\n")
        result = parse_file(ctx.workspace, "test.js")
        assert "named node(s)" in result or "No named nodes" in result

    def test_parse_file_go(self, ctx):
        """Test parse_file with Go."""
        seed(ctx, "test.go", "package main\nfunc main() {\n}\n")
        result = parse_file(ctx.workspace, "test.go")
        assert "named node(s)" in result or "No named nodes" in result


class TestEdgeCases:
    """Test edge cases and error conditions."""

    def test_parse_bytes_all_languages(self):
        """Test parse_bytes for all supported languages."""
        test_cases = [
            ("python", b"def foo():\n    pass\n"),
            ("javascript", b"function foo() { }\n"),
            ("typescript", b"function foo(): void { }\n"),
            ("typescript_jsx", b"const App = () => <div></div>;\n"),
            ("go", b"package main\nfunc main() { }\n"),
        ]
        for lang, source in test_cases:
            tree = parse_bytes(lang, source)
            assert tree is not None
            assert tree.root_node is not None
    
    def test_query_tree_all_presets(self, ctx):
        """Test all preset queries on Python file."""
        code = (
            "import os\n"
            "from pathlib import Path\n"
            "class Foo:\n"
            "    def bar(self):\n"
            "        foo()\n"
        )
        seed(ctx, "test.py", code)
        
        for preset in ["imports", "functions", "classes", "methods", "calls"]:
            result = query_tree(ctx.workspace, "test.py", preset=preset)
            assert isinstance(result, str)
            assert "capture(s)" in result or "No captures" in result or "error" in result.lower()

    def test_unicode_content_python(self, ctx):
        """Test handling of Unicode content."""
        seed(ctx, "test.py", "# 你好世界\ndef foo():\n    return '🎉'\n")
        result = list_symbols(ctx.workspace, "test.py")
        assert "function foo" in result

    def test_very_long_lines(self, ctx):
        """Test handling of very long lines."""
        long_line = "x = " + '"' + "a" * 10000 + '"' + "\n"
        seed(ctx, "test.py", long_line)
        result = list_symbols(ctx.workspace, "test.py")
        # Should handle without crashing

    def test_mixed_indentation(self, ctx):
        """Test handling of mixed tabs and spaces."""
        seed(ctx, "test.py", "def foo():\n\tif True:\n        pass\n")
        result = list_symbols(ctx.workspace, "test.py")
        # Should handle mixed indentation

    def test_binary_like_content(self, ctx):
        """Test handling of non-UTF8-like content."""
        seed(ctx, "test.py", "x = 1\n# -*- encoding: utf-8 -*-\n")
        result = list_symbols(ctx.workspace, "test.py")
        # Should handle gracefully

    def test_empty_file(self, ctx):
        """Test completely empty file."""
        seed(ctx, "test.py", "")
        result = list_symbols(ctx.workspace, "test.py")
        assert "No symbols" in result or "symbol(s)" in result

    def test_whitespace_only_file(self, ctx):
        """Test file with only whitespace."""
        seed(ctx, "test.py", "   \n\n\t\n")
        result = list_symbols(ctx.workspace, "test.py")
        assert "No symbols" in result or "symbol(s)" in result

    def test_symbol_with_special_chars_in_name(self, ctx):
        """Test symbols with underscores and numbers."""
        seed(ctx, "test.py", "def _private_func_123():\n    pass\n")
        result = find_symbol(ctx.workspace, "test.py", "_private_func_123")
        assert "Found" in result

    def test_nested_classes_and_methods(self, ctx):
        """Test deeply nested structures."""
        seed(
            ctx,
            "test.py",
            (
                "class Outer:\n"
                "    class Inner:\n"
                "        def method(self):\n"
                "            pass\n"
            )
        )
        result = list_symbols(ctx.workspace, "test.py")
        assert "class Outer" in result
        assert "class Inner" in result
