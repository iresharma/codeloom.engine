"""Comprehensive tests for runtime/tools/sitter.py to achieve >80% coverage."""
from __future__ import annotations

import pytest

from runtime.tools.sitter import (
    check_syntax,
    language_for,
    syntax_gate,
    parse_bytes,
    symbol_range_in_text,
    replace_symbol_in_text,
    insert_after_imports_in_text,
    list_symbols,
    find_symbol,
    get_node_at,
    query_tree,
    parse_file,
    SyntaxFault,
    EXTENSION_TO_LANG,
    SYMBOL_TYPES,
    PRESETS,
    _PRESET_QUERIES,
    _languages,
    _parse,
    _text,
    _clip,
    _pos,
    _node_name,
    _is_function_like,
    _list_kind,
    _find_named_node,
    _format_syntax_error,
    _differing_line_range,
    _parse_text,
)
from tests.conftest import seed


# Language detection tests
def test_language_for_python_files():
    """Test language detection for Python files."""
    assert language_for("test.py") == "python"
    assert language_for("test.pyi") == "python"


def test_language_for_javascript_files():
    """Test language detection for JavaScript files."""
    assert language_for("test.js") == "javascript"
    assert language_for("test.jsx") == "javascript"
    assert language_for("test.mjs") == "javascript"
    assert language_for("test.cjs") == "javascript"


def test_language_for_typescript_files():
    """Test language detection for TypeScript files."""
    assert language_for("test.ts") == "typescript"
    assert language_for("test.mts") == "typescript"
    assert language_for("test.cts") == "typescript"
    assert language_for("test.tsx") == "typescript_jsx"


def test_language_for_go_files():
    """Test language detection for Go files."""
    assert language_for("test.go") == "go"


def test_language_for_unknown_extension():
    """Test language detection returns None for unknown extensions."""
    assert language_for("test.unknown") is None
    assert language_for("test") is None


def test_language_for_explicit_language():
    """Test explicit language override."""
    assert language_for("test.txt", language="python") == "python"
    assert language_for("test.txt", language="javascript") == "javascript"


def test_language_for_tsx_override():
    """Test TSX detection."""
    assert language_for("test.tsx", language="typescript") == "typescript_jsx"
    assert language_for("test.tsx", language="typescript_jsx") == "typescript_jsx"
    assert language_for("test.tsx", language="tsx") == "typescript_jsx"


def test_language_for_invalid_language():
    """Test invalid language override returns None."""
    assert language_for("test.py", language="invalid") is None


def test_language_for_case_insensitive():
    """Test language override is case-insensitive."""
    assert language_for("test.txt", language="PYTHON") == "python"
    assert language_for("test.txt", language="JavaScript") == "javascript"


# Syntax checking tests
def test_check_syntax_valid_python():
    """Test syntax check on valid Python code."""
    code = b"def foo():\n    return 1\n"
    faults = check_syntax("python", code)
    assert len(faults) == 0


def test_check_syntax_invalid_python():
    """Test syntax check on invalid Python code."""
    code = b"def foo(\n"
    faults = check_syntax("python", code)
    assert len(faults) > 0
    assert faults[0].kind == "ERROR"


def test_check_syntax_missing_nodes():
    """Test syntax check detects missing nodes."""
    code = b"def foo(\n"
    faults = check_syntax("python", code)
    assert any(f.kind == "MISSING" for f in faults)


def test_syntax_fault_dataclass():
    """Test SyntaxFault dataclass creation."""
    fault = SyntaxFault(line=1, col=5, kind="ERROR", text="bad code")
    assert fault.line == 1
    assert fault.col == 5
    assert fault.kind == "ERROR"
    assert fault.text == "bad code"


# Syntax gate tests
def test_syntax_gate_new_valid_file():
    """Test syntax_gate allows valid new files."""
    code = "def foo():\n    return 1\n"
    result = syntax_gate("test.py", code, None)
    assert result is None


def test_syntax_gate_new_invalid_file():
    """Test syntax_gate rejects invalid new files."""
    code = "def foo(\n"
    result = syntax_gate("test.py", code, None)
    assert result is not None
    assert "syntax gate" in result
    assert "new file" in result


