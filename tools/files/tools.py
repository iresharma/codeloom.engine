from __future__ import annotations

import difflib
import json
from pathlib import Path

from protocol.events import FileChanged
from protocol.snapshot import FileTreeNode
from tools.base import BaseTool, ToolContext, ToolResult
from tools.files.tree import build_file_tree
from tools.paths import relative_to_workspace, resolve_workspace_path


def _dump_tree(nodes: list[FileTreeNode]) -> list[dict]:
    return [node.to_json() for node in nodes]


def unified_diff(path: str, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


class ListTree(BaseTool):
    name = "list_tree"
    description = "List the workspace folder tree, honoring .gitignore."
    parameters = {
        "type": "object",
        "properties": {},
    }
    allowed_roles = {"orchestrator", "subagent"}

    async def execute(self, ctx: ToolContext, **kwargs) -> ToolResult:
        del kwargs
        tree = build_file_tree(ctx.workspace)
        return ToolResult(ok=True, content=json.dumps(_dump_tree(tree), indent=2), data={"tree": _dump_tree(tree)})


class ReadFile(BaseTool):
    name = "read_file"
    description = "Read a text file from the workspace."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path relative to the workspace"},
        },
        "required": ["path"],
    }
    allowed_roles = {"orchestrator", "subagent"}

    async def execute(self, ctx: ToolContext, **kwargs) -> ToolResult:
        path = resolve_workspace_path(ctx.workspace, str(kwargs["path"]))
        if not path.is_file():
            return ToolResult(ok=False, content=f"file not found: {kwargs['path']}")
        content = path.read_text(encoding="utf-8", errors="replace")
        rel = relative_to_workspace(ctx.workspace, path)
        return ToolResult(ok=True, content=content, data={"path": rel})


class WriteFile(BaseTool):
    name = "write_file"
    description = "Create or overwrite a text file in the workspace."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    }
    allowed_roles = {"orchestrator", "subagent"}

    async def execute(self, ctx: ToolContext, **kwargs) -> ToolResult:
        path = resolve_workspace_path(ctx.workspace, str(kwargs["path"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        before = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        after = str(kwargs["content"])
        path.write_text(after, encoding="utf-8")
        rel = relative_to_workspace(ctx.workspace, path)
        diff = unified_diff(rel, before, after)
        ctx.emit(FileChanged(path=rel, diff=diff, content=after))
        if ctx.refresh:
            await ctx.refresh()
        return ToolResult(ok=True, content=f"wrote {rel}", data={"path": rel, "diff": diff}, files_touched=[rel])


class EditFile(BaseTool):
    name = "edit_file"
    description = "Replace one exact occurrence of old_string with new_string in a file."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
        },
        "required": ["path", "old_string", "new_string"],
    }
    allowed_roles = {"orchestrator", "subagent"}

    async def execute(self, ctx: ToolContext, **kwargs) -> ToolResult:
        path = resolve_workspace_path(ctx.workspace, str(kwargs["path"]))
        if not path.is_file():
            return ToolResult(ok=False, content=f"file not found: {kwargs['path']}")
        before = path.read_text(encoding="utf-8", errors="replace")
        old = str(kwargs["old_string"])
        new = str(kwargs["new_string"])
        count = before.count(old)
        if count != 1:
            return ToolResult(
                ok=False,
                content=f"old_string must match exactly once, found {count}",
            )
        after = before.replace(old, new, 1)
        path.write_text(after, encoding="utf-8")
        rel = relative_to_workspace(ctx.workspace, path)
        diff = unified_diff(rel, before, after)
        ctx.emit(FileChanged(path=rel, diff=diff, content=after))
        if ctx.refresh:
            await ctx.refresh()
        return ToolResult(ok=True, content=f"edited {rel}", data={"path": rel, "diff": diff}, files_touched=[rel])


class SearchFiles(BaseTool):
    name = "search_files"
    description = "Search file contents (and optionally names) under the workspace."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "glob": {"type": "string", "description": "Optional glob, e.g. *.py"},
            "max_hits": {"type": "integer", "default": 50},
        },
        "required": ["query"],
    }
    allowed_roles = {"orchestrator", "subagent"}

    async def execute(self, ctx: ToolContext, **kwargs) -> ToolResult:
        query = str(kwargs["query"])
        glob = str(kwargs.get("glob") or "*")
        max_hits = int(kwargs.get("max_hits") or 50)
        hits: list[str] = []
        root = ctx.workspace.resolve()
        for path in root.rglob(glob):
            if not path.is_file():
                continue
            if any(part in {".git", "node_modules", ".venv", "__pycache__"} for part in path.parts):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if query not in text and query not in path.name:
                continue
            rel = str(path.relative_to(root))
            for index, line in enumerate(text.splitlines(), start=1):
                if query in line:
                    hits.append(f"{rel}:{index}: {line.strip()}")
                    if len(hits) >= max_hits:
                        return ToolResult(ok=True, content="\n".join(hits), data={"hits": hits})
        return ToolResult(ok=True, content="\n".join(hits) or "no matches", data={"hits": hits})


def file_tools() -> list[BaseTool]:
    return [ListTree(), ReadFile(), WriteFile(), EditFile(), SearchFiles()]
