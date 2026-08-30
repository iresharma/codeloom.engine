from tools.base import ToolContext, tool
from runtime.fs import list_tree
from protocol.snapshot import FileTreeNode


@tool(description="List files in the workspace as relative paths, one per line.")
def list_files(ctx: ToolContext) -> str:
    paths: list[str] = []
    _collect(list_tree(ctx.workspace), paths)
    if not paths:
        return "(empty)"
    return "\n".join(paths)


def _collect(nodes: list[FileTreeNode], paths: list[str]) -> None:
    for node in nodes:
        if node.is_dir:
            _collect(node.children or [], paths)
        else:
            paths.append(node.path)
