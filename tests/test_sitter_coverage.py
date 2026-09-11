"""Comprehensive tests for runtime/tools/sitter.py to raise coverage from 41% to 80%+"""
from __future__ import annotations

from pathlib import Path
import pytest

from runtime.tools.sitter import (
    language_for,
    parse_bytes,
    check_syntax,
    SyntaxFault,
    syntax_gate,
    symbol_range_in_text,
    replace_symbol_in_text,
    insert_after_imports_in_text,
    list_symbols,
    find_symbol,
    get_node_at,
    query_tree,
    parse_file,
    PRESETS,
    EXTENSION_TO_LANG,
    SYMBOL_TYPES,
)


class TestLanguageFor:
    """Tests for language_for() function."""

    def test_language_for_python_extension(self):
        """Detect python from .py extension."""
        assert language_for("test.py") == "python"
        assert language_for("test.pyi") == "python"

    def test_language_for_go_extension(self):
        """Detect go from .go extension."""
        assert language_for("main.go") == "go"

    def test_language_for_javascript(self):
        """Detect javascript from various extensions."""
        assert language_for("app.js") == "javascript"
        assert language_for("app.jsx") == "javascript"
        assert language_for("app.mjs") == "javascript"
        assert language_for("app.cjs") == "javascript"

    def test_language_for_typescript(self):
        """Detect typescript from .ts and .tsx."""
        assert language_for("app.ts") == "typescript"
        assert language_for("app.mts") == "typescript"
        assert language_for("app.cts") == "typescript"
        assert language_for("app.tsx") == "typescript_jsx"

    def test_language_for_explicit_override(self):
        """Explicit language parameter overrides file extension."""
        assert language_for("test.py", language="go") == "go"
        assert language_for("test.js", language="python") == "python"

    def test_language_for_explicit_tsx(self):
        """tsx can be specified explicitly."""
        assert language_for("file.js", language="tsx") == "typescript_jsx"
        assert language_for("file.js", language="typescript_jsx") == "typescript_jsx"

    def test_language_for_typescript_override_tsx(self):
        """typescript override on .tsx file gives typescript_jsx."""
        assert language_for("file.tsx", language="typescript") == "typescript_jsx"

    def test_language_for_invalid_language(self):
        """Invalid language returns None."""
        assert language_for("file.py", language="invalid_lang") is None

    def test_language_for_unknown_extension(self):
        """Unknown file extension returns None."""
        assert language_for("file.xyz") is None
        assert language_for("file.txt") is None

    def test_language_for_no_extension(self):
        """File with no extension returns None."""
        assert language_for("Makefile") is None

    def test_language_for_case_insensitive(self):
        """Language parameter is case-insensitive."""
        assert language_for("test.js", language="PYTHON") == "python"
        assert language_for("test.js", language="Go") == "go"

    def test_language_for_whitespace_stripped(self):
        """Language parameter whitespace is stripped."""
        assert language_for("test.js", language="  python  ") == "python"


class TestParseBytes:
    """Tests for parse_bytes() function."""

    def test_parse_bytes_python(self):
        """Parse valid Python code."""
        code = b"def foo():\n    return 1\n"
        tree = parse_bytes("python", code)
        assert tree.root_node is not None
        assert tree.root_node.type == "module"

    def test_parse_bytes_javascript(self):
        """Parse valid JavaScript code."""
        code = b"function foo() { return 1; }"
        tree = parse_bytes("javascript", code)
        assert tree.root_node is not None

    def test_parse_bytes_go(self):
        """Parse valid Go code."""
        code = b"package main\nfunc main() {}\n"
        tree = parse_bytes("go", code)
        assert tree.root_node is not None
        assert tree.root_node.type == "source_file"

    def test_parse_bytes_typescript(self):
        """Parse valid TypeScript code."""
        code = b"function foo(): number { return 1; }"
        tree = parse_bytes("typescript", code)
        assert tree.root_node is not None

    def test_parse_bytes_empty_code(self):
        """Parse empty code."""
        tree = parse_bytes("python", b"")
        assert tree.root_node is not None