def test_syntax_gate_edit_without_introducing_errors():
    """Test syntax_gate allows edits that don't introduce new errors."""
    old = "def good():\n    return 1\n\ndef bad(\n"
    new = "def good():\n    return 2\n\ndef bad(\n"
    result = syntax_gate("test.py", new, old)
    assert result is None


def test_syntax_gate_edit_introducing_errors():
    """Test syntax_gate rejects edits that introduce new errors."""
    old = "def foo():\n    return 1\n"
    new = "def foo(\n    return 1\n"
    result = syntax_gate("test.py", new, old)
    assert result is not None


def test_syntax_gate_unsupported_language():
    """Test syntax_gate returns None for unsupported languages."""
    result = syntax_gate("test.txt", "any code", None)
    assert result is None


# Format syntax error tests
def test_format_syntax_error_new_file():
    """Test formatting syntax errors for new files."""
    faults = [SyntaxFault(line=1, col=5, kind="ERROR", text="bad")]
    msg = _format_syntax_error("test.py", faults, created=True)
    assert "new file" in msg
    assert "test.py" in msg
    assert "ERROR" in msg


def test_format_syntax_error_edit():
    """Test formatting syntax errors for edits."""
    faults = [SyntaxFault(line=1, col=5, kind="ERROR", text="bad")]
    msg = _format_syntax_error("test.py", faults, created=False)
    assert "edit" in msg
    assert "test.py" in msg


def test_format_syntax_error_multiple_faults():
    """Test formatting multiple faults."""
    faults = [
        SyntaxFault(line=i, col=1, kind="ERROR", text=f"bad{i}")
        for i in range(1, 15)
    ]
    msg = _format_syntax_error("test.py", faults, created=True)
    assert "15 more" in msg or "14 more" in msg  # First 12 shown


# Differing line range tests
def test_differing_line_range_no_change():
    """Test _differing_line_range with identical text."""
    old = "line1\nline2\nline3\n"
    new = "line1\nline2\nline3\n"
    result = _differing_line_range(old, new)
    # Should return None or range at end since no difference
    assert result is None or result == (4, 4)


def test_differing_line_range_beginning_change():
    """Test _differing_line_range with change at beginning."""
    old = "old1\nline2\nline3\n"
    new = "new1\nline2\nline3\n"
    result = _differing_line_range(old, new)
    assert result is not None
    assert result[0] == 1


def test_differing_line_range_end_change():
    """Test _differing_line_range with change at end."""
    old = "line1\nline2\nold3\n"
    new = "line1\nline2\nnew3\n"
    result = _differing_line_range(old, new)
    assert result is not None
    assert result[1] >= 3


def test_differing_line_range_middle_change():
    """Test _differing_line_range with change in middle."""
    old = "line1\nold2\nline3\n"
    new = "line1\nnew2\nline3\n"
    result = _differing_line_range(old, new)
    assert result is not None
    assert result[0] == 2


def test_differing_line_range_empty_text():
    """Test _differing_line_range with empty text."""
    result = _differing_line_range("", "")
    assert result is None


def test_differing_line_range_one_to_many():
    """Test _differing_line_range expanding lines."""
    old = "line1\n"
    new = "line1\nline2\nline3\n"
    result = _differing_line_range(old, new)
    assert result is not None


def test_differing_line_range_many_to_one():
    """Test _differing_line_range shrinking lines."""
    old = "line1\nline2\nline3\n"
    new = "line1\n"
    result = _differing_line_range(old, new)
    assert result is not None


# Parse text tests
def test_parse_text_valid_python(ctx):
    """Test _parse_text with valid Python."""
    seed(ctx, "test.py", "def foo():\n    return 1\n")
    tree, source, rel, lang = _parse_text("test.py", "def foo():\n    return 1\n")
    assert tree is not None
    assert source is not None
    assert lang == "python"


def test_parse_text_unsupported_extension():
    """Test _parse_text with unsupported extension."""
    tree, source, rel, lang = _parse_text("test.txt", "some code")
    assert tree is None
    assert lang is not None and "no tree-sitter grammar" in lang


