from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

EXT_LANGUAGE = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".rb": "ruby",
}

SYMBOL_PATTERNS = {
    "python": re.compile(r"^(?P<indent>\s*)(?P<kind>async def|def|class)\s+(?P<name>\w+)"),
    "javascript": re.compile(
        r"^(?P<indent>\s*)(?P<kind>export\s+)?((async\s+)?function|class|const|let)\s+(?P<name>\w+)"
    ),
    "typescript": re.compile(
        r"^(?P<indent>\s*)(?P<kind>export\s+)?((async\s+)?function|class|const|let|type|interface)\s+(?P<name>\w+)"
    ),
    "go": re.compile(r"^(?P<indent>\s*)(?P<kind>func|type)\s+(?:\([^)]+\)\s+)?(?P<name>\w+)"),
    "rust": re.compile(r"^(?P<indent>\s*)(?P<kind>fn|struct|enum|impl|mod)\s+(?P<name>\w+)"),
}


@dataclass
class Capture:
    name: str
    text: str
    start_line: int
    start_col: int
    end_line: int
    end_col: int


def infer_language(path: Path) -> str | None:
    return EXT_LANGUAGE.get(path.suffix.lower())


def _try_tree_sitter(source: bytes, language: str):
    try:
        from tree_sitter import Language, Parser
    except ImportError:
        return None
    lang_mod = None
    module_names = {
        "python": "tree_sitter_python",
        "javascript": "tree_sitter_javascript",
        "typescript": "tree_sitter_typescript",
        "go": "tree_sitter_go",
        "rust": "tree_sitter_rust",
    }
    try:
        lang_mod = __import__(module_names.get(language, ""), fromlist=["language"])
    except Exception:
        return None
    parser = Parser()
    try:
        parser.language = Language(lang_mod.language())
    except Exception:
        try:
            parser.set_language(Language(lang_mod.language()))
        except Exception:
            return None
    return parser.parse(source)


def parse_source(path: Path, source: str, language: str | None = None) -> dict:
    lang = language or infer_language(path)
    tree = _try_tree_sitter(source.encode("utf-8"), lang) if lang else None
    if tree is not None:
        return {
            "language": lang,
            "backend": "tree-sitter",
            "sexp": tree.root_node.sexp(),
        }
    return {
        "language": lang,
        "backend": "fallback",
        "sexp": None,
        "note": "tree-sitter not available; using regex fallback for symbols",
    }


def query_source(path: Path, source: str, query: str, language: str | None = None) -> list[Capture]:
    lang = language or infer_language(path)
    tree = _try_tree_sitter(source.encode("utf-8"), lang) if lang else None
    if tree is None:
        return []
    try:
        from tree_sitter import Query
    except ImportError:
        return []
    compiled = Query(tree.language, query)
    captures = []
    for node, name in compiled.captures(tree.root_node):
        captures.append(
            Capture(
                name=name,
                text=source[node.start_byte : node.end_byte],
                start_line=node.start_point[0] + 1,
                start_col=node.start_point[1],
                end_line=node.end_point[0] + 1,
                end_col=node.end_point[1],
            )
        )
    return captures


def node_at(source: str, line: int, col: int, path: Path, language: str | None = None) -> dict | None:
    lang = language or infer_language(path)
    tree = _try_tree_sitter(source.encode("utf-8"), lang) if lang else None
    if tree is None:
        return None
    point = (max(line - 1, 0), max(col, 0))
    node = tree.root_node.descendant_for_point_range(point, point)
    if node is None:
        return None
    parent = node.parent
    children = [
        {
            "type": child.type,
            "named": child.is_named,
            "start_line": child.start_point[0] + 1,
            "start_col": child.start_point[1],
        }
        for child in node.named_children
    ]
    return {
        "type": node.type,
        "text": source[node.start_byte : node.end_byte][:400],
        "start_line": node.start_point[0] + 1,
        "start_col": node.start_point[1],
        "parent": parent.type if parent else None,
        "named_children": children,
    }


def list_symbols(path: Path, source: str, language: str | None = None) -> list[dict]:
    lang = language or infer_language(path)
    if lang == "tsx":
        lang = "typescript"
    pattern = SYMBOL_PATTERNS.get(lang or "")
    if not pattern:
        return []
    symbols = []
    for index, line in enumerate(source.splitlines(), start=1):
        match = pattern.match(line)
        if not match:
            continue
        symbols.append(
            {
                "name": match.group("name"),
                "kind": match.group("kind").strip(),
                "line": index,
                "path": str(path),
            }
        )
    return symbols
