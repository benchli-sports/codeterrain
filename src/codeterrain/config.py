"""Configuration: ``--config``, then ``[tool.codeterrain]`` in pyproject.toml, then defaults.

The target repository's pyproject is not the user's opt-in: the keys that widen what the sandboxed
child gets (``USER_ONLY_KEYS``) are honoured only from ``--config`` or the per-user config file, and
are dropped (and reported in ``Config.ignored``) when the repository sets them.
"""

from __future__ import annotations

import dataclasses
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_MANIFEST_KEYS = {
    "rows": "routes",
    "method": "method",
    "path": "path",
    "access": "access",
    "apps": "apps",
}
_KNOWN_KEYS = {
    "apps",
    "import_env",
    "test_paths",
    "region",
    "route_manifest",
    "manifest_keys",
    "plugins",
    "moto_mock",
    "pytest_args",
    "pass_env",
    "respect_addopts",
    "client_paths",
    "path_prefix",
    "client_repos",
}
#: Keys that hand the child more of the user's environment, let it write files, or make
#: codeterrain read other checkouts.
USER_ONLY_KEYS = ("pass_env", "pytest_args", "respect_addopts", "client_repos")
USER_CONFIG_ENV = "CODETERRAIN_USER_CONFIG"
#: The repository's own config file, for repositories without a ``pyproject.toml`` (front ends).
REPO_CONFIG = "codeterrain.toml"


class ConfigError(ValueError):
    """The configuration is malformed; the message names the offending key."""


@dataclass(frozen=True)
class ClientRepo:
    """A front-end checkout for the routes join. ``path_prefix`` and ``client_paths`` set here
    win over the checkout's own config, so a read-only checkout needs no file of its own."""

    root: Path
    path_prefix: str | None = None
    client_paths: tuple[str, ...] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.root.as_posix(),
            "path_prefix": self.path_prefix,
            "client_paths": None if self.client_paths is None else list(self.client_paths),
        }


@dataclass(frozen=True)
class Config:
    root: Path
    source: str = "defaults"
    apps: tuple[str, ...] = ()
    import_env: dict[str, str] = field(default_factory=dict)
    test_paths: tuple[str, ...] = ("tests",)
    region: str = "us-east-1"
    route_manifest: Path | None = None
    manifest_keys: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_MANIFEST_KEYS))
    plugins: tuple[str, ...] = ()
    moto_mock: bool = False
    pytest_args: tuple[str, ...] = ()
    pass_env: tuple[str, ...] = ()
    respect_addopts: bool = False
    #: Where ``terrain calls`` looks for JavaScript and TypeScript; empty means the whole checkout.
    client_paths: tuple[str, ...] = ()
    #: Put in front of every frontend call path before it is matched to a backend route.
    path_prefix: str = ""
    #: Front-end checkouts whose calls ``terrain callers`` joins to this repository's routes.
    client_repos: tuple[ClientRepo, ...] = ()
    #: User-only keys the repository's pyproject set and that were therefore dropped.
    ignored: tuple[str, ...] = ()
    #: The per-user config file that supplied values, if any.
    user_config: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "root": self.root.as_posix(),
            "source": self.source,
            "apps": list(self.apps),
            "import_env": dict(self.import_env),
            "test_paths": list(self.test_paths),
            "region": self.region,
            "route_manifest": self.route_manifest.as_posix() if self.route_manifest else None,
            "manifest_keys": dict(self.manifest_keys),
            "plugins": list(self.plugins),
            "moto_mock": self.moto_mock,
            "pytest_args": list(self.pytest_args),
            "pass_env": list(self.pass_env),
            "respect_addopts": self.respect_addopts,
            "client_paths": list(self.client_paths),
            "path_prefix": self.path_prefix,
            "client_repos": [c.as_dict() for c in self.client_repos],
        }


def resolve_root(root: str | os.PathLike[str] | None) -> Path:
    """The target repository root, symlinks and junctions resolved."""
    return Path(os.path.realpath(root if root else os.getcwd()))


def _table_from_toml(path: Path, *, nested_only: bool) -> dict[str, Any] | None:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path.as_posix()}: invalid TOML: {exc}") from exc
    tool = data.get("tool", {})
    if not isinstance(tool, dict):
        raise ConfigError(f"{path.as_posix()}: [tool] must be a table")
    nested = tool.get("codeterrain")
    if nested is not None:
        if not isinstance(nested, dict):
            raise ConfigError(f"{path.as_posix()}: [tool.codeterrain] must be a table")
        return nested
    return None if nested_only else data