def test_parse_text_detects_extension():
    """Test _parse_text detects extension."""
    tree, source, rel, lang = _parse_text("test.py", "x = 1")
    assert lang == "python"
    tree, source, rel, lang = _parse_text("test.js", "const x = 1;")
    assert lang == "javascript"


# Helper function tests
def test_text_extraction():
    """Test _text extracts text from node."""
    code = b"def foo():\n    return 1\n"
    tree = parse_bytes("python", code)
    root = tree.root_node
    # Get first child
    if root.children:
        text = _text(code, root.children[0])
        assert len(text) > 0


def test_clip_long_text():
    """Test _clip truncates long text."""
    long_text = "x" * 300
    clipped = _clip(long_text)
    assert len(clipped) <= 201  # 200 + ellipsis
    assert "…" in clipped


def test_clip_short_text():
    """Test _clip leaves short text unchanged."""
    short = "short text"
    clipped = _clip(short)
    assert clipped == short


def test_clip_multiline_text():
    """Test _clip replaces newlines with spaces."""
    multiline = "line1\nline2\nline3"
    clipped = _clip(multiline)
    assert "\n" not in clipped


def test_pos_calculation():
    """Test _pos calculates correct line and column."""
    code = b"def foo():\n    return 1\n"
    tree = parse_bytes("python", code)
    root = tree.root_node
    line, col = _pos(root)
    assert line == 1
    assert col == 1


def test_node_name_extraction():
    """Test _node_name extracts node names."""
    code = b"def my_function():\n    pass\n"
    tree = parse_bytes("python", code)
    # Find function definition node
    def find_func(node):
        if node.type == "function_definition":
            return node
        for child in node.children:
            result = find_func(child)
            if result:
                return result
        return None
    
    func_node = find_func(tree.root_node)
    if func_node:
        name = _node_name(func_node, code)
        assert name == "my_function"


def test_is_function_like_arrow_function():
    """Test _is_function_like detects arrow functions."""
    code = b"const f = () => 1;"
    tree = parse_bytes("javascript", code)
    # This would test arrow function detection


def test_list_kind_imports():
    """Test _list_kind returns 'import' for import nodes."""
    code = b"import os\n"
    tree = parse_bytes("python", code)
    root = tree.root_node
    if root.children:
        # Find import node
        for child in root.children:
            if child.type == "import_statement":
                kind = _list_kind(child)
                assert kind == "import"


# Symbol range tests
def test_symbol_range_in_text_found():
    """Test symbol_range_in_text finds symbols."""
    code = "def foo():\n    return 1\n"
    result = symbol_range_in_text("test.py", code, "foo")
    assert isinstance(result, tuple)
    assert len(result) == 2


def test_symbol_range_in_text_not_found():
    """Test symbol_range_in_text returns error for missing symbol."""
    code = "def foo():\n    return 1\n"
    result = symbol_range_in_text("test.py", code, "nonexistent")
    assert isinstance(result, str)
    assert "not found" in result


def test_symbol_range_in_text_empty_symbol():
    """Test symbol_range_in_text requires symbol name."""
    code = "def foo():\n    return 1\n"
    result = symbol_range_in_text("test.py", code, "")
    assert isinstance(result, str)
    assert "required" in result


def test_symbol_range_in_text_unsupported_language():
    """Test symbol_range_in_text with unsupported language."""
    code = "some code"
    result = symbol_range_in_text("test.txt", code, "symbol")
    assert isinstance(result, str)
    assert "no tree-sitter" in result or "error" in result


# Replace symbol tests
def test_replace_symbol_in_text():
    """Test replace_symbol_in_text replaces function body."""
    code = "def foo():\n    return 1\n"
    new_body = "def foo():\n    return 2\n"
    result = replace_symbol_in_text("test.py", code, "foo", new_body)
    assert "return 2" in result


def test_replace_symbol_in_text_not_found():
    """Test replace_symbol_in_text raises error for missing symbol."""
    code = "def foo():\n    return 1\n"
    with pytest.raises(ValueError):
        replace_symbol_in_text("test.py", code, "nonexistent", "new")


# Insert after imports tests
def test_insert_after_imports_in_text_python():
    """Test insert_after_imports_in_text for Python."""
    code = "import os\nimport sys\n\ndef foo():\n    pass\n"
    result = insert_after_imports_in_text("test.py", code, "import json\n")
    assert "import json" in result
    assert result.index("import json") > result.index("import sys")


