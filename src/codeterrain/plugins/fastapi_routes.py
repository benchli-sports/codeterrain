"""FastAPI route plugin: the ``iter_routes`` hook, run inside the target's interpreter.

FastAPI 0.137 stopped flattening included routers into ``app.routes``; they stay as nested
containers and ``fastapi.routing._iter_routes_with_context`` yields each concrete route together
with its effective (prefix- and dependency-merged) context. That helper is private, so it is read
in exactly one function (``iter_route_contexts``) with the flat ``app.routes`` walk as the fallback.

A context carries the handler only for ``APIRoute``; websockets, plain Starlette routes and mounts
sit in ``context.starlette_route`` (already prefixed). Mounts are walked into their sub-app;
``Host`` routes are not walked and are reported through ``host_routes_seen``.
"""

from __future__ import annotations

import inspect
import sys
import types
import typing
from collections.abc import Iterable, Iterator, Sequence
from typing import Any

from codeterrain.hookspecs import hookimpl

_HOSTS: list[str] = []


def forget_host_routes() -> None:
    _HOSTS.clear()


def host_routes_seen() -> list[str]:
    """Host patterns met (and skipped) by the last ``iter_routes`` call."""
    return list(_HOSTS)


def iter_route_contexts(routes: Sequence[Any]) -> Iterator[tuple[Any, Any | None]]:
    """(route, effective context or None) for every concrete route in ``routes``.

    Private API, isolated here: ``fastapi.routing._iter_routes_with_context`` (FastAPI >= 0.137).
    Older versions already hold the merged routes flat in ``app.routes``.
    """
    try:
        from fastapi import routing

        walker = getattr(routing, "_iter_routes_with_context", None)
    except ImportError:
        walker = None
    if walker is None:
        for route in routes:
            yield route, None
        return
    yield from walker(routes)


def _callable_name(func: Any) -> str:
    target = inspect.unwrap(func) if callable(func) else func
    module = getattr(target, "__module__", "") or ""
    name = getattr(target, "__qualname__", None) or type(target).__name__
    return f"{module}.{name}" if module else name


def _class_location(cls: type) -> tuple[str, int]:
    try:
        file = inspect.getsourcefile(cls) or inspect.getfile(cls)
        return file, inspect.getsourcelines(cls)[1]
    except (OSError, TypeError):
        module = sys.modules.get(getattr(cls, "__module__", ""), None)
        return getattr(module, "__file__", None) or "", 1


def _location(endpoint: Any) -> tuple[str, int]:
    """Absolute file and first line of the handler, decorators and wrappers looked through.

    A class endpoint (``HTTPEndpoint``) reports the class statement."""
    target = inspect.unwrap(endpoint)
    if inspect.isclass(target):
        return _class_location(target)
    for candidate in (target, type(target).__call__):
        code = getattr(candidate, "__code__", None)
        if code is not None:
            return code.co_filename, code.co_firstlineno
    return "", 0


def _is_websocket(source: Any) -> bool:
    from starlette.routing import WebSocketRoute

    return isinstance(source, WebSocketRoute)


def _methods(source: Any) -> list[str]:
    """HTTP methods sorted, ``WEBSOCKET`` for websockets, ``ANY`` when Starlette matches any method
    (``methods`` None, or empty: FastAPI < 0.137 turns a class endpoint's None into ``[]``)."""
    if not hasattr(source, "methods") or _is_websocket(source):
        return ["WEBSOCKET"]
    if not source.methods:
        return ["ANY"]
    found = sorted(source.methods)
    if "GET" in found and "HEAD" in found:
        found.remove("HEAD")
    return found


def _also_head(source: Any) -> bool:
    """True when the route answers HEAD next to GET (a Starlette ``Route`` adds it; a FastAPI
    ``APIRoute`` does not), which ``_methods`` folds into the GET record."""
    methods = getattr(source, "methods", None) or ()
    return "GET" in methods and "HEAD" in methods


def _type_name(tp: Any) -> str:
    """``Item``, ``list[Item]``, ``Optional[Item]``: how the annotation reads in source."""
    if tp is None or tp is type(None):
        return "None"
    if tp is Ellipsis:
        return "..."
    origin, args = typing.get_origin(tp), typing.get_args(tp)
    if origin is None:
        return getattr(tp, "__name__", None) or str(tp)
    if origin is typing.Annotated:
        return _type_name(args[0])
    # types.UnionType (X | Y) is 3.10+; the plugin also runs on a 3.9 target.
    if origin is typing.Union or origin is getattr(types, "UnionType", typing.Union):
        members = [a for a in args if a is not type(None)]
        inner = _type_name(members[0]) if len(members) == 1 else (
            "Union[" + ", ".join(_type_name(a) for a in members) + "]"
        )
        return f"Optional[{inner}]" if len(members) < len(args) else inner
    if origin is typing.Literal:
        return "Literal[" + ", ".join(repr(a) for a in args) + "]"
    name = getattr(origin, "__name__", None) or str(origin)
    return f"{name}[{', '.join(_type_name(a) for a in args)}]" if args else name


def _response_model(source: Any) -> str | None:
    model = getattr(source, "response_model", None)
    if model is None:
        return None
    return _type_name(model)


def _dependencies(source: Any) -> list[str]:
    dependant = getattr(source, "dependant", None)
    names: list[str] = []
    for dep in getattr(dependant, "dependencies", None) or []:
        call = getattr(dep, "call", None)
        if call is not None:
            names.append(_callable_name(call))
    return names


def _records(routes: Iterable[Any], prefix: str) -> Iterator[dict[str, Any]]:
    from starlette.routing import Host, Mount

    for route, context in iter_route_contexts(list(routes)):
        effective = getattr(context, "starlette_route", None)
        source = context if getattr(context, "endpoint", None) is not None else None
        source = source or effective or route
        if isinstance(source, Host) or isinstance(route, Host):
            _HOSTS.append(str(getattr(source, "host", "")))
            continue
        if isinstance(source, Mount):
            yield from _records(source.routes, prefix + source.path)
            continue
        endpoint = getattr(source, "endpoint", None)
        path = getattr(source, "path", None)
        if endpoint is None or path is None:
            continue
        file, line = _location(endpoint)
        methods_of = source if source is context else route
        for method in _methods(methods_of):
            yield {
                "method": method,
                "also_head": method == "GET" and _also_head(methods_of),
                "path": prefix + path,
                "handler": _callable_name(endpoint),
                "file": file,
                "line": line,
                "dependencies": _dependencies(source),
                "response_model": _response_model(source),
            }


@hookimpl
def iter_routes(app: Any) -> list[dict[str, Any]] | None:
    try:
        from starlette.applications import Starlette
    except ImportError:
        return None
    if not isinstance(app, Starlette):
        return None
    forget_host_routes()
    return list(_records(app.routes, ""))
