"""The extractor protocol, as the core reads it.

An extractor is any program that runs inside the target's runtime and writes one JSON document to
the path in ``$CODETERRAIN_RESULT``. The core never imports extractor code in-process; it only reads
that document and refuses any whose ``schema`` it does not know or whose fields have the wrong type.
See docs/extractor-protocol.md.
"""

from __future__ import annotations

from typing import Any

ORDER_SCHEMA = "codeterrain.order/1"
ROUTES_SCHEMA = "codeterrain.routes/1"

ORDER_KEYS = ("closures", "nodes", "errors")
ROUTES_KEYS = ("routes", "errors")
FIXTURE_KEYS = ("name", "scope", "file", "line")
ROUTE_KEYS = ("app", "method", "path", "handler", "file", "line", "dependencies", "response_model")
ERROR_KEYS = ("app", "error")


class ProtocolError(ValueError):
    """The extractor's document is not one this core understands."""


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _require(schema: str, ok: bool, what: str) -> None:
    if not ok:
        raise ProtocolError(f"{schema}: {what}")


def _check_fixture(schema: str, fx: Any) -> None:
    _require(schema, isinstance(fx, dict), "a closure entry must be an object")
    _require(
        schema,
        all(k in fx for k in FIXTURE_KEYS),
        f"each fixture needs {', '.join(FIXTURE_KEYS)}",
    )
    _require(
        schema,
        all(isinstance(fx[k], str) for k in ("name", "scope", "file")),
        "fixture name, scope and file must be strings",
    )
    _require(schema, _is_int(fx["line"]), "fixture line must be an integer")


def _check_order(doc: dict[str, Any], schema: str) -> None:
    _require(schema, _is_int(doc["errors"]), "field 'errors' must be int")
    closures = doc["closures"]
    for closure in closures:
        _require(schema, isinstance(closure, list), "each closure must be a list of fixtures")
        for fx in closure:
            _check_fixture(schema, fx)
    for node, index in doc["nodes"].items():
        _require(schema, isinstance(node, str), "node ids must be strings")
        _require(schema, _is_int(index), f"node {node!r} must map to an integer closure index")
        _require(
            schema,
            0 <= index < len(closures),
            f"node {node!r} points at a closure that does not exist",
        )


def _check_routes(doc: dict[str, Any], schema: str) -> None:
    for route in doc["routes"]:
        _require(schema, isinstance(route, dict), "each route must be an object")
        _require(
            schema,
            all(k in route for k in ROUTE_KEYS),
            f"each route needs {', '.join(ROUTE_KEYS)}",
        )
        strings = ("app", "method", "path", "handler", "file")
        _require(
            schema,
            all(isinstance(route[k], str) for k in strings),
            f"route {', '.join(strings)} must be strings",
        )
        _require(schema, _is_int(route["line"]), "route line must be an integer")
        _require(schema, _is_str_list(route["dependencies"]), "route dependencies must be strings")
        _require(
            schema,
            route["response_model"] is None or isinstance(route["response_model"], str),
            "route response_model must be a string or null",
        )
        _require(
            schema,
            isinstance(route.get("also_head", False), bool),
            "route also_head must be a boolean",
        )
    handlers = doc.get("exception_handlers", [])
    _require(schema, isinstance(handlers, list), "exception_handlers must be a list")
    for h in handlers:
        _require(schema, isinstance(h, dict), "each exception handler must be an object")
        strings = ("app", "exception", "handler", "file")
        _require(
            schema,
            all(isinstance(h.get(k), str) for k in strings),
            f"exception handler {', '.join(strings)} must be strings",
        )
        _require(schema, _is_int(h.get("line")), "exception handler line must be an integer")
        _require(
            schema,
            h.get("status") is None or _is_int(h.get("status")),
            "exception handler status must be an integer or null",
        )
        _require(
            schema,
            _is_str_list(h.get("subclasses")),
            "exception handler subclasses must be strings",
        )
    for failure in doc["errors"]:
        _require(schema, isinstance(failure, dict), "each error must be an object")
        _require(
            schema,
            all(k in failure for k in ERROR_KEYS),
            f"each error needs {', '.join(ERROR_KEYS)}",
        )
        _require(
            schema,
            all(isinstance(failure[k], str) for k in ERROR_KEYS),
            "error app and error must be strings",
        )
        _require(
            schema,
            "chain" not in failure or _is_str_list(failure["chain"]),
            "error chain must be a list of strings",
        )


def check(doc: Any, schema: str) -> dict[str, Any]:
    """Return ``doc`` if it is a valid ``schema`` document; raise ProtocolError otherwise."""
    if not isinstance(doc, dict):
        raise ProtocolError(f"extractor output is not a JSON object (expected {schema})")
    found = doc.get("schema")
    if found != schema:
        raise ProtocolError(
            f"unsupported extractor schema {found!r}: this core reads {schema!r}; the extractor "
            "and the core must agree on the protocol version (docs/extractor-protocol.md)"
        )
    if schema == ORDER_SCHEMA:
        kinds: dict[str, type] = {"closures": list, "nodes": dict, "errors": int}
    else:
        kinds = {"routes": list, "errors": list}
    for key, kind in kinds.items():
        if not isinstance(doc.get(key), kind):
            raise ProtocolError(f"{schema}: field {key!r} must be {kind.__name__}")
    if schema == ORDER_SCHEMA:
        _check_order(doc, schema)
    else:
        _check_routes(doc, schema)
    return doc