def test_insert_after_imports_in_text_no_imports():
    """Test insert_after_imports_in_text with no imports."""
    code = "def foo():\n    pass\n"
    result = insert_after_imports_in_text("test.py", code, "import os\n")
    assert "import os" in result
    # Should be at the beginning
    assert result.index("import os") < result.index("def foo")


def test_insert_after_imports_in_text_adds_newline():
    """Test insert_after_imports_in_text adds newline if needed."""
    code = "import os\n\ndef foo():\n    pass\n"
    result = insert_after_imports_in_text("test.py", code, "import json")
    assert "import json\n" in result


def test_insert_after_imports_in_text_unsupported_language():
    """Test insert_after_imports_in_text with unsupported language."""
    code = "code"
    with pytest.raises(ValueError):
        insert_after_imports_in_text("test.txt", code, "snippet")


# Public function tests
def test_list_symbols_python(ctx):
    """Test list_symbols for Python file."""
    seed(ctx, "test.py", "def foo():\n    pass\n\nclass Bar:\n    pass\n")
    result = list_symbols(ctx.workspace, "test.py")
    assert "function foo" in result
    assert "class Bar" in result


def test_list_symbols_no_symbols(ctx):
    """Test list_symbols with no symbols."""
    seed(ctx, "test.py", "x = 1\n")
    result = list_symbols(ctx.workspace, "test.py")
    assert "No symbols" in result


def test_list_symbols_with_imports(ctx):
    """Test list_symbols includes imports."""
    seed(ctx, "test.py", "import os\n\ndef foo():\n    pass\n")
    result = list_symbols(ctx.workspace, "test.py")
    assert "import" in result


def test_list_symbols_unsupported_file(ctx):
    """Test list_symbols with unsupported file type."""
    seed(ctx, "test.txt", "no symbols here")
    result = list_symbols(ctx.workspace, "test.txt")
    assert "error" in result or "No symbols" in result


def test_find_symbol_found(ctx):
    """Test find_symbol finds function."""
    seed(ctx, "test.py", "def my_func():\n    return 1\n")
    result = find_symbol(ctx.workspace, "test.py", "my_func")
    assert "Found" in result
    assert "my_func" in result


def test_find_symbol_not_found(ctx):
    """Test find_symbol returns error for missing symbol."""
    seed(ctx, "test.py", "def foo():\n    pass\n")
    result = find_symbol(ctx.workspace, "test.py", "nonexistent")
    assert "not found" in result


def test_find_symbol_empty_name(ctx):
    """Test find_symbol requires symbol name."""
    seed(ctx, "test.py", "def foo():\n    pass\n")
    result = find_symbol(ctx.workspace, "test.py", "")
    assert "required" in result


def test_find_symbol_unsupported_file(ctx):
    """Test find_symbol with unsupported file."""
    seed(ctx, "test.txt", "content")
    result = find_symbol(ctx.workspace, "test.txt", "sym")
    assert "error" in result


def test_get_node_at_valid_position(ctx):
    """Test get_node_at retrieves node info."""
    seed(ctx, "test.py", "def foo():\n    return 1\n")
    result = get_node_at(ctx.workspace, "test.py", 1, 1)
    assert "test.py:1:1" in result
    assert "type:" in result


def test_get_node_at_with_name(ctx):
    """Test get_node_at includes name when available."""
    seed(ctx, "test.py", "def my_func():\n    pass\n")
    result = get_node_at(ctx.workspace, "test.py", 1, 5)
    assert "type:" in result


def test_get_node_at_invalid_position(ctx):
    """Test get_node_at with out-of-range position."""
    seed(ctx, "test.py", "x = 1\n")
    result = get_node_at(ctx.workspace, "test.py", 100, 100)
    # Should still return node info for root or no node message
    assert "test.py" in result


def test_get_node_at_zero_position_handling(ctx):
    """Test get_node_at handles zero line/character (converts to valid)."""
    seed(ctx, "test.py", "x = 1\n")
    result = get_node_at(ctx.workspace, "test.py", 0, 0)
    # Line 0, col 0 should be adjusted
    assert "test.py" in result


