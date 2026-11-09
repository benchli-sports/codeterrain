"""Which files each cached view depends on, and how many of them moved since it was built."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codeterrain import __version__, calls_static, fixtures_static, protocol
from codeterrain.cache import Cache, ViewRecord, changed_files
from codeterrain.config import Config
from codeterrain.executables import resolve_executable
from codeterrain.sandbox import SandboxError, probe_versions

STATIC_VIEWS = ("fixtures", "calls")
SLOW_VIEWS = ("order", "routes")
ALL_VIEWS = STATIC_VIEWS + SLOW_VIEWS
SCHEMAS = {"order": protocol.ORDER_SCHEMA, "routes": protocol.ROUTES_SCHEMA}
CONFIG_FILES = ("pyproject.toml", "codeterrain.toml", "pytest.ini", "setup.cfg", "tox.ini")
#: Config fields that do not change what an extraction produces: ``root`` (the cache is per
#: checkout), ``source`` (where the values came from, not the values), ``route_manifest`` and
#: ``manifest_keys`` (read at query time by the access join, never by the extractors), and
#: ``ignored`` and ``user_config`` (reporting only: dropped values never reach the child, and the
#: values in use are keyed under their own names), and ``client_paths``, ``path_prefix`` and
#: ``client_repos`` (the frontend calls view, whose own fingerprint is the files it read, and the
#: join, which runs at query time).
KEY_EXEMPT = frozenset(
    {
        "root",
        "source",
        "route_manifest",
        "manifest_keys",
        "ignored",
        "user_config",
        "client_paths",
        "path_prefix",
        "client_repos",
    }
)
SITE_PACKAGES = ("Lib/site-packages", "lib/python*/site-packages", "lib/python*/dist-packages")


def _site_dirs(prefix: Path) -> list[Path]:
    found: list[Path] = []
    for pattern in SITE_PACKAGES:
        found.extend(p for p in prefix.glob(pattern) if p.is_dir())
    return sorted(found)


def interpreter_fingerprint(
    python: str, config: Config | None = None, cache: Cache | None = None
) -> dict[str, Any]:
    """What identifies the target runtime: the interpreter path, its venv config and the names
    (with versions) of the distributions installed beside it. Falls back to the version probe when
    no site-packages sits next to the interpreter; the probe (python, prefix, site dirs) is cached
    against the interpreter file, but the ``*.dist-info`` listing of those site dirs is read on
    every call, so a ``pip install`` behind a launcher or shim still changes the key."""
    try:
        exe = Path(resolve_executable(python, untrusted=config.root if config else None))
    except FileNotFoundError:
        exe = Path(os.path.abspath(python))
    info: dict[str, Any] = {"path": exe.as_posix()}
    dists: list[str] = []
    for prefix in (exe.parent, exe.parent.parent):
        cfg = prefix / "pyvenv.cfg"
        if cfg.is_file() and "pyvenv_cfg" not in info:
            info["pyvenv_cfg"] = cfg.read_text(encoding="utf-8", errors="replace")
        for site in _site_dirs(prefix):
            with os.scandir(site) as entries:
                dists.extend(e.name for e in entries if e.name.endswith(".dist-info"))
    if dists:
        info["dist_info"] = sorted(set(dists))
    elif config is not None and cache is not None:
        try:
            probe = cache.cached_probe(exe, lambda: _stable_probe(python, config, cache))
        except SandboxError:
            probe = None
        info["probe"] = probe
        if probe is not None:
            info["dist_info"] = _listed_dists(probe.get("sites"))
    return info


def _stable_probe(python: str, config: Config, cache: Cache) -> dict[str, Any]:
    """The probe minus its distribution listing, which is read live (see above)."""
    found = probe_versions(python, config, cache.dir)
    return {k: v for k, v in found.items() if k != "dists"}


def _listed_dists(sites: Any) -> list[str]:
    names: set[str] = set()
    for site in sites if isinstance(sites, list) else []:
        try:
            with os.scandir(site) as entries:
                names.update(e.name for e in entries if e.name.endswith(".dist-info"))
        except OSError:
            continue
    return sorted(names)


def pass_env_digests(names: tuple[str, ...], key: bytes) -> dict[str, str | None]:
    """HMAC-SHA256 of ``NAME=value`` under the cache's own random key for each listed variable
    (None when unset); never the value, and not guessable offline from a copied view file."""
    return {
        name: (
            hmac.new(key, f"{name}={os.environ[name]}".encode(), hashlib.sha256).hexdigest()
            if name in os.environ
            else None
        )
        for name in names
    }


def view_key(cache: Cache, config: Config, python: str, view: str) -> str:
    """Hash of everything besides the tracked files that a slow view's content depends on."""
    material = {
        "config": {k: v for k, v in config.as_dict().items() if k not in KEY_EXEMPT},
        "pass_env_values": pass_env_digests(
            config.pass_env, cache.secret_key() if config.pass_env else b""
        ),
        "interpreter": interpreter_fingerprint(python, config, cache),
        "codeterrain": __version__,
        "protocol": SCHEMAS[view],
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()


def tracked_fingerprint(cache: Cache, config: Config) -> dict[str, str]:
    """Every Python file under the root (conftest.py included), plus the project config files
    (pyproject.toml, pytest.ini, setup.cfg, tox.ini) and the route manifest."""
    fingerprint = cache.scan(None)
    extras = [config.root / name for name in CONFIG_FILES]
    if config.route_manifest is not None:
        extras.append(config.route_manifest)
    fingerprint.update(cache.hash_files(extras))
    return fingerprint


def stale_files(cache: Cache, config: Config, record: ViewRecord) -> int:
    """Files that differ now from what ``record`` was built from."""
    if record.view == calls_static.VIEW:
        current = calls_static.current_fingerprint(cache, config)
    elif record.view in STATIC_VIEWS:
        current = cache.scan(fixtures_static.scanned_paths(config))
    else:
        current = tracked_fingerprint(cache, config)
    cache.flush()
    return changed_files(record.fingerprint, current)


@dataclass
class Served:
    record: ViewRecord
    stale: int
    built_now: bool


def serve_slow(
    cache: Cache,
    config: Config,
    view: str,
    schema: str,
    build: Callable[[], dict[str, Any]],
    python: str,
    fresh: bool = False,
    notify: Callable[[str], None] = lambda message: None,
) -> Served:
    """The cached slow-tier view, stamped stale when files moved; built only if absent or ``fresh``.

    A cached document whose schema this core no longer reads, or whose key (config, interpreter,
    versions) differs from the current one, is treated as absent and rebuilt.
    """
    key = view_key(cache, config, python, view)
    record = cache.read_view(view)
    if record is not None and not record.verified:
        # A view this core wrote and signed was checked before it was written.
        try:
            protocol.check(record.data, schema)
        except protocol.ProtocolError:
            record = None
    if record is not None and record.key != key:
        record = None
    if record is None or fresh:
        notify(f"building {view} (imports your code in a sandbox; this can take a while)")
        fingerprint = tracked_fingerprint(cache, config)
        # Checked here as well as in the builder: the view is signed when written, and a signed
        # view is served without another check.
        record = cache.write_view(
            view, protocol.check(build(), schema), fingerprint, key, sign=True
        )
        cache.flush()
        return Served(record, 0, True)
    return Served(record, stale_files(cache, config, record), False)
