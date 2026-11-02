from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from codeterrain.cache import CACHE_ENV, Cache, git_env
from codeterrain.config import USER_CONFIG_ENV, Config, load_config, resolve_root

SAMPLE_REPO = Path(__file__).parent / "sample_repo"


@pytest.fixture(scope="session", autouse=True)
def _no_ambient_user_config(tmp_path_factory):
    """The developer's own user config and per-user dirs (the digest key) must not leak into the
    suite; session scoped so that the session fixtures below see it too."""
    user = tmp_path_factory.mktemp("user")
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(USER_CONFIG_ENV, str(user / "absent.toml"))
        patch.setenv("LOCALAPPDATA", str(user / "local"))
        patch.setenv("XDG_CACHE_HOME", str(user / "cache"))
        yield


@pytest.fixture(autouse=True)
def _no_ambient_cache_dir(monkeypatch):
    monkeypatch.delenv(CACHE_ENV, raising=False)
    monkeypatch.delenv("CODETERRAIN_PYTHON", raising=False)


@pytest.fixture
def sample_copy(tmp_path) -> Path:
    """A throwaway copy of tests/sample_repo that a test may edit."""
    target = tmp_path / "repo"
    shutil.copytree(SAMPLE_REPO, target, ignore=shutil.ignore_patterns("__pycache__"))
    return Path(resolve_root(target))


@pytest.fixture
def git():
    """Run git in a directory without the variables that would redirect it elsewhere."""

    def run(cwd, *args):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            env=git_env(),
        )

    return run


@pytest.fixture
def cache_dir(tmp_path) -> Path:
    return tmp_path / "cache"


@pytest.fixture
def sample_config(sample_copy) -> Config:
    return load_config(sample_copy)


@pytest.fixture
def sample_cache(sample_copy, cache_dir) -> Cache:
    return Cache(sample_copy, cache_dir)


@pytest.fixture(scope="session")
def warm_sample(tmp_path_factory) -> tuple[Path, Path]:
    """(repo, cache dir) of a sample copy whose slow views were built once for the whole session.

    Read-only for tests: anything that edits files or the cache takes ``sample_copy`` instead.
    """
    from codeterrain.cli import main

    base = tmp_path_factory.mktemp("warm")
    repo = base / "repo"
    shutil.copytree(SAMPLE_REPO, repo, ignore=shutil.ignore_patterns("__pycache__"))
    repo = Path(resolve_root(repo))
    cache = base / "cache"
    assert main(["refresh", "--root", str(repo), "--cache-dir", str(cache)]) == 0
    return repo, cache