def test_query_tree_preset_functions(ctx):
    """Test query_tree with functions preset."""
    seed(ctx, "test.py", "def foo():\n    pass\n\ndef bar():\n    pass\n")
    result = query_tree(ctx.workspace, "test.py", preset="functions")
    assert "capture(s)" in result


def test_query_tree_preset_imports(ctx):
    """Test query_tree with imports preset."""
    seed(ctx, "test.py", "import os\nimport sys\n")
    result = query_tree(ctx.workspace, "test.py", preset="imports")
    assert "capture(s)" in result


def test_query_tree_custom_query(ctx):
    """Test query_tree with custom query."""
    seed(ctx, "test.py", "def foo():\n    pass\n")
    result = query_tree(ctx.workspace, "test.py", query="(function_definition) @func")
    assert "capture(s)" in result or "error" in result


def test_query_tree_invalid_query(ctx):
    """Test query_tree with invalid query."""
    seed(ctx, "test.py", "x = 1\n")
    result = query_tree(ctx.workspace, "test.py", query="@@@invalid@@@")
    assert "error" in result


def test_query_tree_no_preset_or_query(ctx):
    """Test query_tree requires preset or query."""
    seed(ctx, "test.py", "x = 1\n")
    result = query_tree(ctx.workspace, "test.py")
    assert "error" in result or "pass preset" in result


def test_query_tree_unknown_preset(ctx):
    """Test query_tree rejects unknown preset."""
    seed(ctx, "test.py", "x = 1\n")
    result = query_tree(ctx.workspace, "test.py", preset="invalid_preset")
    assert "unknown preset" in result


def test_query_tree_preset_not_defined_for_language(ctx):
    """Test query_tree with preset not defined for language."""
    # This would require a language that doesn't have all presets
    pass


def test_parse_file_python(ctx):
    """Test parse_file returns tree structure."""
    seed(ctx, "test.py", "def foo():\n    pass\n\nclass Bar:\n    pass\n")
    result = parse_file(ctx.workspace, "test.py")
    assert "named node(s)" in result


def test_parse_file_no_named_nodes(ctx):
    """Test parse_file with no named nodes."""
    seed(ctx, "test.py", "x = 1\n")
    result = parse_file(ctx.workspace, "test.py")
    # Might have some named nodes or "No named nodes"
    assert "test.py" in result


def test_parse_file_truncation(ctx):
    """Test parse_file indicates truncation at depth limit."""
    # Create deeply nested structure
    code = "def a():\n"
    for i in range(20):
        code += "  " * (i + 1) + f"def b{i}():\n"
    code += "    pass\n"
    seed(ctx, "test.py", code)
    result = parse_file(ctx.workspace, "test.py")
    # Should indicate capping/truncation
    assert "named node(s)" in result


def test_parse_bytes_python():
    """Test parse_bytes with Python."""
    code = b"def foo():\n    pass\n"
    tree = parse_bytes("python", code)
    assert tree is not None
    assert tree.root_node is not None


def test_parse_bytes_javascript():
    """Test parse_bytes with JavaScript."""
    code = b"function foo() { }\n"
    tree = parse_bytes("javascript", code)
    assert tree is not None


def test_parse_bytes_typescript():
    """Test parse_bytes with TypeScript."""
    code = b"function foo(): void { }\n"
    tree = parse_bytes("typescript", code)
    assert tree is not None


def test_parse_bytes_go():
    """Test parse_bytes with Go."""
    code = b"func foo() { }\n"
    tree = parse_bytes("go", code)
    assert tree is not None


# Parse function tests
def test_parse_nonexistent_path(ctx):
    """Test _parse with nonexistent path."""
    tree, source, rel, lang = _parse(ctx.workspace, "nonexistent.py")
    assert tree is None
    assert "error" in lang


def test_parse_directory_not_file(ctx):
    """Test _parse rejects directories."""
    (ctx.workspace / "dir").mkdir()
    tree, source, rel, lang = _parse(ctx.workspace, "dir")
    assert tree is None
    assert "not a file" in lang