def _str_list(table: dict[str, Any], key: str) -> tuple[str, ...]:
    value = table[key]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{key}: expected a list of strings")
    return tuple(value)


def _under_root(root: Path, key: str, value: str) -> Path:
    """``root / value``, symlinks and junctions resolved; refused unless it stays in ``root``."""
    if Path(value).is_absolute() or Path(value).drive:
        raise ConfigError(f"{key}: {value!r} must be relative to the repository root")
    resolved = Path(os.path.realpath(root / value))
    try:
        resolved.relative_to(os.path.realpath(root))
    except ValueError:
        raise ConfigError(f"{key}: {value!r} resolves outside the repository root") from None
    return resolved


def _path_prefix(value: Any, key: str) -> str:
    if not isinstance(value, str) or (value and not value.startswith("/")):
        raise ConfigError(f"{key}: expected a path starting with '/' or ''")
    return value.rstrip("/")


def _client_repos(root: Path, value: Any) -> tuple[ClientRepo, ...]:
    """``["../web", {path = "../admin", path_prefix = "/v1", client_paths = ["src"]}]``. Relative
    paths are read from the repository root; the checkouts are outside it by design, which is why
    the key is user only."""
    if not isinstance(value, list):
        raise ConfigError("client_repos: expected a list of paths or tables")
    repos: list[ClientRepo] = []
    for entry in value:
        if isinstance(entry, str):
            entry = {"path": entry}
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise ConfigError("client_repos: each entry is a path or a table with a path")
        extra = sorted(set(entry) - {"path", "path_prefix", "client_paths"})
        if extra:
            raise ConfigError(f"client_repos: unknown key(s): {', '.join(extra)}")
        repos.append(
            ClientRepo(
                Path(os.path.realpath(root / entry["path"])),
                _path_prefix(entry["path_prefix"], "client_repos.path_prefix")
                if "path_prefix" in entry
                else None,
                _str_list(entry, "client_paths") if "client_paths" in entry else None,
            )
        )
    return tuple(repos)


def client_config(repo: ClientRepo) -> Config:
    """The front end's own config with the overrides from its ``client_repos`` entry; an
    overriding ``client_paths`` is confined to that checkout like its own would be."""
    config = load_config(repo.root)
    changes: dict[str, Any] = {}
    if repo.path_prefix is not None:
        changes["path_prefix"] = repo.path_prefix
    if repo.client_paths is not None:
        for entry in repo.client_paths:
            _under_root(repo.root, "client_repos.client_paths", entry)
        changes["client_paths"] = repo.client_paths
    return dataclasses.replace(config, **changes)


def _build(root: Path, table: dict[str, Any], source: str) -> Config:
    unknown = sorted(set(table) - _KNOWN_KEYS)
    if unknown:
        raise ConfigError(f"unknown config key(s): {', '.join(unknown)}")
    values: dict[str, Any] = {"root": root, "source": source}
    if "apps" in table:
        apps = _str_list(table, "apps")
        bad = [a for a in apps if a.count(":") != 1 or not all(a.split(":"))]
        if bad:
            raise ConfigError(f"apps: expected 'module:attr', got {bad[0]!r}")
        values["apps"] = apps
    if "import_env" in table:
        env = table["import_env"]
        if not isinstance(env, dict):
            raise ConfigError("import_env: expected a table")
        values["import_env"] = {str(k): str(v) for k, v in env.items()}
    if "test_paths" in table:
        values["test_paths"] = _str_list(table, "test_paths")
    if "region" in table:
        if not isinstance(table["region"], str) or not table["region"]:
            raise ConfigError("region: expected a non-empty string")
        values["region"] = table["region"]
    if "route_manifest" in table:
        if not isinstance(table["route_manifest"], str):
            raise ConfigError("route_manifest: expected a path string")
        values["route_manifest"] = _under_root(root, "route_manifest", table["route_manifest"])
    if "manifest_keys" in table:
        keys = table["manifest_keys"]
        if not isinstance(keys, dict) or not all(isinstance(v, str) for v in keys.values()):
            raise ConfigError("manifest_keys: expected a table of strings")
        extra = sorted(set(keys) - set(DEFAULT_MANIFEST_KEYS))
        if extra:
            raise ConfigError(f"manifest_keys: unknown key(s): {', '.join(extra)}")
        values["manifest_keys"] = {**DEFAULT_MANIFEST_KEYS, **keys}
    if "plugins" in table:
        values["plugins"] = _str_list(table, "plugins")
    if "pytest_args" in table:
        values["pytest_args"] = _str_list(table, "pytest_args")
    if "moto_mock" in table:
        if not isinstance(table["moto_mock"], bool):
            raise ConfigError("moto_mock: expected true or false")
        values["moto_mock"] = table["moto_mock"]
    if "pass_env" in table:
        values["pass_env"] = _str_list(table, "pass_env")
    if "respect_addopts" in table:
        if not isinstance(table["respect_addopts"], bool):
            raise ConfigError("respect_addopts: expected true or false")
        values["respect_addopts"] = table["respect_addopts"]
    if "client_paths" in table:
        values["client_paths"] = _str_list(table, "client_paths")
    if "path_prefix" in table:
        values["path_prefix"] = _path_prefix(table["path_prefix"], "path_prefix")
    if "client_repos" in table:
        values["client_repos"] = _client_repos(root, table["client_repos"])
    config = Config(**values)
    # The default ``tests`` too: a junction or a cloned symlink there must not lead out.
    for entry in config.test_paths:
        _under_root(root, "test_paths", entry)
    for entry in config.client_paths:
        _under_root(root, "client_paths", entry)
    return config


