from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import codeterrain
from codeterrain import executables
from codeterrain.cache import find_cache_dir, user_cache_dir
from codeterrain.config import resolve_root
from codeterrain.executables import distrust, resolve_executable

WINDOWS = os.name == "nt"

#: Subprocess call sites in src/ that need no resolution, with the reason. Every other call site
#: must sit in a function that calls ``resolve_executable``.
NOT_RESOLVED: dict[tuple[str, str], str] = {
    ("sandbox.py", "_kill_tree"): "taskkill.exe by its absolute path under %SystemRoot%",
}

SPAWNERS = {
    ("subprocess", "run"),
    ("subprocess", "Popen"),
    ("subprocess", "call"),
    ("subprocess", "check_call"),
    ("subprocess", "check_output"),
    ("os", "system"),
    ("os", "popen"),
}


def _plant(directory: Path, name: str) -> Path:
    """A program called ``name`` in ``directory`` that is not the real one."""
    directory.mkdir(parents=True, exist_ok=True)
    if WINDOWS:
        target = directory / f"{name}.exe"
        shutil.copy(Path(os.environ["SYSTEMROOT"]) / "System32" / "whoami.exe", target)
    else:
        target = directory / name
        target.write_text("#!/bin/sh\necho planted\nexit 1\n", encoding="utf-8")
        target.chmod(0o755)
    return target


def test_a_bare_name_skips_empty_dot_and_relative_path_entries(tmp_path, monkeypatch):
    real = _plant(tmp_path / "bin", "ct-tool")
    _plant(tmp_path / "cwd", "ct-tool")
    _plant(tmp_path / "cwd" / "rel", "ct-tool")
    monkeypatch.chdir(tmp_path / "cwd")
    monkeypatch.setenv("PATH", os.pathsep.join(["", ".", "rel", str(tmp_path / "bin")]))
    assert Path(resolve_executable("ct-tool")) == real


def test_a_name_with_a_separator_is_the_callers_choice_made_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert resolve_executable("venv/bin/python") == os.path.abspath("venv/bin/python")


def test_an_unknown_name_is_file_not_found(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(FileNotFoundError, match="ct-nothing"):
        resolve_executable("ct-nothing")


@pytest.mark.skipif(not WINDOWS, reason="batch files are Windows only")
def test_a_batch_file_on_path_is_never_resolved_and_an_explicit_one_is_refused(
    tmp_path, monkeypatch
):
    # Security review r2 P3-1: a python.bat shim first on PATH won, and cmd.exe truncated the
    # bootstrap program at its first newline.
    shim = tmp_path / "shim"
    shim.mkdir()
    for name in ("ct-tool.cmd", "ct-tool.bat"):
        (shim / name).write_text("@echo off\n", encoding="utf-8")
    real = _plant(tmp_path / "bin", "ct-tool")
    monkeypatch.setenv("PATH", os.pathsep.join([str(shim), str(tmp_path / "bin")]))
    monkeypatch.setenv("PATHEXT", ".BAT;.CMD;.EXE")
    assert Path(resolve_executable("ct-tool")) == real
    for name in ("ct-tool.cmd", str(shim / "ct-tool.bat")):
        with pytest.raises(FileNotFoundError, match="cmd.exe"):
            resolve_executable(name)


@pytest.mark.skipif(not WINDOWS, reason="batch files are Windows only")
def test_a_git_cmd_first_on_path_never_runs_the_checkout_name_as_commands(
    tmp_path, git, monkeypatch
):
    # Security review r2 P3-1: with a git.cmd wrapper first on PATH, cmd.exe read `&` in the
    # checkout's directory name as a command separator and ran the rest in the parent.
    real_git = resolve_executable("git")
    wrappers = tmp_path / "wrappers"
    wrappers.mkdir()
    (wrappers / "git.cmd").write_text(f'@"{real_git}" %*\n', encoding="utf-8")
    base = tmp_path / "base"
    root = resolve_root(base / "proj&md,PWNED.txt&rem")
    root.mkdir(parents=True)
    git(root, "init", "-q")
    monkeypatch.chdir(base)
    monkeypatch.setenv("PATH", os.pathsep.join([str(wrappers), os.environ["PATH"]]))
    assert find_cache_dir(root) == Path(os.path.realpath(root / ".git")) / "codeterrain"
    assert not list(tmp_path.rglob("PWNED*"))


@pytest.mark.skipif(not WINDOWS, reason="drive-relative paths are Windows only")
def test_a_driveless_path_entry_is_skipped(tmp_path, monkeypatch):
    # Security review r2 P3-11: a rooted entry such as \tools resolves against the current drive,
    # the checkout's, and isabs() accepts it on Python 3.12.
    real = _plant(tmp_path / "bin", "ct-tool")
    _plant(tmp_path / "rooted", "ct-tool")
    monkeypatch.chdir(tmp_path)
    rooted = str(tmp_path / "rooted")[2:]  # the same directory without its drive
    monkeypatch.setenv("PATH", os.pathsep.join([rooted, str(tmp_path / "bin")]))
    assert Path(resolve_executable("ct-tool")) == real


def test_a_git_planted_in_the_repository_root_is_never_run(tmp_path, git, monkeypatch):
    # Security review P2-1: with the variable below unset (the Windows default), CreateProcess ran
    # <cwd>/git.exe, and the documented cwd is the target repository.
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    root = resolve_root(tmp_path / "repo")
    root.mkdir()
    git(root, "init", "-q")
    _plant(root, "git")
    monkeypatch.chdir(root)
    monkeypatch.setenv("PATH", os.pathsep.join([".", os.environ["PATH"]]))
    assert find_cache_dir(root) == Path(os.path.realpath(root / ".git")) / "codeterrain"


def test_a_program_found_through_an_absolute_path_entry_inside_the_checkout_is_refused(
    tmp_path, git, monkeypatch
):
    # With <checkout>/bin first on PATH as an absolute entry, a
    # planted git ran outside the sandbox with the parent's environment.
    root = resolve_root(tmp_path / "repo")
    root.mkdir()
    git(root, "init", "-q")
    planted = _plant(root / "bin", "git")
    monkeypatch.setenv("PATH", os.pathsep.join([str(root / "bin"), os.environ["PATH"]]))
    with pytest.raises(FileNotFoundError, match="inside a target repository"):
        resolve_executable("git", untrusted=root)
    assert Path(resolve_executable("git")) == planted  # the check is the caller's to ask for
    assert find_cache_dir(root) == user_cache_dir(root)  # git refused: the per-user fallback


def test_a_program_in_any_distrusted_checkout_is_refused_whichever_checkout_is_asked_about(
    tmp_path, monkeypatch
):
    # `terrain callers` reads the backend and every front end;
    # git planted in one checkout ran while git status was asked about another.
    backend, web = tmp_path / "backend", tmp_path / "web"
    web.mkdir()
    _plant(backend / "bin", "git")
    monkeypatch.setenv("PATH", os.pathsep.join([str(backend / "bin"), os.environ["PATH"]]))
    distrust([backend, web])
    try:
        with pytest.raises(FileNotFoundError, match="inside a target repository"):
            resolve_executable("git", untrusted=web)
        with pytest.raises(FileNotFoundError, match="inside a target repository"):
            resolve_executable("git")
    finally:
        distrust([])
    assert Path(resolve_executable("git", untrusted=web)).parent == backend / "bin"


def test_a_bare_python_planted_in_the_cwd_is_never_the_interpreter(tmp_path, monkeypatch):
    from codeterrain.config import Config
    from codeterrain.sandbox import run_python

    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    name = Path(sys.executable).stem
    _plant(tmp_path / "repo", name)
    monkeypatch.chdir(tmp_path / "repo")
    real_dir = str(Path(sys.executable).parent)
    monkeypatch.setenv("PATH", os.pathsep.join([".", real_dir]))
    code = "import os, sys; open(os.environ['CODETERRAIN_RESULT'], 'w').write('1')"
    result = run_python(name, ["-c", code], Config(root=tmp_path / "repo"), tmp_path / "cache")
    assert result.returncode == 0 and result.result == 1


def _spawn_calls(tree: ast.Module):
    """(function name, call) for every spawning call, by the function that encloses it."""
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and (
                    (node.func.value.id, node.func.attr) in SPAWNERS
                    or (node.func.value.id == "os" and node.func.attr.startswith(("exec", "spawn")))
                )
            ):
                yield fn, node


