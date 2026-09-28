from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from pathlib import Path

from runtime.tools.shell import DEFAULT_TIMEOUT, format_command_result, run_command

JS_MANAGERS = ("pnpm", "yarn", "npm", "bun")
MUTATE_VERBS = frozenset({"add", "remove"})
VERBS = frozenset({"which", "install", "add", "remove", "run", "typecheck", "test", "build"})
MANAGER_LOCKFILE = {
    "pnpm": "pnpm-lock.yaml",
    "yarn": "yarn.lock",
    "npm": "package-lock.json",
    "bun": "bun.lock",
}
LOCKFILE_MANAGER = {
    "pnpm-lock.yaml": "pnpm",
    "yarn.lock": "yarn",
    "package-lock.json": "npm",
    "bun.lock": "bun",
    "bun.lockb": "bun",
}
EMIT_SUFFIXES = (".js", ".js.map")


@dataclass(frozen=True)
class Toolchain:
    kind: str
    manager: str

    @property
    def lockfile(self) -> str:
        return MANAGER_LOCKFILE.get(self.manager, "")


def detect_toolchain(workspace: Path) -> Toolchain:
    root = Path(workspace)
    package = root / "package.json"
    if package.is_file():
        return Toolchain("js", _js_manager(root, package))
    if (root / "go.mod").is_file():
        return Toolchain("go", "go")
    pyproject = root / "pyproject.toml"
    if (root / "uv.lock").is_file() or _pyproject_has_uv(pyproject):
        return Toolchain("python", "uv")
    if pyproject.is_file() or any(root.glob("requirements*.txt")):
        return Toolchain("python", "pip")
    return Toolchain("", "")