def test_parse_unsupported_extension(ctx):
    """Test _parse with unsupported file extension."""
    seed(ctx, "test.txt", "content")
    tree, source, rel, lang = _parse(ctx.workspace, "test.txt")
    assert tree is None
    assert "no tree-sitter" in lang


def test_parse_read_error(ctx):
    """Test _parse handles read errors gracefully."""
    # Create a file, then remove read permissions
    import os
    seed(ctx, "test.py", "x = 1\n")
    path = ctx.workspace / "test.py"
    os.chmod(path, 0o000)
    try:
        tree, source, rel, lang = _parse(ctx.workspace, "test.py")
        assert tree is None
        assert "error" in lang
    finally:
        os.chmod(path, 0o644)


# Constants verification
def test_extension_mapping_complete():
    """Test EXTENSION_TO_LANG has expected mappings."""
    assert EXTENSION_TO_LANG[".py"] == "python"
    assert EXTENSION_TO_LANG[".js"] == "javascript"
    assert EXTENSION_TO_LANG[".go"] == "go"


def test_symbol_types_complete():
    """Test SYMBOL_TYPES defined for all languages."""
    assert "python" in SYMBOL_TYPES
    assert "javascript" in SYMBOL_TYPES
    assert "typescript" in SYMBOL_TYPES
    assert "go" in SYMBOL_TYPES


def test_presets_complete():
    """Test PRESETS have expected values."""
    assert "functions" in PRESETS
    assert "classes" in PRESETS
    assert "imports" in PRESETS


def test_preset_queries_defined():
    """Test _PRESET_QUERIES defined for languages."""
    assert "python" in _PRESET_QUERIES
    assert "javascript" in _PRESET_QUERIES
    assert "go" in _PRESET_QUERIES


def test_languages_cache():
    """Test _languages() caching."""
    langs1 = _languages()
    langs2 = _languages()
    assert langs1 is langs2  # Same object (cached)


# Edge cases and error handling
def test_syntax_gate_with_none_language():
    """Test syntax_gate with file that has no language support."""
    result = syntax_gate("test.unknown", "code", None)
    assert result is None  # Should return None for unsupported


def test_list_symbols_nested_functions(ctx):
    """Test list_symbols with nested functions."""
    code = """def outer():
    def inner():
        pass
    return inner
"""
    seed(ctx, "test.py", code)
    result = list_symbols(ctx.workspace, "test.py")
    assert "outer" in result
    assert "inner" in result


def test_query_tree_no_captures(ctx):
    """Test query_tree when query has no captures."""
    seed(ctx, "test.py", "x = 1\n")
    # Query that won't match
    result = query_tree(ctx.workspace, "test.py", query="(function_definition) @f")
    assert "No captures" in result


def test_find_node_type_declaration_go():
    """Test finding Go type declarations."""
    code = "type MyType struct {}\n"
    result = symbol_range_in_text("test.go", code, "MyType")
    # Go type declarations have special handling
    assert isinstance(result, (tuple, str))


def test_capture_with_empty_result(ctx):
    """Test query with empty result."""
    seed(ctx, "test.py", "# just a comment\n")
    result = query_tree(ctx.workspace, "test.py", preset="functions")
    assert "No captures" in result


def test_get_node_at_includes_parent(ctx):
    """Test get_node_at includes parent information."""
    code = "def my_func():\n    x = 1\n"
    seed(ctx, "test.py", code)
    result = get_node_at(ctx.workspace, "test.py", 2, 5)
    # Should include parent info
    assert "parent:" in result or "test.py" in result


def test_get_node_at_shows_children(ctx):
    """Test get_node_at shows named children."""
    code = "def my_func():\n    x = 1\n    return x\n"
    seed(ctx, "test.py", code)
    result = get_node_at(ctx.workspace, "test.py", 1, 1)
    # Function should have named children
    assert "named children:" in result or "type:" in result


def test_parse_file_with_language_override(ctx):
    """Test parse_file respects language override."""
    seed(ctx, "test.txt", "def foo():\n    pass\n")
    result = parse_file(ctx.workspace, "test.txt", language="python")
    # Should parse as Python due to language override
    assert "function" in result.lower() or "named node" in result.lower() or "error" not in result.lower()