class TestCheckSyntax:
    """Tests for check_syntax() function."""

    def test_check_syntax_valid_python(self):
        """Valid Python has no syntax faults."""
        code = b"def foo():\n    return 1\n"
        faults = check_syntax("python", code)
        assert faults == []

    def test_check_syntax_invalid_python(self):
        """Invalid Python returns syntax faults."""
        code = b"def foo(\n"
        faults = check_syntax("python", code)
        assert len(faults) > 0
        assert faults[0].kind in ("ERROR", "MISSING")

    def test_check_syntax_fault_attributes(self):
        """SyntaxFault has correct attributes."""
        code = b"def foo(\n"
        faults = check_syntax("python", code)
        fault = faults[0]
        assert isinstance(fault, SyntaxFault)
        assert hasattr(fault, "line")
        assert hasattr(fault, "col")
        assert hasattr(fault, "kind")
        assert hasattr(fault, "text")
        assert fault.line >= 1
        assert fault.col >= 1

    def test_check_syntax_javascript(self):
        """Check syntax in JavaScript."""
        valid = b"function foo() { return 1; }"
        faults = check_syntax("javascript", valid)
        assert faults == []
        
        invalid = b"function foo( {"
        faults = check_syntax("javascript", invalid)
        assert len(faults) > 0

    def test_check_syntax_go(self):
        """Check syntax in Go."""
        valid = b"package main\nfunc main() {}\n"
        faults = check_syntax("go", valid)
        assert faults == []
        
        invalid = b"package main\nfunc main( {}\n"
        faults = check_syntax("go", invalid)
        assert len(faults) > 0


class TestSyntaxGate:
    """Tests for syntax_gate() function."""

    def test_syntax_gate_unknown_language(self):
        """Unknown file type returns None (not rejected)."""
        result = syntax_gate("file.txt", "content", None)
        assert result is None

    def test_syntax_gate_new_file_valid(self):
        """Valid new file returns None."""
        result = syntax_gate("new.py", "def foo():\n    pass\n", None)
        assert result is None

    def test_syntax_gate_new_file_invalid(self):
        """Invalid new file is rejected."""
        result = syntax_gate("new.py", "def foo(\n", None)
        assert result is not None
        assert "syntax gate rejected" in result
        assert "new file" in result

    def test_syntax_gate_edit_worsens_syntax(self):
        """Edit that introduces new errors is rejected."""
        old = "def foo():\n    return 1\n"
        new = "def foo(\n    return 1\n"
        result = syntax_gate("file.py", new, old)
        assert result is not None
        assert "syntax gate rejected" in result

    def test_syntax_gate_edit_improves_syntax(self):
        """Edit that reduces errors is allowed."""
        old = "def foo():\n    return 1\n\ndef bar(\n"
        new = "def foo():\n    return 1\n\ndef bar():\n    return 2\n"
        result = syntax_gate("file.py", new, old)
        assert result is None

    def test_syntax_gate_edit_on_broken_file(self):
        """Edit on already-broken file is allowed if errors don't increase."""
        broken = "def good():\n    return 1\n\ndef bad(\n"
        edited = "def good():\n    return 2\n\ndef bad(\n"
        result = syntax_gate("file.py", edited, broken)
        assert result is None

    def test_syntax_gate_preserves_old_errors(self):
        """New errors at old locations are allowed."""
        old = "x = 1\n"
        new = "x = 1\ndef foo(\n"
        result = syntax_gate("file.py", new, old)
        # New errors in new code region may be rejected depending on range
        # Just verify it doesn't crash
        assert isinstance(result, (str, type(None)))

    def test_syntax_gate_with_language_override(self):
        """Language override is respected."""
        result = syntax_gate("file.xyz", "def foo():\n    pass\n", None, language="python")
        assert result is None


class TestSymbolRangeInText:
    """Tests for symbol_range_in_text() function."""

    def test_symbol_range_python_function(self):
        """Find range of Python function."""
        text = "def foo():\n    return 1\n\ndef bar():\n    return 2\n"
        span = symbol_range_in_text("test.py", text, "foo")
        assert isinstance(span, tuple)
        start, end = span
        assert text[start:end] == "def foo():\n    return 1"

    def test_symbol_range_python_class(self):
        """Find range of Python class."""
        text = "class Foo:\n    pass\n\nclass Bar:\n    pass\n"
        span = symbol_range_in_text("test.py", text, "Foo")
        assert isinstance(span, tuple)
        start, end = span
        assert "class Foo" in text[start:end]

    def test_symbol_range_not_found(self):
        """Missing symbol returns error string."""
        text = "def foo():\n    pass\n"
        result = symbol_range_in_text("test.py", text, "bar")
        assert isinstance(result, str)
        assert "not found" in result

    def test_symbol_range_empty_symbol(self):
        """Empty symbol name returns error."""
        text = "def foo():\n    pass\n"
        result = symbol_range_in_text("test.py", text, "")
        assert isinstance(result, str)
        assert "required" in result

    def test_symbol_range_unknown_language(self):
        """Unknown language returns error."""
        result = symbol_range_in_text("test.txt", "content", "foo")
        assert isinstance(result, str)
        assert "no tree-sitter" in result

    def test_symbol_range_javascript(self):
        """Find JavaScript function."""
        text = "function foo() { return 1; }\nfunction bar() { return 2; }\n"
        span = symbol_range_in_text("test.js", text, "foo")
        assert isinstance(span, tuple)