def user_config_path() -> Path:
    """``$CODETERRAIN_USER_CONFIG``, else ``config.toml`` in the platform's per-user config dir:
    ``%APPDATA%\\codeterrain`` on Windows, ``$XDG_CONFIG_HOME/codeterrain`` (default
    ``~/.config/codeterrain``) elsewhere."""
    override = os.environ.get(USER_CONFIG_ENV)
    if override:
        return Path(override)
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "codeterrain" / "config.toml"


def _user_table() -> tuple[dict[str, Any], str | None]:
    path = user_config_path()
    if not path.is_file():
        return {}, None
    table = _table_from_toml(path, nested_only=False) or {}
    other = sorted(set(table) - set(USER_ONLY_KEYS))
    if other:
        raise ConfigError(
            f"{path.as_posix()}: the user config takes only {', '.join(USER_ONLY_KEYS)}; "
            f"move {', '.join(other)} to --config or the repository's pyproject"
        )
    return table, path.as_posix()


def load_config(root: Path, config_path: str | os.PathLike[str] | None = None) -> Config:
    """Resolve the configuration for ``root``.

    Order: (a) ``config_path``, a TOML file whose ``[tool.codeterrain]`` table (or, failing that,
    top-level keys) is used; (b) ``root/codeterrain.toml``, read the same way; (c)
    ``[tool.codeterrain]`` in ``root/pyproject.toml``; (d) defaults. The user config
    (``user_config_path``) supplies ``USER_ONLY_KEYS`` that (a) does not set; the same keys in (b)
    or (c) are ignored and listed in ``Config.ignored``.
    """
    user, user_source = _user_table()
    table: dict[str, Any] = {}
    ignored: tuple[str, ...] = ()
    if config_path is not None:
        path = Path(config_path)
        if not path.is_file():
            raise ConfigError(f"--config file not found: {path.as_posix()}")
        table = _table_from_toml(path, nested_only=False) or {}
        source = f"--config {path.resolve().as_posix()}"
    else:
        source = "defaults"
        repo_config = root / REPO_CONFIG
        pyproject = root / "pyproject.toml"
        if repo_config.is_file():
            # A link out of the checkout is refused like any other repository path.
            _under_root(root, REPO_CONFIG, REPO_CONFIG)
            nested = _table_from_toml(repo_config, nested_only=False) or {}
            source = REPO_CONFIG
        elif pyproject.is_file():
            nested = _table_from_toml(pyproject, nested_only=True)
            source = "pyproject.toml [tool.codeterrain]" if nested is not None else source
        else:
            nested = None
        if nested is not None:
            ignored = tuple(k for k in USER_ONLY_KEYS if k in nested)
            table = {k: v for k, v in nested.items() if k not in USER_ONLY_KEYS}
    used_user = user_source if set(user) - set(table) else None
    config = _build(root, {**user, **table}, source)
    return dataclasses.replace(config, ignored=ignored, user_config=used_user)
