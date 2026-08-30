from __future__ import annotations

from pathlib import Path

from runtime.tools.fs import WorkspacePathError, relative_posix, resolve_in_workspace

SNIPPET_MAX = 200
QUERY_MAX = 80
PARSE_MAX_NODES = 200
PARSE_MAX_DEPTH = 8

EXTENSION_TO_LANG = {
    ".py": "python",
    ".pyi": "python",
    ".go": "go",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "typescript_jsx",
}

SYMBOL_TYPES = {
    "python": {"function_definition", "class_definition"},
    "javascript": {
        "function_declaration",
        "class_declaration",
        "method_definition",
        "variable_declarator",
    },
    "typescript": {
        "function_declaration",
        "class_declaration",
        "method_definition",
        "variable_declarator",
        "interface_declaration",
        "type_alias_declaration",
    },
    "typescript_jsx": {
        "function_declaration",
        "class_declaration",
        "method_definition",
        "variable_declarator",
        "interface_declaration",
        "type_alias_declaration",
    },
    "go": {"function_declaration", "method_declaration", "type_declaration"},
}

IMPORT_TYPES = {
    "import_statement",
    "import_from_statement",
    "import_declaration",
    "import_spec",
}

KIND_BY_TYPE = {
    "function_definition": "function",
    "function_declaration": "function",
    "class_definition": "class",
    "class_declaration": "class",
    "method_definition": "method",
    "method_declaration": "method",
    "interface_declaration": "interface",
    "type_declaration": "type",
    "type_alias_declaration": "type",
    "variable_declarator": "variable",
    "import_statement": "import",
    "import_from_statement": "import",
    "import_declaration": "import",
    "import_spec": "import",
}

PRESETS = ("imports", "functions", "classes", "methods", "calls")

_PRESET_QUERIES = {
    "python": {
        "functions": "(function_definition name: (identifier) @name)",
        "classes": "(class_definition name: (identifier) @name)",
        "methods": (
            "(class_definition body: (block (function_definition name: (identifier) @name)))"
        ),
        "imports": (
            "(import_statement) @import\n"
            "(import_from_statement) @import"
        ),
        "calls": (
            "(call function: (identifier) @name)\n"
            "(call function: (attribute attribute: (identifier) @name))"
        ),
    },
    "javascript": {
        "functions": (
            "(function_declaration name: (identifier) @name)\n"
            "(variable_declarator name: (identifier) @name value: (arrow_function))\n"
            "(variable_declarator name: (identifier) @name value: (function_expression))"
        ),
        "classes": "(class_declaration name: (identifier) @name)",
        "methods": "(method_definition name: (property_identifier) @name)",
        "imports": "(import_statement) @import",
        "calls": (
            "(call_expression function: (identifier) @name)\n"
            "(call_expression function: (member_expression property: (property_identifier) @name))"
        ),
    },
    "go": {
        "functions": "(function_declaration name: (identifier) @name)",
        "classes": "(type_declaration (type_spec name: (type_identifier) @name))",
        "methods": "(method_declaration name: (field_identifier) @name)",
        "imports": "(import_declaration) @import\n(import_spec) @import",
        "calls": (
            "(call_expression function: (identifier) @name)\n"
            "(call_expression function: (selector_expression field: (field_identifier) @name))"
        ),
    },
}

_PRESET_QUERIES["typescript"] = {
    **_PRESET_QUERIES["javascript"],
    "classes": (
        "(class_declaration name: (type_identifier) @name)\n"
        "(class_declaration name: (identifier) @name)\n"
        "(interface_declaration name: (type_identifier) @name)\n"
        "(type_alias_declaration name: (type_identifier) @name)"
    ),
}
_PRESET_QUERIES["typescript_jsx"] = dict(_PRESET_QUERIES["typescript"])

_PARSE_TYPES = set().union(*SYMBOL_TYPES.values()) | {
    "module",
    "program",
    "source_file",
    "decorated_definition",
    "export_statement",
}

_LANGUAGES: dict | None = None


def _languages():
    global _LANGUAGES
    if _LANGUAGES is not None:
        return _LANGUAGES
    import tree_sitter_go as tsgo
    import tree_sitter_javascript as tsjs
    import tree_sitter_python as tspython
    import tree_sitter_typescript as tsts
    from tree_sitter import Language

    _LANGUAGES = {
        "python": Language(tspython.language()),
        "javascript": Language(tsjs.language()),
        "typescript": Language(tsts.language_typescript()),
        "typescript_jsx": Language(tsts.language_tsx()),
        "go": Language(tsgo.language()),
    }
    return _LANGUAGES


def language_for(path: str, language: str = "") -> str | None:
    if language:
        key = language.strip().lower()
        if key in ("tsx", "typescript_jsx"):
            return "typescript_jsx"
        if key == "typescript" and Path(path).suffix.lower() == ".tsx":
            return "typescript_jsx"
        if key in _languages():
            return key
        return None
    return EXTENSION_TO_LANG.get(Path(path).suffix.lower())