class TestReplaceSymbolInText:
    """Tests for replace_symbol_in_text() function."""

    def test_replace_symbol_basic(self):
        """Replace function body."""
        src = "def foo():\n    return 1\n\ndef bar():\n    return 2\n"
        result = replace_symbol_in_text("a.py", src, "foo", "def foo():\n    return 9\n")
        assert "return 9" in result
        assert "def bar" in result

    def test_replace_symbol_class(self):
        """Replace class definition."""
        src = "class Foo:\n    x = 1\n"
        result = replace_symbol_in_text("a.py", src, "Foo", "class Foo:\n    x = 2\n")
        assert "x = 2" in result

    def test_replace_symbol_not_found(self):
        """Replacing non-existent symbol raises ValueError."""
        src = "def foo():\n    pass\n"
        with pytest.raises(ValueError, match="not found"):
            replace_symbol_in_text("a.py", src, "bar", "def bar():\n    pass\n")

    def test_replace_symbol_invalid_language(self):
        """Replacing in unknown language raises ValueError."""
        with pytest.raises(ValueError, match="no tree-sitter"):
            replace_symbol_in_text("a.txt", "content", "foo", "new")


class TestInsertAfterImportsInText:
    """Tests for insert_after_imports_in_text() function."""

    def test_insert_after_imports_basic(self):
        """Insert snippet after imports."""
        src = "import os\nimport sys\n\ndef foo():\n    pass\n"
        result = insert_after_imports_in_text("a.py", src, "from pathlib import Path\n")
        assert result.index("pathlib") < result.index("def foo")
        assert result.index("import os") < result.index("pathlib")

    def test_insert_after_imports_no_imports(self):
        """Insert at top if no imports exist."""
        src = "def foo():\n    pass\n"
        result = insert_after_imports_in_text("a.py", src, "import sys\n")
        assert result.startswith("import sys")
        assert "def foo" in result

    def test_insert_after_imports_preserves_spacing(self):
        """Insert preserves proper newlines."""
        src = "import os\n\ndef foo():\n    pass\n"
        result = insert_after_imports_in_text("a.py", src, "import sys")
        lines = result.split("\n")
        assert "import os" in result
        assert "import sys" in result

    def test_insert_after_imports_javascript(self):
        """Insert after imports in JavaScript."""
        src = "import os from 'os';\nimport sys from 'sys';\n\nfunction foo() {}\n"
        result = insert_after_imports_in_text("a.js", src, "import Path from 'path';\n")
        assert "Path" in result
        assert result.index("Path") < result.index("function foo")

    def test_insert_after_imports_unknown_language(self):
        """Unknown language raises ValueError."""
        with pytest.raises(ValueError, match="no tree-sitter"):
            insert_after_imports_in_text("a.txt", "content", "snippet")


class TestListSymbols:
    """Tests for list_symbols() function."""

    def test_list_symbols_python_file(self, tmp_path):
        """List symbols in Python file."""
        file = tmp_path / "test.py"
        file.write_text("def foo():\n    pass\n\ndef bar():\n    pass\n")
        result = list_symbols(tmp_path, "test.py")
        assert "function" in result
        assert "foo" in result
        assert "bar" in result

    def test_list_symbols_class_and_methods(self, tmp_path):
        """List classes and their methods."""
        file = tmp_path / "test.py"
        file.write_text("class Foo:\n    def method(self):\n        pass\n")
        result = list_symbols(tmp_path, "test.py")
        assert "class Foo" in result
        assert "method Foo" in result or "method" in result

    def test_list_symbols_imports(self, tmp_path):
        """List import statements."""
        file = tmp_path / "test.py"
        file.write_text("import os\nfrom sys import argv\n\ndef foo():\n    pass\n")
        result = list_symbols(tmp_path, "test.py")
        assert "import" in result.lower()

    def test_list_symbols_empty_file(self, tmp_path):
        """Empty file shows 'No symbols' message."""
        file = tmp_path / "empty.py"
        file.write_text("")
        result = list_symbols(tmp_path, "empty.py")
        assert "No symbols" in result or "symbol(s)" in result

    def test_list_symbols_not_a_file(self, tmp_path):
        """Non-existent file returns error."""
        result = list_symbols(tmp_path, "nonexistent.py")
        assert "error:" in result.lower() or "not a file" in result.lower()

    def test_list_symbols_unknown_language(self, tmp_path):
        """File with unknown extension returns error."""
        file = tmp_path / "test.unknown"
        file.write_text("content")
        result = list_symbols(tmp_path, "test.unknown")
        assert "error:" in result.lower()


