"""Custom PyInstaller hook for `mcp`.

The stock behaviour we want is the same as `--collect-all mcp`: enumerate
every submodule so nothing dynamically-imported at runtime is silently
missed. But a blanket `collect_submodules('mcp')` (what `--collect-all`
and the CLI's `--collect-submodules` both do under the hood) imports every
submodule during analysis to discover it, including `mcp.cli` /
`mcp.cli.cli`. That module does `import typer` at module scope and calls
`sys.exit(1)` when typer is missing (it's an optional extra behind `pip
install "mcp[cli]"`, not in requirements.txt, and the engine never uses
it at runtime). `sys.exit` raises `SystemExit`, which is not an
`Exception`, so PyInstaller's `collect_submodules` on-error handling
(which only catches `Exception`) does not swallow it — the whole build
aborts.

`--exclude-module mcp.cli` alone does not help here: it only takes effect
inside `Analysis`, after this hiddenimports-collection step has already
run and already crashed.

The fix: use `collect_submodules`'s `filter` parameter (not exposed by
the `--collect-submodules`/`--collect-all` CLI flags) to skip `mcp.cli`
and everything under it before it is ever imported.
"""

from PyInstaller.utils.hooks import collect_submodules


def _skip_cli(name: str) -> bool:
    return not (name == "mcp.cli" or name.startswith("mcp.cli."))


hiddenimports = collect_submodules("mcp", filter=_skip_cli)