def _parse(workspace: Path, path: str, language: str = ""):
    try:
        resolved = resolve_in_workspace(workspace, path)
    except WorkspacePathError as exc:
        return None, None, None, f"error: {exc}"
    if not resolved.is_file():
        return None, None, None, f"error: not a file: {path}"
    lang = language_for(path, language)
    if lang is None:
        ext = Path(path).suffix or "(none)"
        return None, None, None, (
            f"error: no tree-sitter grammar for '{path}' "
            f"(extension {ext}; supported: python, go, javascript, typescript)"
        )
    try:
        source = resolved.read_bytes()
    except OSError as exc:
        return None, None, None, f"error: {exc}"
    from tree_sitter import Parser

    parser = Parser(_languages()[lang])
    tree = parser.parse(source)
    rel = relative_posix(workspace, resolved)
    return tree, source, rel, lang


def _text(source: bytes, node) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _clip(text: str) -> str:
    text = text.replace("\n", " ").strip()
    if len(text) > SNIPPET_MAX:
        return text[:SNIPPET_MAX] + "…"
    return text


def _pos(node) -> tuple[int, int]:
    return node.start_point[0] + 1, node.start_point[1] + 1


def _node_name(node, source: bytes) -> str | None:
    name_node = node.child_by_field_name("name")
    if name_node is not None:
        return _text(source, name_node)
    if node.type == "type_declaration":
        for child in node.children:
            if child.type == "type_spec":
                inner = child.child_by_field_name("name")
                if inner is not None:
                    return _text(source, inner)
    return None


def _is_function_like(node) -> bool:
    value = node.child_by_field_name("value")
    if value is None:
        return False
    return value.type in ("arrow_function", "function_expression", "function")


def _list_kind(node) -> str | None:
    if node.type in IMPORT_TYPES:
        return "import"
    kind = KIND_BY_TYPE.get(node.type)
    if kind == "variable" and not _is_function_like(node):
        return None
    return kind


def _find_named_node(root, symbol_types: set[str], symbol: str, source: bytes):
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in symbol_types:
            name = _node_name(node, source)
            if name == symbol:
                return node
        stack.extend(node.children)
    return None


def _captures(language, root, query_source: str) -> list[tuple[object, object]]:
    from tree_sitter import Query, QueryError

    try:
        query = Query(language, query_source)
        raw = query.captures(root)
    except QueryError as exc:
        raise ValueError(str(exc)) from exc
    if isinstance(raw, dict):
        out: list[tuple[object, object]] = []
        for name, nodes in raw.items():
            for node in nodes:
                out.append((node, name))
        return out
    names = getattr(query, "capture_names", None)
    out = []
    for item in raw:
        node, cap = item[0], item[1]
        if isinstance(cap, int) and names:
            cap = names[cap]
        out.append((node, cap))
    return out


def list_symbols(workspace: Path, path: str, language: str = "") -> str:
    tree, source, rel, lang = _parse(workspace, path, language)
    if tree is None:
        return lang
    types = set(SYMBOL_TYPES.get(lang, set())) | IMPORT_TYPES
    lines: list[str] = []

    def walk(node, depth: int) -> None:
        kind = _list_kind(node) if node.type in types else None
        name = None
        next_depth = depth
        if kind is not None:
            if kind == "import":
                name = _clip(_text(source, node))
            else:
                name = _node_name(node, source)
            if name:
                line, col = _pos(node.child_by_field_name("name") or node)
                if kind == "import":
                    lines.append(f"{'  ' * depth}{name}  {rel}:{line}:{col}")
                else:
                    if kind == "function" and depth > 0:
                        kind = "method"
                    lines.append(f"{'  ' * depth}{kind} {name}  {rel}:{line}:{col}")
                next_depth = depth + 1
        for child in node.children:
            walk(child, next_depth)

    walk(tree.root_node, 0)
    if not lines:
        return f"No symbols in '{rel}'"
    return f"{len(lines)} symbol(s) in {rel}\n" + "\n".join(lines)


def find_symbol(workspace: Path, path: str, symbol: str, language: str = "") -> str:
    if not symbol:
        return "error: symbol is required"
    tree, source, rel, lang = _parse(workspace, path, language)
    if tree is None:
        return lang
    types = SYMBOL_TYPES.get(lang, set())
    node = _find_named_node(tree.root_node, types, symbol, source)
    if node is None:
        return f"Symbol '{symbol}' not found in '{rel}'"
    name_node = node.child_by_field_name("name")
    if name_node is None and node.type == "type_declaration":
        for child in node.children:
            if child.type == "type_spec" and child.child_by_field_name("name"):
                name_node = child.child_by_field_name("name")
                break
    name_node = name_node or node
    line, col = _pos(name_node)
    body = _text(source, node)
    return (
        f"Found '{symbol}' at {rel}:{line}:{col} "
        f"(pass this line/character to goto_definition, find_references, or hover)\n\n"
        f"{body}"
    )