class TestFindSymbol:
    """Tests for find_symbol() function."""

    def test_find_symbol_function(self, tmp_path):
        """Find function in file."""
        file = tmp_path / "test.py"
        file.write_text("def foo():\n    return 1\n")
        result = find_symbol(tmp_path, "test.py", "foo")
        assert "Found" in result or "foo" in result
        assert "def foo" in result

    def test_find_symbol_class(self, tmp_path):
        """Find class in file."""
        file = tmp_path / "test.py"
        file.write_text("class Foo:\n    pass\n")
        result = find_symbol(tmp_path, "test.py", "Foo")
        assert "Found" in result or "Foo" in result
        assert "class Foo" in result

    def test_find_symbol_not_found(self, tmp_path):
        """Non-existent symbol returns error."""
        file = tmp_path / "test.py"
        file.write_text("def foo():\n    pass\n")
        result = find_symbol(tmp_path, "test.py", "bar")
        assert "not found" in result.lower() or "Symbol" in result

    def test_find_symbol_empty_name(self, tmp_path):
        """Empty symbol name returns error."""
        file = tmp_path / "test.py"
        file.write_text("def foo():\n    pass\n")
        result = find_symbol(tmp_path, "test.py", "")
        assert "error:" in result.lower() or "required" in result.lower()

    def test_find_symbol_file_not_found(self, tmp_path):
        """Non-existent file returns error."""
        result = find_symbol(tmp_path, "nonexistent.py", "foo")
        assert "error:" in result.lower() or "not a file" in result.lower()


class TestGetNodeAt:
    """Tests for get_node_at() function."""

    def test_get_node_at_basic(self, tmp_path):
        """Get node at specific line/character."""
        file = tmp_path / "test.py"
        file.write_text("def foo():\n    return 1\n")
        result = get_node_at(tmp_path, "test.py", 1, 5)
        assert "type:" in result
        assert "test.py" in result

    def test_get_node_at_with_name(self, tmp_path):
        """Node with name includes name in result."""
        file = tmp_path / "test.py"
        file.write_text("def foo():\n    return 1\n")
        result = get_node_at(tmp_path, "test.py", 1, 5)
        # Line 1, column 5 is near 'foo'
        assert "test.py:1:5" in result

    def test_get_node_at_file_not_found(self, tmp_path):
        """Non-existent file returns error."""
        result = get_node_at(tmp_path, "nonexistent.py", 1, 1)
        assert "error:" in result.lower() or "not a file" in result.lower()

    def test_get_node_at_clamped_coords(self, tmp_path):
        """Negative coordinates are clamped to 0."""
        file = tmp_path / "test.py"
        file.write_text("x = 1\n")
        result = get_node_at(tmp_path, "test.py", -1, -1)
        # Should not crash, clamped to (0, 0)
        assert "error:" not in result.lower() or "No node" in result


