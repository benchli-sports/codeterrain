"""The ``routes`` view: the configured apps' routes, extracted by importing them in the sandbox.

The core runs ``python -m codeterrain._routes_runner`` in the sandbox and reads the
``codeterrain.routes/1`` document it leaves behind (docs/extractor-protocol.md). The access class
is joined from the configured route manifest on every read, so a manifest edit shows at once.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from codeterrain import protocol
from codeterrain.config import Config
from codeterrain.fixtures_order import ExtractError
from codeterrain.plugins import manifest_join
from codeterrain.sandbox import redact_document, redactor, run_python

VIEW = "routes"


def extract(config: Config, cache_dir: Path, python: str) -> dict[str, Any]:
    if not config.apps:
        raise ExtractError("no apps configured; set apps = [\"module:attr\"] in the config")
    args = ["-m", "codeterrain._routes_runner", "--apps", *config.apps]
    if config.plugins:
        args += ["--plugins", *config.plugins]
    if config.moto_mock:
        args.append("--moto-mock")
    result = run_python(python, args, config, cache_dir)
    if result.result is None:
        detail = (result.stderr.strip() or result.stdout.strip())[-1200:]
        raise ExtractError(f"route walk left no document (exit {result.returncode}): {detail}")
    # Every string the child reports, optional and plugin fields included, is its own text and
    # may quote an environment value: redact it before it is cached.
    return protocol.check(redact_document(result.result, redactor(config)), protocol.ROUTES_SCHEMA)


def joined(doc: dict[str, Any], config: Config) -> manifest_join.JoinResult:
    """Routes with their ``access`` (None when no manifest is configured or no row matches)."""
    routes = sorted(doc["routes"], key=lambda r: (r["path"], r["method"], r["app"]))
    if config.route_manifest is None:
        return manifest_join.JoinResult(rows=[{**r, "access": None} for r in routes])
    rows = manifest_join.load_rows(config.route_manifest, config.manifest_keys)
    return manifest_join.join(routes, rows, config.manifest_keys)


def select(
    rows: list[dict[str, Any]],
    substring: str = "",
    app: str | None = None,
    access: str | None = None,
) -> list[dict[str, Any]]:
    needle = substring.lower()
    return [
        r
        for r in rows
        if (needle in r["path"].lower() or needle in r["handler"].lower())
        and (app is None or app in r["app"])
        and (access is None or (r["access"] or "").lower() == access.lower())
    ]
