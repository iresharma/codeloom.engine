from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from runtime.tools.edits import apply_edit
from runtime.tools.fileid import guard_write_path, named_in_request
from runtime.tools.fs import WorkspacePathError
from runtime.tools.git import commit_if_dirty, pr_title_from_summary
from tests.test_orchestrator import _init_git
from runtime.tools.toolchain import (
    build_command,
    deny_agent_command,
    detect_toolchain,
    run_toolchain,
    skip_commit_path,
)
from tests.conftest import seed
from tools.registry import discover_tools


def _pnpm_repo(root: Path) -> None:
    (root / "package.json").write_text(
        '{"name":"app","packageManager":"pnpm@10.33.3","scripts":{"test":"echo ok"}}\n',
        encoding="utf-8",
    )
    (root / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")


def test_detects_pnpm_from_package_manager_and_lockfile(tmp_path):
    _pnpm_repo(tmp_path)
    spec = detect_toolchain(tmp_path)
    assert spec.kind == "js"
    assert spec.manager == "pnpm"
    assert spec.lockfile == "pnpm-lock.yaml"


def test_detects_go_and_uv(tmp_path):
    (tmp_path / "go.mod").write_text("module example.com/app\n", encoding="utf-8")
    assert detect_toolchain(tmp_path).manager == "go"
    py = tmp_path / "py"
    py.mkdir()
    (py / "pyproject.toml").write_text("[tool.uv]\n", encoding="utf-8")
    assert detect_toolchain(py).manager == "uv"
    pip = tmp_path / "pip"
    pip.mkdir()
    (pip / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    assert detect_toolchain(pip).manager == "pip"


def test_typecheck_is_never_bare_tsc(tmp_path):
    _pnpm_repo(tmp_path)
    spec = detect_toolchain(tmp_path)
    command = build_command(spec, "typecheck", workspace=tmp_path)
    assert "tsc" in command
    assert "--noEmit" in command
    assert not command.startswith("tsc")
    npm = tmp_path / "npm"
    npm.mkdir()
    (npm / "package.json").write_text('{"name":"app"}\n', encoding="utf-8")
    (npm / "package-lock.json").write_text("{}\n", encoding="utf-8")
    npm_cmd = build_command(detect_toolchain(npm), "typecheck", workspace=npm)
    assert npm_cmd == "npm exec -- tsc --noEmit"


def test_run_requires_existing_script(tmp_path):
    _pnpm_repo(tmp_path)
    spec = detect_toolchain(tmp_path)
    err = build_command(spec, "run", script="missing", workspace=tmp_path)
    assert err.startswith("error:")
    assert "test" in err
    ok = build_command(spec, "run", script="test", workspace=tmp_path)
    assert ok == "pnpm run test"


def test_pnpm_repo_refuses_npm_and_bare_tsc(tmp_path):
    _pnpm_repo(tmp_path)
    with pytest.raises(RuntimeError, match="package manager"):
        deny_agent_command(tmp_path, "npm install")
    with pytest.raises(RuntimeError, match="--noEmit"):
        deny_agent_command(tmp_path, "tsc")
    with pytest.raises(RuntimeError, match="--noEmit"):
        deny_agent_command(tmp_path, "pnpm exec tsc")
    deny_agent_command(tmp_path, "pnpm exec tsc --noEmit")
    deny_agent_command(tmp_path, "pnpm test")


def test_run_command_refuses_dep_mutations(tmp_path):
    with pytest.raises(RuntimeError, match="toolchain"):
        deny_agent_command(tmp_path, "go get example.com/pkg")
    with pytest.raises(RuntimeError, match="toolchain"):
        deny_agent_command(tmp_path, "go mod tidy")
    with pytest.raises(RuntimeError, match="toolchain"):
        deny_agent_command(tmp_path, "pip install requests")
    with pytest.raises(RuntimeError, match="toolchain"):
        deny_agent_command(tmp_path, "uv add ruff")
    deny_agent_command(tmp_path, "go test ./...")
    deny_agent_command(tmp_path, "pytest")


def test_toolchain_add_is_coder_only(ctx):
    _pnpm_repo(ctx.workspace)
    ctx.profile = "tester"

    async def run():
        return await run_toolchain(ctx, "add", package="lucide-react")

    result = asyncio.run(run())
    assert result.startswith("error:")
    assert "coder" in result
    which = asyncio.run(run_toolchain(ctx, "which"))
    assert "manager: pnpm" in which


def test_guard_refuses_toolchain_files_unless_named(ctx):
    seed(ctx, "package.json", '{"name":"app"}\n')
    seed(ctx, "tsconfig.json", "{}\n")
    seed(ctx, "src/app.tsx", "export const x = 1\n")
    with pytest.raises(WorkspacePathError, match="toolchain"):
        guard_write_path(ctx.workspace, "package.json")
    with pytest.raises(WorkspacePathError, match="toolchain"):
        guard_write_path(ctx.workspace, "tsconfig.json")
    guard_write_path(
        ctx.workspace,
        "tsconfig.json",
        origin_request="Please edit tsconfig.json for path aliases",
    )
    guard_write_path(ctx.workspace, "src/app.tsx")
    with pytest.raises(WorkspacePathError, match="lockfile"):
        guard_write_path(ctx.workspace, "pnpm-lock.yaml")


def test_named_in_request_is_filename_not_word():
    assert named_in_request("package.json", "update the package") is False
    assert named_in_request("package.json", "edit package.json please") is True


def test_apply_edit_rejects_duplicate_const(ctx):
    seed(ctx, "home.tsx", "const detail = { a: 1 }\n")

    async def run():
        return await apply_edit(
            ctx,
            "home.tsx",
            lambda src: src.text + "const detail = { b: 2 }\n",
            "str_replace",
        )

    result = asyncio.run(run())
    assert result.startswith("error:")
    assert "detail" in result


def test_apply_edit_refuses_tsconfig_without_origin(ctx):
    seed(ctx, "tsconfig.json", '{ "compilerOptions": {} }\n')
    ctx.origin_request = "add a workspace mock to the clients page"

    async def run():
        return await apply_edit(
            ctx, "tsconfig.json", lambda src: '{ "compilerOptions": { "strict": true } }\n', "str_replace"
        )

    result = asyncio.run(run())
    assert result.startswith("error:")
    assert "toolchain" in result


def test_skip_emit_and_wrong_lockfile(tmp_path):
    _pnpm_repo(tmp_path)
    ts = tmp_path / "packages" / "ui" / "src" / "landings"
    ts.mkdir(parents=True)
    (ts / "home.tsx").write_text("export const Home = () => null\n", encoding="utf-8")
    assert skip_commit_path(
        tmp_path, "packages/ui/src/landings/home.js", untracked=True, manager="pnpm"
    )
    assert skip_commit_path(tmp_path, "package-lock.json", untracked=True, manager="pnpm")
    assert not skip_commit_path(tmp_path, "pnpm-lock.yaml", untracked=True, manager="pnpm")
    assert not skip_commit_path(
        tmp_path, "packages/ui/src/landings/home.tsx", untracked=False, manager="pnpm"
    )


def test_commit_if_dirty_skips_emit_and_npm_lock(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git(repo)
    _pnpm_repo(repo)
    landings = repo / "packages" / "ui" / "src" / "landings"
    landings.mkdir(parents=True)
    (landings / "home.tsx").write_text("export const Home = () => null\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
    (landings / "home.js").write_text("exports.Home = () => null\n", encoding="utf-8")
    (repo / "package-lock.json").write_text("{}\n", encoding="utf-8")
    (landings / "home.tsx").write_text(
        "export const Home = () => null\nexport const Extra = 1\n",
        encoding="utf-8",
    )
    err = commit_if_dirty(repo, "engine(coder): add extra")
    assert err == ""
    names = subprocess.run(
        ["git", "show", "--name-only", "--pretty=", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert "packages/ui/src/landings/home.tsx" in names
    assert "packages/ui/src/landings/home.js" not in names
    assert "package-lock.json" not in names


def test_commit_title_uses_what_not_paths():
    summary = "paths: None changed\nwhat: add a web workspace mock\nverdict: ok\n"
    assert pr_title_from_summary(summary) == "add a web workspace mock"


def test_profiles_expose_toolchain():
    from agents.profile import discover_profiles

    tools = discover_tools()
    profiles = discover_profiles()
    for name in ("coder", "tester", "debugger"):
        names = tools.subset(profiles.get(name).tool_names).names()
        assert "toolchain" in names
    reviewer = tools.subset(profiles.get("reviewer").tool_names).names()
    assert "toolchain" not in reviewer
    assert "run_command" not in reviewer