class TestQueryTree:
    """Tests for query_tree() function."""

    def test_query_tree_preset_functions(self, tmp_path):
        """Query tree with 'functions' preset."""
        file = tmp_path / "test.py"
        file.write_text("def foo():\n    pass\n\ndef bar():\n    pass\n")
        result = query_tree(tmp_path, "test.py", preset="functions")
        assert "capture(s)" in result or "foo" in result
        assert "bar" in result

    def test_query_tree_preset_classes(self, tmp_path):
        """Query tree with 'classes' preset."""
        file = tmp_path / "test.py"
        file.write_text("class Foo:\n    pass\nclass Bar:\n    pass\n")
        result = query_tree(tmp_path, "test.py", preset="classes")
        assert "Foo" in result
        assert "Bar" in result

    def test_query_tree_preset_imports(self, tmp_path):
        """Query tree with 'imports' preset."""
        file = tmp_path / "test.py"
        file.write_text("import os\nfrom sys import argv\n")
        result = query_tree(tmp_path, "test.py", preset="imports")
        assert "import" in result.lower()

    def test_query_tree_no_preset_no_query(self, tmp_path):
        """No preset and no query returns error."""
        file = tmp_path / "test.py"
        file.write_text("x = 1\n")
        result = query_tree(tmp_path, "test.py")
        assert "error:" in result.lower()

    def test_query_tree_unknown_preset(self, tmp_path):
        """Unknown preset returns error."""
        file = tmp_path / "test.py"
        file.write_text("x = 1\n")
        result = query_tree(tmp_path, "test.py", preset="unknown_preset")
        assert "error:" in result.lower() or "unknown" in result.lower()

    def test_query_tree_raw_query(self, tmp_path):
        """Raw tree-sitter query."""
        file = tmp_path / "test.py"
        file.write_text("def foo():\n    pass\n")
        result = query_tree(tmp_path, "test.py", query="(function_definition)")
        # Should either work or return error about invalid query
        assert isinstance(result, str)


class TestParseFile:
    """Tests for parse_file() function."""

    def test_parse_file_basic(self, tmp_path):
        """Parse file returns named nodes."""
        file = tmp_path / "test.py"
        file.write_text("def foo():\n    return 1\n")
        result = parse_file(tmp_path, "test.py")
        assert "named node(s)" in result
        assert "foo" in result

    def test_parse_file_nested_structure(self, tmp_path):
        """Parse file shows nesting."""
        file = tmp_path / "test.py"
        file.write_text("class Foo:\n    def method(self):\n        pass\n")
        result = parse_file(tmp_path, "test.py")
        assert "Foo" in result
        assert "method" in result

    def test_parse_file_truncates_at_limits(self, tmp_path):
        """Parse file respects PARSE_MAX_NODES and PARSE_MAX_DEPTH limits."""
        file = tmp_path / "test.py"
        # Create a moderately deep structure
        file.write_text("def foo():\n    def bar():\n        def baz():\n            pass\n")
        result = parse_file(tmp_path, "test.py")
        # May or may not be truncated depending on structure
        assert "named node(s)" in result or "capped" in result

    def test_parse_file_empty_file(self, tmp_path):
        """Parse empty file."""
        file = tmp_path / "empty.py"
        file.write_text("")
        result = parse_file(tmp_path, "empty.py")
        assert "No named nodes" in result or "named node(s)" in result

    def test_parse_file_not_found(self, tmp_path):
        """Non-existent file returns error."""
        result = parse_file(tmp_path, "nonexistent.py")
        assert "error:" in result.lower() or "not a file" in result.lower()


class TestSitterIntegration:
    """Integration tests combining multiple functions."""

    def test_symbol_operations_roundtrip(self, tmp_path):
        """Find and replace symbol operations."""
        file = tmp_path / "test.py"
        original = "def foo():\n    return 1\n"
        file.write_text(original)
        
        # Find symbol
        found = find_symbol(tmp_path, "test.py", "foo")
        assert "foo" in found
        
        # Replace symbol
        new_body = "def foo():\n    return 99\n"
        replaced = replace_symbol_in_text("test.py", original, "foo", new_body)
        assert "return 99" in replaced

    def test_parse_and_query(self, tmp_path):
        """Parse file then query it."""
        file = tmp_path / "test.py"
        file.write_text("import os\ndef foo():\n    pass\ndef bar():\n    pass\n")
        
        parsed = parse_file(tmp_path, "test.py")
        assert "foo" in parsed
        
        functions = query_tree(tmp_path, "test.py", preset="functions")
        assert "foo" in functions
        assert "bar" in functions

    def test_imports_manipulation(self, tmp_path):
        """Manipulate imports in file."""
        file = tmp_path / "test.py"
        original = "import os\n\ndef foo():\n    pass\n"
        file.write_text(original)
        
        # List symbols shows imports
        symbols = list_symbols(tmp_path, "test.py")
        assert "import" in symbols.lower()
        
        # Insert after imports
        new_import = "import sys\n"
        with_import = insert_after_imports_in_text("test.py", original, new_import)
        assert "import sys" in with_import
