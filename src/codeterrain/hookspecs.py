"""pluggy hookspecs. Plugins run inside the target runtime, never in the core process."""

from __future__ import annotations

from typing import Any

import pluggy

PROJECT = "codeterrain"
hookspec = pluggy.HookspecMarker(PROJECT)
hookimpl = pluggy.HookimplMarker(PROJECT)


@hookspec(firstresult=True)
def iter_routes(app: Any) -> list[dict[str, Any]] | None:
    """Return the routes of ``app`` as dicts, or None when this plugin does not know the app.

    Each dict carries ``method``, ``path``, ``handler`` (qualified name), ``file`` (absolute),
    ``line``, ``dependencies`` (list of names) and ``response_model`` (name or None).
    """


@hookspec(firstresult=True)
def iter_exception_handlers(app: Any) -> list[dict[str, Any]] | None:
    """Return the exception handlers registered on ``app``, or None when this plugin does not
    know the app.

    Each dict carries ``exception`` (``module.qualname`` of the class it handles, "" for a
    status code), ``status`` (the status code it handles, or None), ``handler`` (qualified
    name), ``file`` (absolute), ``line`` and ``subclasses`` (``module.qualname`` of the loaded
    classes below ``exception``, so a class the core reads statically can be placed under it).
    """


@hookspec
def tests_for(file: str) -> list[str] | None:
    """Seam only in v0.1: the test node ids that exercise ``file``. Nothing calls it yet."""
