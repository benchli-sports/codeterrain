from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Literal, Optional, Union

import pytest
from fastapi import FastAPI, routing
from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.endpoints import HTTPEndpoint
from starlette.responses import PlainTextResponse
from starlette.routing import Host, Mount, Route, WebSocketRoute

from codeterrain.plugins import fastapi_routes

SAMPLE_REPO = Path(__file__).parent / "sample_repo"


@pytest.fixture
def sample_app(monkeypatch):
    monkeypatch.syspath_prepend(str(SAMPLE_REPO))
    for name in [m for m in sys.modules if m == "sampleapp" or m.startswith("sampleapp.")]:
        monkeypatch.delitem(sys.modules, name)
    import sampleapp.main

    yield sampleapp.main.app
    for name in [m for m in sys.modules if m == "sampleapp" or m.startswith("sampleapp.")]:
        sys.modules.pop(name, None)


def _by_key(records):
    return {(r["method"], r["path"]): r for r in records}


def _line_of(text):
    source = (SAMPLE_REPO / "sampleapp" / "main.py").read_text(encoding="utf-8")
    for number, line in enumerate(source.splitlines(), 1):
        if line.startswith(text):
            return number
    raise AssertionError(text)


def test_nested_routers_f_string_prefix_and_mount_resolve_to_full_paths(sample_app):
    routes = _by_key(fastapi_routes.iter_routes(sample_app))
    assert {
        ("GET", "/v1/items/{item_id}"),
        ("POST", "/v1/items"),
        ("GET", "/v1/items/{item_id}/parts"),
        ("GET", "/v1/admin/audit"),
        ("GET", "/legacy/ping"),
    } <= set(routes)


def test_handler_location_response_model_and_include_time_dependency(sample_app):
    routes = _by_key(fastapi_routes.iter_routes(sample_app))
    get_item = routes[("GET", "/v1/items/{item_id}")]
    assert get_item["handler"] == "sampleapp.main.get_item"
    assert get_item["response_model"] == "Item"
    assert get_item["dependencies"] == ["sampleapp.main.require_token"]
    assert get_item["file"].replace("\\", "/").endswith("sampleapp/main.py")
    assert get_item["line"] == _line_of('@items.get("/{item_id}"')
    assert routes[("POST", "/v1/items")]["response_model"] == "Item"
    assert routes[("GET", "/v1/admin/audit")]["dependencies"] == []


def test_decorated_handler_resolves_to_the_wrapped_function_not_the_wrapper(sample_app):
    audit = _by_key(fastapi_routes.iter_routes(sample_app))[("GET", "/v1/admin/audit")]
    assert audit["handler"] == "sampleapp.main.audit_log"
    assert audit["line"] == _line_of('@admin.get("/audit")')


def test_get_routes_do_not_list_the_implied_head(sample_app):
    methods = {r["method"] for r in fastapi_routes.iter_routes(sample_app)}
    assert methods == {"GET", "POST", "WEBSOCKET"}


def test_non_starlette_objects_are_not_recognised():
    assert fastapi_routes.iter_routes(object()) is None


