"""Generic app importer, run INSIDE the target interpreter (``-m codeterrain._routes_runner``).

Imports each ``module:attr`` application, asks the pluggy ``iter_routes`` hook for its routes and
writes a ``codeterrain.routes/1`` document (docs/extractor-protocol.md) to ``$CODETERRAIN_RESULT``.
One app failing to import is recorded in ``errors`` and does not stop the others.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any

import pluggy

from codeterrain import hookspecs
from codeterrain.plugins import fastapi_routes

SCHEMA = "codeterrain.routes/1"


def make_plugin_manager(extra: list[str]) -> pluggy.PluginManager:
    manager = pluggy.PluginManager(hookspecs.PROJECT)
    manager.add_hookspecs(hookspecs)
    manager.register(fastapi_routes)
    for module in extra:
        manager.register(importlib.import_module(module))
    return manager


def relative_file(file: str, root: Path) -> str:
    """Repo-relative POSIX path with symlinks and junctions resolved; absolute when outside."""
    if not file:
        return ""
    real = Path(os.path.realpath(file))
    try:
        return real.relative_to(root).as_posix()
    except ValueError:
        return real.as_posix()


def exception_chain(exc: BaseException | None) -> list[str]:
    """``Type: message`` for the exception and each cause behind it, outermost first."""
    chain: list[str] = []
    while exc is not None and len(chain) < 12:
        chain.append(f"{type(exc).__name__}: {exc}")
        exc = exc.__cause__ or exc.__context__
    return chain


def collect(
    apps: list[str], plugins: list[str], root: Path, moto_mock: bool
) -> dict[str, Any]:
    routes: list[dict[str, Any]] = []
    handlers: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    notes: list[str] = []
    sys.path.insert(0, str(root))
    with contextlib.ExitStack() as stack:
        if moto_mock:
            try:
                from moto import mock_aws
            except ImportError:
                notes.append("moto_mock is set but moto is not importable in the target")
            else:
                stack.enter_context(mock_aws())
                notes.append("imports ran inside moto.mock_aws()")
        manager = make_plugin_manager(plugins)
        for spec in apps:
            module_name, _, attr = spec.partition(":")
            fastapi_routes.forget_host_routes()
            try:
                app = getattr(importlib.import_module(module_name), attr)
                found = manager.hook.iter_routes(app=app)
                if found is None:
                    raise TypeError(f"no plugin recognises {type(app).__name__}")
            except Exception as exc:
                tail = traceback.format_exc().strip().splitlines()
                errors.append(
                    {"app": spec, "error": "\n".join(tail[-30:]), "chain": exception_chain(exc)}
                )
                continue
            notes.extend(
                f"warning: {spec}: Host route {host!r} is not walked, its routes are missing"
                for host in fastapi_routes.host_routes_seen()
            )
            for record in found:
                routes.append(
                    {**record, "app": spec, "file": relative_file(record["file"], root)}
                )
            try:
                registered = manager.hook.iter_exception_handlers(app=app) or []
            except Exception as exc:  # the routes stand; the handlers are reported missing
                notes.append(f"warning: {spec}: exception handlers not read: {exc}")
                registered = []
            for record in registered:
                handlers.append(
                    {**record, "app": spec, "file": relative_file(record["file"], root)}
                )
    return {
        "schema": SCHEMA,
        "generator": {"name": "codeterrain-routes-runner", "python": sys.version.split()[0]},
        "routes": routes,
        "exception_handlers": handlers,
        "errors": errors,
        "notes": notes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="codeterrain._routes_runner")
    parser.add_argument("--apps", nargs="*", default=[])
    parser.add_argument("--plugins", nargs="*", default=[])
    parser.add_argument("--moto-mock", action="store_true")
    args = parser.parse_args(argv)
    root = Path(os.path.realpath(os.environ.get("CODETERRAIN_ROOT") or os.getcwd()))
    document = collect(args.apps, args.plugins, root, args.moto_mock)
    # bytes, not write_text(newline=...), which needs 3.10 in the target's interpreter
    Path(os.environ["CODETERRAIN_RESULT"]).write_bytes(json.dumps(document).encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