def test_every_subprocess_in_src_resolves_its_program_or_is_listed():
    src = Path(codeterrain.__file__).parent
    seen: set[tuple[str, str]] = set()
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn, _call in _spawn_calls(tree):
            site = (path.relative_to(src).as_posix(), fn.name)
            seen.add(site)
            resolves = any(
                isinstance(n, ast.Name) and n.id == "resolve_executable" for n in ast.walk(fn)
            )
            assert resolves or site in NOT_RESOLVED, f"{site} spawns without resolve_executable"
    assert seen, "the walk found no subprocess call at all"
    assert set(NOT_RESOLVED) <= seen, "NOT_RESOLVED lists a call site that no longer exists"


def test_the_real_git_still_answers_through_the_helper(tmp_path, git):
    root = resolve_root(tmp_path / "repo")
    root.mkdir()
    git(root, "init", "-q")
    out = subprocess.run(
        [resolve_executable("git"), "-C", str(root), "rev-parse", "--git-dir"],
        capture_output=True, text=True, check=True,
    )
    assert out.stdout.strip() == ".git"


def test_only_the_environment_terrain_runs_from_is_trusted_inside_a_checkout(
    tmp_path, monkeypatch
):
    checkout = tmp_path / "checkout"
    venv = checkout / ".venv"
    (venv / "lib").mkdir(parents=True)
    monkeypatch.setattr(executables.sys, "prefix", str(venv))
    executables.distrust([checkout])
    try:
        assert executables.distrusted(checkout / "vendor" / "pack")
        assert not executables.distrusted(venv / "lib" / "pack")
        assert not executables.distrusted(tmp_path / "elsewhere")
    finally:
        executables.distrust([])


def test_a_checkout_inside_the_environment_is_not_trusted(tmp_path, monkeypatch):
    env = tmp_path / "env"
    checkout = env / "work" / "checkout"
    checkout.mkdir(parents=True)
    monkeypatch.setattr(executables.sys, "prefix", str(env))
    executables.distrust([checkout])
    try:
        assert executables.distrusted(checkout / "vendor" / "pack")
    finally:
        executables.distrust([])