def get_node_at(
    workspace: Path,
    path: str,
    line: int,
    character: int,
    language: str = "",
) -> str:
    tree, source, rel, lang = _parse(workspace, path, language)
    if tree is None:
        return lang
    row = max(0, line - 1)
    col = max(0, character - 1)
    node = tree.root_node.descendant_for_point_range((row, col), (row, col))
    if node is None:
        return f"No node at {rel}:{line}:{character}"
    lines = [
        f"{rel}:{line}:{character}",
        f"type: {node.type}",
    ]
    name = _node_name(node, source)
    if name:
        lines.append(f"name: {name}")
    snippet = _clip(_text(source, node))
    if snippet:
        lines.append(f"text: {snippet}")
    named_parent = node.parent
    while named_parent is not None and not named_parent.is_named:
        named_parent = named_parent.parent
    if named_parent is not None:
        pl, pc = _pos(named_parent)
        parent_name = _node_name(named_parent, source)
        extra = f" {parent_name}" if parent_name else ""
        lines.append(f"parent: {named_parent.type}{extra}  {rel}:{pl}:{pc}")
    enclosing = node
    symbol_types = SYMBOL_TYPES.get(lang, set())
    while enclosing is not None:
        if enclosing.type in symbol_types:
            el, ec = _pos(enclosing.child_by_field_name("name") or enclosing)
            enc_name = _node_name(enclosing, source) or ""
            kind = KIND_BY_TYPE.get(enclosing.type, enclosing.type)
            lines.append(f"enclosing: {kind} {enc_name}  {rel}:{el}:{ec}")
            break
        enclosing = enclosing.parent
    named_children = [child for child in node.children if child.is_named]
    if named_children:
        lines.append("named children:")
        for child in named_children[:20]:
            cl, cc = _pos(child)
            child_name = _node_name(child, source)
            extra = f" {child_name}" if child_name else ""
            lines.append(f"  {child.type}{extra}  {rel}:{cl}:{cc}")
        if len(named_children) > 20:
            lines.append(f"  ... ({len(named_children) - 20} more)")
    return "\n".join(lines)


def query_tree(
    workspace: Path,
    path: str,
    preset: str = "",
    query: str = "",
    language: str = "",
) -> str:
    tree, source, rel, lang = _parse(workspace, path, language)
    if tree is None:
        return lang
    source_query = (query or "").strip()
    preset_key = (preset or "").strip().lower()
    if not source_query:
        if not preset_key:
            return (
                "error: pass preset "
                f"({', '.join(PRESETS)}) or a tree-sitter query"
            )
        if preset_key not in PRESETS:
            return f"error: unknown preset '{preset}'; use one of {', '.join(PRESETS)}"
        source_query = _PRESET_QUERIES.get(lang, {}).get(preset_key)
        if not source_query:
            return f"error: preset '{preset_key}' is not defined for {lang}"
    try:
        captures = _captures(_languages()[lang], tree.root_node, source_query)
    except (ValueError, TypeError, OSError) as exc:
        return f"error: invalid query: {exc}"
    if not captures:
        label = preset_key or "query"
        return f"No captures for {label} in '{rel}'"
    seen: set[tuple[object, object, object]] = set()
    lines: list[str] = []
    for node, capture_name in captures:
        key = (node.start_byte, node.end_byte, capture_name)
        if key in seen:
            continue
        seen.add(key)
        line, col = _pos(node)
        lines.append(
            f"{rel}:{line}:{col}  {node.type}  {_clip(_text(source, node))}"
        )
        if len(lines) >= QUERY_MAX:
            break
    extra = ""
    if len(captures) > len(lines):
        extra = f"\n... ({len(captures) - len(lines)} more; narrow the query or preset)"
    header = preset_key or "query"
    return f"{len(lines)} capture(s) for {header} in {rel}\n" + "\n".join(lines) + extra


def parse_file(workspace: Path, path: str, language: str = "") -> str:
    tree, source, rel, lang = _parse(workspace, path, language)
    if tree is None:
        return lang
    lines: list[str] = []
    truncated = False

    def walk(node, depth: int) -> None:
        nonlocal truncated
        if truncated:
            return
        interesting = node.type in _PARSE_TYPES
        if interesting:
            if len(lines) >= PARSE_MAX_NODES or depth > PARSE_MAX_DEPTH:
                truncated = True
                return
            name = _node_name(node, source)
            start_l, start_c = _pos(node)
            end_l = node.end_point[0] + 1
            label = f"{node.type} {name}" if name else node.type
            lines.append(f"{'  ' * depth}{label}  {rel}:{start_l}:{start_c}-{end_l}")
            next_depth = depth + 1
        else:
            next_depth = depth
        for child in node.children:
            walk(child, next_depth)

    walk(tree.root_node, 0)
    if not lines:
        return f"No named nodes in '{rel}'"
    footer = ""
    if truncated:
        footer = f"\n... (capped at {PARSE_MAX_NODES} named nodes / depth {PARSE_MAX_DEPTH})"
    return f"{len(lines)} named node(s) in {rel}\n" + "\n".join(lines) + footer
