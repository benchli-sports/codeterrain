"""Join extracted routes to an access manifest (a JSON file the repository already keeps).

Host side: reads the manifest, never imports the target. Rows are matched on (method, path) with
path converters ignored; the first matching row wins. A row that lists ``apps`` applies only to the
routes whose app module equals one of them (an empty list applies to none; a bare string is one
module); a row without ``apps`` applies to every app.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ManifestError(ValueError):
    """The route manifest is missing or does not have the configured shape."""


@dataclass
class JoinResult:
    rows: list[dict[str, Any]]
    routes_without_row: list[dict[str, Any]] = field(default_factory=list)
    rows_without_route: list[dict[str, Any]] = field(default_factory=list)


def load_rows(path: Path, keys: dict[str, str]) -> list[dict[str, Any]]:
    try:
        document = json.loads(path.read_bytes())
    except OSError as exc:
        raise ManifestError(f"route manifest not readable: {path.as_posix()} ({exc})") from exc
    except ValueError as exc:
        raise ManifestError(f"route manifest is not JSON: {path.as_posix()} ({exc})") from exc
    if isinstance(document, list):
        rows = document
    elif isinstance(document, dict):
        rows = document.get(keys["rows"])
    else:
        raise ManifestError("route manifest must be a JSON object or a list of rows")
    if not isinstance(rows, list):
        raise ManifestError(f"route manifest has no list under {keys['rows']!r}")
    for key in ("method", "path", "access"):
        if any(not isinstance(r, dict) or keys[key] not in r for r in rows):
            raise ManifestError(f"route manifest rows must all carry {keys[key]!r} ({key})")
    for key in ("method", "path"):
        if any(not isinstance(r[keys[key]], str) for r in rows):
            raise ManifestError(f"manifest {keys[key]!r} ({key}) must be a string in every row")
    if any(not isinstance(r[keys["access"]], str) for r in rows):
        raise ManifestError(f"manifest {keys['access']!r} (access) must be a string in every row")
    for row in rows:
        if keys["apps"] in row and not _app_list(row[keys["apps"]]):
            raise ManifestError(
                f"manifest {keys['apps']!r} (apps) must be a string or a list of strings"
            )
    return rows


def _app_list(value: Any) -> bool:
    return isinstance(value, str) or (
        isinstance(value, list) and all(isinstance(v, str) for v in value)
    )


_CONVERTER = re.compile(r"\{([^}:]+):[^}]*\}")


def route_key(method: str, path: str) -> tuple[str, str]:
    """(METHOD, path) with Starlette converters dropped: ``{slug:path}`` and ``{slug}`` are one."""
    return method.upper(), _CONVERTER.sub(lambda m: "{" + m.group(1) + "}", path)


def _app_module(app: str) -> str:
    return app.partition(":")[0]


def _applies(row_apps: Any, app: str) -> bool:
    if row_apps is None:
        return True
    modules = [row_apps] if isinstance(row_apps, str) else row_apps
    return _app_module(app) in modules


def join(
    routes: list[dict[str, Any]], manifest_rows: list[dict[str, Any]], keys: dict[str, str]
) -> JoinResult:
    index: dict[tuple[str, str], list[int]] = {}
    for position, row in enumerate(manifest_rows):
        key = route_key(str(row[keys["method"]]), row[keys["path"]])
        index.setdefault(key, []).append(position)
    used: set[int] = set()
    result = JoinResult(rows=[])
    for route in routes:
        chosen = None
        for position in index.get(route_key(route["method"], route["path"]), []):
            if _applies(manifest_rows[position].get(keys["apps"]), route["app"]):
                chosen = position
                break
        if chosen is None:
            result.routes_without_row.append(route)
            result.rows.append({**route, "access": None})
            continue
        used.add(chosen)
        result.rows.append({**route, "access": manifest_rows[chosen][keys["access"]]})
    result.rows_without_route = [r for i, r in enumerate(manifest_rows) if i not in used]
    return result