def test_private_walker_is_used_when_present_and_the_flat_walk_otherwise(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(
        routing, "_iter_routes_with_context", lambda r: iter([(sentinel, "ctx")]), raising=False
    )
    assert list(fastapi_routes.iter_route_contexts(["a"])) == [(sentinel, "ctx")]
    monkeypatch.delattr(routing, "_iter_routes_with_context")
    assert list(fastapi_routes.iter_route_contexts(["a", "b"])) == [("a", None), ("b", None)]


class Endpoint(HTTPEndpoint):
    async def get(self, request):
        return PlainTextResponse("x")


def _plain(request):
    return PlainTextResponse("x")


async def _socket(websocket):
    await websocket.close()


def _walk(*routes):
    return fastapi_routes.iter_routes(Starlette(routes=list(routes)))


def _line_in_this_file(text):
    for number, line in enumerate(Path(__file__).read_text(encoding="utf-8").splitlines(), 1):
        if line.startswith(text):
            return number
    raise AssertionError(text)


def test_websocket_under_an_included_router_is_listed_with_its_full_path(sample_app):
    found = _by_key(fastapi_routes.iter_routes(sample_app))
    live = found[("WEBSOCKET", "/v1/items/{item_id}/live")]
    assert live["handler"] == "sampleapp.main.live"
    assert live["line"] == _line_of('@items.websocket("/{item_id}/live"')
    assert live["response_model"] is None


def test_sub_app_mounted_inside_an_included_router_is_walked_with_the_full_prefix(sample_app):
    found = _by_key(fastapi_routes.iter_routes(sample_app))
    if hasattr(routing, "_iter_routes_with_context"):
        status = found[("GET", "/v1/embedded/status")]
        assert status["handler"] == "sampleapp.main.embedded_status"
        assert status["line"] == _line_of("def embedded_status")
    else:
        assert not [k for k in found if "/embedded" in k[1]], "FastAPI < 0.137 drops the mount"


def test_a_top_level_websocket_and_nested_mounts_resolve_to_full_paths():
    found = _by_key(
        _walk(
            WebSocketRoute("/ws", _socket),
            Mount("/a", routes=[Mount("/b", routes=[Route("/c", _plain)])]),
        )
    )
    assert set(found) == {("WEBSOCKET", "/ws"), ("GET", "/a/b/c")}
    assert found[("WEBSOCKET", "/ws")]["handler"] == f"{__name__}._socket"


def test_a_head_only_route_is_kept_and_methods_come_out_sorted():
    records = _walk(
        Route("/h", _plain, methods=["HEAD"]),
        Route("/m", _plain, methods=["PUT", "DELETE", "GET", "PATCH"]),
    )
    assert [r["method"] for r in records if r["path"] == "/h"] == ["HEAD"]
    assert [r["method"] for r in records if r["path"] == "/m"] == [
        "DELETE", "GET", "PATCH", "PUT",
    ]


def test_a_get_record_says_whether_the_route_also_answers_head():
    starlette = _walk(Route("/s", _plain), Route("/h", _plain, methods=["HEAD"]))
    assert [(r["method"], r["also_head"]) for r in starlette] == [("GET", True), ("HEAD", False)]
    app = FastAPI()
    app.get("/f")(_plain)
    (record,) = [r for r in fastapi_routes.iter_routes(app) if r["path"] == "/f"]
    assert (record["method"], record["also_head"]) == ("GET", False)


def test_a_class_endpoint_reports_the_class_file_and_line_and_any_method():
    (record,) = _walk(Route("/cls", Endpoint))
    assert record["method"] == "ANY"
    assert record["handler"] == f"{__name__}.Endpoint"
    assert Path(record["file"]).resolve() == Path(__file__).resolve()
    assert record["line"] == _line_in_this_file("class Endpoint(")


def test_an_empty_method_set_is_any_and_a_missing_one_is_a_websocket():
    assert fastapi_routes._methods(SimpleNamespace(methods=set())) == ["ANY"]
    assert fastapi_routes._methods(SimpleNamespace(methods=None)) == ["ANY"]
    assert fastapi_routes._methods(SimpleNamespace()) == ["WEBSOCKET"]
    assert fastapi_routes._methods(SimpleNamespace(methods={"HEAD", "GET"})) == ["GET"]
    assert fastapi_routes._methods(SimpleNamespace(methods={"HEAD"})) == ["HEAD"]


def test_host_routes_are_not_walked_and_are_reported():
    records = _walk(Host("api.example.com", app=Starlette(routes=[Route("/x", _plain)])))
    assert records == []
    assert fastapi_routes.host_routes_seen() == ["api.example.com"]
    fastapi_routes.forget_host_routes()
    assert fastapi_routes.host_routes_seen() == []


class Item(BaseModel):
    id: int


def test_response_models_render_on_a_python_without_union_type(monkeypatch):
    # `types.UnionType` is 3.10+, and the AttributeError on a 3.9 target
    # dropped every route of the application that had a list[Item] response model.
    monkeypatch.setattr(fastapi_routes, "types", SimpleNamespace())  # a 3.9 `types` module
    assert fastapi_routes._type_name(list[int]) == "list[int]"
    assert fastapi_routes._type_name(Optional[int]) == "Optional[int]"  # noqa: UP045


@pytest.mark.parametrize(
    ("annotation", "expected"),
    [
        (Item, "Item"),
        (list[Item], "list[Item]"),
        (Optional[Item], "Optional[Item]"),  # noqa: UP045
        (Item | None, "Optional[Item]"),
        (dict[str, list[int]], "dict[str, list[int]]"),
        (Union[int, str], "Union[int, str]"),  # noqa: UP007
        (Optional[Union[int, str]], "Optional[Union[int, str]]"),  # noqa: UP007, UP045
        (tuple[int, ...], "tuple[int, ...]"),
        (Literal["a", "b"], "Literal['a', 'b']"),
        (Annotated[Item, "meta"], "Item"),
        (int, "int"),
    ],
)
def test_response_models_render_as_they_read_in_source(annotation, expected):
    assert fastapi_routes._type_name(annotation) == expected
    app = FastAPI()

    @app.get("/x", response_model=annotation)
    def handler():
        return None

    (record,) = [r for r in fastapi_routes.iter_routes(app) if r["path"] == "/x"]
    assert record["response_model"] == expected


def test_exception_handlers_name_class_or_status_and_the_loaded_classes_below():
    app = FastAPI()

    class Base(Exception):
        pass

    class Child(Base):
        pass

    @app.exception_handler(Base)
    async def on_base(request, exc):
        return PlainTextResponse("x", status_code=418)

    @app.exception_handler(404)
    async def on_404(request, exc):
        return PlainTextResponse("x", status_code=404)

    found = {
        (h["exception"].rpartition(".")[2] or h["status"]): h
        for h in fastapi_routes.iter_exception_handlers(app)
    }
    assert found["Base"]["handler"].endswith("on_base")
    assert found["Base"]["status"] is None and found["Base"]["line"] > 0
    assert [s.rpartition(".")[2] for s in found["Base"]["subclasses"]] == ["Child"]
    assert found[404]["exception"] == "" and found[404]["subclasses"] == []
    # FastAPI's own handlers are registered too, with what they answer for.
    assert found["HTTPException"]["handler"] == (
        "fastapi.exception_handlers.http_exception_handler"
    )
    assert fastapi_routes.iter_exception_handlers(object()) is None