def _js_manager(root: Path, package: Path) -> str:
    data = _read_json(package)
    field = str(data.get("packageManager") or "").strip()
    if field:
        name = field.split("@", 1)[0].strip().lower()
        if name in JS_MANAGERS:
            return name
    for lockfile, manager in LOCKFILE_MANAGER.items():
        if (root / lockfile).is_file():
            return manager
    return "npm"


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _pyproject_has_uv(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return "[tool.uv]" in text or "[tool.uv." in text


def package_scripts(workspace: Path) -> dict[str, str]:
    data = _read_json(Path(workspace) / "package.json")
    scripts = data.get("scripts")
    if not isinstance(scripts, dict):
        return {}
    return {str(key): str(value) for key, value in scripts.items()}


def requirements_file(workspace: Path) -> str:
    root = Path(workspace)
    for name in ("requirements.txt", "requirements-dev.txt"):
        if (root / name).is_file():
            return name
    found = sorted(path.name for path in root.glob("requirements*.txt") if path.is_file())
    return found[0] if found else "requirements.txt"


def format_which(spec: Toolchain, workspace: Path) -> str:
    lines = [f"kind: {spec.kind or '(none)'}", f"manager: {spec.manager or '(none)'}"]
    if spec.lockfile:
        lines.append(f"lockfile: {spec.lockfile}")
    if spec.kind == "js":
        scripts = package_scripts(workspace)
        listed = ", ".join(sorted(scripts)) or "(none)"
        lines.append(f"scripts: {listed}")
        mgr = spec.manager
        lines.extend(
            [
                f"install: {mgr} install",
                f"add: {mgr} add <package>",
                f"remove: {mgr} remove <package>",
                f"run: {mgr} run <script>",
                f"typecheck: {_typecheck_command(spec)}",
                f"test: {mgr} test",
                f"build: {_build_command(spec)}",
            ]
        )
    elif spec.kind == "go":
        lines.extend(
            [
                "install: go mod download",
                "add: go get <package>",
                "remove: go get <package>@none",
                "typecheck: go build ./...",
                "test: go test ./...",
                "build: go build ./...",
            ]
        )
    elif spec.kind == "python":
        if spec.manager == "uv":
            lines.extend(
                [
                    "install: uv sync",
                    "add: uv add <package>",
                    "remove: uv remove <package>",
                    "test: pytest",
                ]
            )
        else:
            req = requirements_file(workspace)
            lines.extend(
                [
                    f"install: pip install -r {req}",
                    "add: pip install <package>",
                    "test: pytest",
                ]
            )
        lines.append("typecheck: (use get_diagnostics)")
    return "\n".join(lines)


def build_command(
    spec: Toolchain,
    verb: str,
    *,
    package: str = "",
    script: str = "",
    workspace: Path | None = None,
) -> str:
    if not spec.kind:
        return "error: no package.json, go.mod, or Python manifest in this workspace"
    if spec.kind == "js":
        return _js_command(spec, verb, package=package, script=script, workspace=workspace)
    if spec.kind == "go":
        return _go_command(verb, package=package)
    return _python_command(spec, verb, package=package, workspace=workspace)


def _typecheck_command(spec: Toolchain) -> str:
    if spec.manager == "npm":
        return "npm exec -- tsc --noEmit"
    if spec.manager == "yarn":
        return "yarn exec tsc --noEmit"
    if spec.manager == "bun":
        return "bunx tsc --noEmit"
    return "pnpm exec tsc --noEmit"


def _build_command(spec: Toolchain) -> str:
    if spec.manager == "npm":
        return "npm run build"
    return f"{spec.manager} run build"


def _js_command(
    spec: Toolchain,
    verb: str,
    *,
    package: str,
    script: str,
    workspace: Path | None,
) -> str:
    mgr = spec.manager
    if verb == "install":
        return f"{mgr} install"
    if verb == "add":
        if not package.strip():
            return "error: toolchain add requires package"
        return f"{mgr} add {shlex.quote(package.strip())}"
    if verb == "remove":
        if not package.strip():
            return "error: toolchain remove requires package"
        return f"{mgr} remove {shlex.quote(package.strip())}"
    if verb == "run":
        name = script.strip()
        if not name:
            return "error: toolchain run requires script"
        if workspace is not None:
            scripts = package_scripts(workspace)
            if name not in scripts:
                available = ", ".join(sorted(scripts)) or "(none)"
                return f"error: no package.json script '{name}'. available: {available}"
        return f"{mgr} run {shlex.quote(name)}"
    if verb == "typecheck":
        return _typecheck_command(spec)
    if verb == "test":
        return f"{mgr} test"
    if verb == "build":
        return _build_command(spec)
    return f"error: unknown toolchain verb '{verb}'"


def _go_command(verb: str, *, package: str) -> str:
    if verb == "install":
        return "go mod download"
    if verb == "add":
        if not package.strip():
            return "error: toolchain add requires package"
        return f"go get {shlex.quote(package.strip())}"
    if verb == "remove":
        if not package.strip():
            return "error: toolchain remove requires package"
        return f"go get {shlex.quote(package.strip())}@none"
    if verb in {"typecheck", "build"}:
        return "go build ./..."
    if verb == "test":
        return "go test ./..."
    if verb == "run":
        return "error: go has no package.json scripts; use toolchain test or build"
    return f"error: unknown toolchain verb '{verb}'"


def _python_command(
    spec: Toolchain, verb: str, *, package: str, workspace: Path | None
) -> str:
    if verb == "typecheck":
        return "error: no toolchain typecheck for Python; use get_diagnostics"
    if verb == "test":
        return "pytest"
    if verb == "build":
        return "error: no default Python build command"
    if verb == "run":
        return "error: Python has no package.json scripts; use toolchain test"
    if spec.manager == "uv":
        if verb == "install":
            return "uv sync"
        if verb == "add":
            if not package.strip():
                return "error: toolchain add requires package"
            return f"uv add {shlex.quote(package.strip())}"
        if verb == "remove":
            if not package.strip():
                return "error: toolchain remove requires package"
            return f"uv remove {shlex.quote(package.strip())}"
    if verb == "install":
        req = requirements_file(workspace or Path("."))
        return f"pip install -r {shlex.quote(req)}"
    if verb == "add":
        if not package.strip():
            return "error: toolchain add requires package"
        return f"pip install {shlex.quote(package.strip())}"
    if verb == "remove":
        return "error: pip has no remove; name the requirements file in the user task"
    return f"error: unknown toolchain verb '{verb}'"


def command_tokens(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def command_binary(tokens: list[str]) -> str:
    for token in tokens:
        if "=" in token and not token.startswith("-") and " " not in token:
            continue
        return Path(token).name.lower()
    return ""


def deny_agent_command(workspace: Path, command: str) -> None:
    """Refuse the wrong manager, emitting tsc, and dependency mutations.

    Called only from the agent-facing run_command tool. The toolchain tool
    runs commands through run_command_impl and is not subject to this.
    """
    tokens = command_tokens(command)
    if not tokens:
        return
    lowered = [token.lower() for token in tokens]
    if "tsc" in lowered and "--noemit" not in lowered:
        raise RuntimeError("tsc must be run with --noEmit; use toolchain typecheck")
    binary = command_binary(tokens)
    if binary in JS_MANAGERS:
        spec = detect_toolchain(workspace)
        if spec.kind == "js" and spec.manager and binary != spec.manager:
            raise RuntimeError(
                f"{binary} is not this repo's package manager; "
                f"use {spec.manager} or the toolchain tool"
            )
    if _is_dep_mutation(tokens):
        raise RuntimeError(
            "install or remove packages with the toolchain tool, not run_command"
        )


def _is_dep_mutation(tokens: list[str]) -> bool:
    if not tokens:
        return False
    names = [Path(token).name.lower() for token in tokens]
    if names[0] == "go":
        if len(names) >= 2 and names[1] == "get":
            return True
        if len(names) >= 3 and names[1] == "mod" and names[2] == "tidy":
            return True
        return False
    if "pip" in names and "install" in names:
        return True
    if names[0] == "uv":
        if len(names) >= 2 and names[1] in {"add", "remove"}:
            return True
        if len(names) >= 3 and names[1] == "pip" and names[2] == "install":
            return True
    return False


def skip_commit_path(
    workspace: Path, rel: str, *, untracked: bool, manager: str = ""
) -> bool:
    name = Path(rel).name
    if name.endswith(".tsbuildinfo"):
        return True
    if manager and name in LOCKFILE_MANAGER and LOCKFILE_MANAGER[name] != manager:
        return True
    if untracked and _is_ts_emit(workspace, rel):
        return True
    return False


def _is_ts_emit(workspace: Path, rel: str) -> bool:
    name = Path(rel).name
    stem = ""
    if name.endswith(".js.map"):
        stem = name[: -len(".js.map")]
    elif name.endswith(".js"):
        stem = name[:-3]
    else:
        return False
    parent = Path(workspace) / Path(rel).parent
    return (parent / f"{stem}.ts").is_file() or (parent / f"{stem}.tsx").is_file()


async def run_toolchain(
    ctx,
    verb: str,
    *,
    package: str = "",
    script: str = "",
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    name = (verb or "").strip().lower()
    if name not in VERBS:
        return f"error: unknown toolchain verb '{verb}'"
    if name in MUTATE_VERBS and getattr(ctx, "profile", "") != "coder":
        return f"error: toolchain {name} is only available to coder"
    spec = detect_toolchain(ctx.workspace)
    if name == "which":
        return format_which(spec, ctx.workspace)
    command = build_command(
        spec, name, package=package, script=script, workspace=ctx.workspace
    )
    if command.startswith("error:"):
        return command

    def on_output(stream: str, text: str) -> None:
        if getattr(ctx, "on_output", None) is not None:
            ctx.on_output("", stream, text)

    result = await run_command(
        ctx.workspace,
        command,
        timeout=timeout,
        approval="never",
        on_output=on_output,
        on_proc=getattr(ctx, "on_proc", None),
    )
    return format_command_result(result)
