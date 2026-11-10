from __future__ import annotations

from codeterrain.cache import Cache
from codeterrain.code_static import Index, update
from codeterrain.config import load_config
from codeterrain.raises import Trees, named, outcome, raised, reaching

FILES = {
    "app/__init__.py": "",
    "app/errors.py": (
        "from fastapi import HTTPException\n\n\n"
        "class NotFound(HTTPException):\n"
        "    def __init__(self, what):\n"
        "        super().__init__(status_code=404, detail=what)\n\n\n"
        "class Conflict(Exception):\n    pass\n\n\n"
        "class Gone(Conflict):\n    status_code = 410\n"
    ),
    "app/services.py": (
        "from fastapi import HTTPException, status\n\n"
        "from app.errors import Conflict, Gone, NotFound\n\n\n"
        "def load(order_id):\n"
        "    if order_id < 0:\n        raise ValueError('negative')\n"
        "    if order_id == 0:\n        raise NotFound('order')\n"
        "    if order_id == 1:\n        raise Gone()\n"
        "    if order_id == 2:\n        raise HTTPException(status.HTTP_403_FORBIDDEN)\n"
        "    def inner():\n        raise Conflict()\n"
        "    return order_id\n\n\n"
        "def current_order(order_id):\n    return load(order_id)\n"
    ),
    "app/main.py": (
        "from typing import Annotated\n\n"
        "from fastapi import Depends\n"
        "from fastapi.responses import JSONResponse\n\n"
        "from app.services import current_order, load\n\n\n"
        "async def conflict_handler(request, exc):\n"
        "    return JSONResponse({}, status_code=409)\n\n\n"
        "def order_dep(order=Depends(current_order)):\n    return order\n\n\n"
        "def get_order(order_id: int):\n    return load(order_id)\n\n\n"
        "def summary(order: Annotated[dict, Depends(order_dep)]):\n    return order\n\n\n"
        "def health():\n    return {}\n"
    ),
}


def _repo(tmp_path):
    root = tmp_path / "repo"
    for rel, text in FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return root, Index(update(Cache(root, tmp_path / "cache"), load_config(root)))


def _route(path, handler, deps=()):
    return {"app": "app.main:app", "method": "GET", "path": path, "handler": handler,
            "dependencies": list(deps)}


ROUTES = [
    _route("/orders/{id}", "app.main.get_order"),
    # FastAPI lists a dependency declared in the signature, so the route reaches it directly...
    _route("/orders/{id}/summary", "app.main.summary", ["app.main.order_dep"]),
    _route("/health", "app.main.health"),
]
HANDLERS = [
    {"app": "app.main:app", "exception": "starlette.exceptions.HTTPException", "status": None,
     "handler": "fastapi.exception_handlers.http_exception_handler", "file": "/lib/f.py",
     "line": 1, "subclasses": ["fastapi.exceptions.HTTPException"]},
    {"app": "app.main:app", "exception": "app.errors.Conflict", "status": None,
     "handler": "app.main.conflict_handler", "file": "app/main.py", "line": 9,
     "subclasses": ["app.errors.Gone"]},
]


def test_the_own_raises_of_a_function_with_the_status_each_states(tmp_path):
    root, index = _repo(tmp_path)
    found = [(r["exception"], r["status"]) for r in raised(index, Trees(root), "app.services.load")]
    # A raise in a nested function is that function's, not this one's.
    assert found == [
        ("builtins.ValueError", None),
        ("app.errors.NotFound", 404),  # from the class's __init__
        ("app.errors.Gone", 410),  # from the class body
        ("fastapi.HTTPException", 403),  # a status constant at the raise site
    ]


def test_routes_reach_through_callers_and_dependencies(tmp_path):
    root, index = _repo(tmp_path)
    found, cut = reaching(index, Trees(root), ROUTES, "app.services.load")
    assert not cut
    assert [(r["path"], via) for r, via in found] == [
        ("/orders/{id}", ["app.main.get_order", "app.services.load"]),
        ("/orders/{id}/summary",
         ["app.main.order_dep", "app.services.current_order", "app.services.load"]),
    ]
    # ...and one only declared in another dependency's signature is followed through it.
    routes = [_route("/s", "app.main.summary")]
    found, _ = reaching(index, Trees(root), routes, "app.services.current_order")
    assert [via for _, via in found] == [
        ["app.main.summary", "app.main.order_dep", "app.services.current_order"]
    ]
    # A dependency that is a callable instance is named by its class; its body is `__call__`.
    (root / "app" / "checks.py").write_text(
        "from app.services import load\n\n\n"
        "class Checker:\n    def __call__(self):\n        return load(1)\n",
        encoding="utf-8",
    )
    index = Index(update(Cache(root, tmp_path / "cache2"), load_config(root)))
    routes = [_route("/c", "app.main.health", ["app.checks.Checker"])]
    found, _ = reaching(index, Trees(root), routes, "app.services.load")
    assert [via for _, via in found] == [["app.checks.Checker.__call__", "app.services.load"]]
    assert reaching(index, Trees(root), ROUTES, "app.services.load", depth=1)[0][0][1] == [
        "app.main.get_order", "app.services.load"
    ]


def test_the_status_comes_from_the_nearest_handler_or_is_500(tmp_path):
    root, index = _repo(tmp_path)
    trees = Trees(root)

    def status(exception, raised_status=None, handlers=HANDLERS):
        return outcome(index, trees, handlers, exception, raised_status)["status"]

    assert status("builtins.ValueError") == 500
    # The framework's handler answers with the status the exception carries.
    assert status("app.errors.NotFound", 404) == 404
    assert status("fastapi.HTTPException", 403) == 403
    # A handler in the repository answers with the literal status its body responds with,
    # for its class and for a subclass.
    assert status("app.errors.Conflict") == 409
    assert status("app.errors.Gone", 410) == 409
    # A handler registered for a status code wins for an exception carrying it.
    by_code = [*HANDLERS, {**HANDLERS[1], "exception": "", "status": 404, "subclasses": []}]
    assert status("app.errors.NotFound", 404, by_code) == 409
    # ...and only for an HTTPException: another class stating a status goes by its class.
    only_code = [by_code[-1]]
    assert status("app.errors.NotFound", 404, only_code) == 409
    assert status("app.errors.Gone", 404, only_code) == 500
    # A catch-all handler takes what nothing nearer does.
    catch_all = [{**HANDLERS[1], "exception": "builtins.Exception", "subclasses": []}]
    assert status("builtins.ValueError", None, catch_all) == 409
    # So does a handler registered for status 500, which is the server error's.
    on_500 = [{**HANDLERS[1], "exception": "", "status": 500, "subclasses": []}]
    assert status("builtins.ValueError", None, on_500) == 409


def test_between_two_handled_bases_the_method_resolution_order_decides(tmp_path):
    root, index = _repo(tmp_path)
    (root / "app" / "both.py").write_text(
        "class Left(Exception):\n    pass\n\n\n"
        "class Right(Exception):\n    pass\n\n\n"
        "class Leaf(Left, Right):\n    pass\n\n\n"
        "class Other(Left):\n    pass\n",
        encoding="utf-8",
    )
    index = Index(update(Cache(root, tmp_path / "cache2"), load_config(root)))
    # Left has more loaded subclasses than Right: their number does not decide.
    left = {**HANDLERS[1], "exception": "app.both.Left", "handler": "app.main.left",
            "subclasses": ["app.both.Leaf", "app.both.Other"]}
    right = {**HANDLERS[1], "exception": "app.both.Right", "handler": "app.main.right",
             "subclasses": ["app.both.Leaf"]}
    for handlers in ([left, right], [right, left]):
        found = outcome(index, Trees(root), handlers, "app.both.Leaf", None)
        assert found["handler"] == "app.main.left"


def test_a_handler_of_the_re_exported_class_itself_wins_over_one_of_its_base(tmp_path):
    # `fastapi.HTTPException` as code imports it is the loaded `fastapi.exceptions.HTTPException`:
    # the app's handler for that class answers, not the framework's for the Starlette base.
    root, index = _repo(tmp_path)
    own = {**HANDLERS[1], "exception": "fastapi.exceptions.HTTPException", "subclasses": []}
    for handlers in ([*HANDLERS, own], [own, *HANDLERS]):
        found = outcome(index, Trees(root), handlers, "fastapi.HTTPException", None)
        assert (found["status"], found["handler"]) == (409, "app.main.conflict_handler")
        # A subclass in the repository reaches it through its base outside.
        assert outcome(index, Trees(root), handlers, "app.errors.NotFound", None)["status"] == 409


def test_a_handler_file_outside_the_repository_is_not_read(tmp_path):
    # The routes view's handler file comes from the child that imported the target.
    root, index = _repo(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text(
        "def h(request, exc):\n    return Response(status_code=451)\n", encoding="utf-8"
    )
    handler = {**HANDLERS[1], "handler": "outside.h", "file": "../outside.py"}
    assert outcome(index, Trees(root), [handler], "app.errors.Conflict", None)["status"] is None


def test_names_as_the_running_app_has_them_join_the_index_in_a_nested_project(tmp_path):
    # `apps/web/routes.py` run from `apps/web` is the module `routes`: the routes view names its
    # functions `routes.NAME`, the index `apps.web.routes.NAME`.
    root = tmp_path / "repo"
    (root / "apps" / "web").mkdir(parents=True)
    (root / "apps" / "web" / "routes.py").write_text(
        "class Conflict(Exception):\n    pass\n\n\n"
        "def load():\n    raise Conflict()\n\n\n"
        "def endpoint():\n    return load()\n\n\n"
        "def dep():\n    return load()\n\n\n"
        "def other():\n    return {}\n",
        encoding="utf-8",
    )
    index = Index(update(Cache(root, tmp_path / "cache"), load_config(root)))

    def route(path, handler, deps=()):
        return {**_route(path, handler, deps), "file": "apps/web/routes.py"}

    routes = [
        route("/a", "routes.endpoint"),
        route("/b", "routes.other", ["routes.dep"]),
        route("/d", "routes.other"),
    ]
    found, _ = reaching(index, Trees(root), routes, "apps.web.routes.load")
    assert [(r["path"], via[0]) for r, via in found] == [
        ("/a", "apps.web.routes.endpoint"), ("/b", "apps.web.routes.dep"),
    ]
    # The handled class is loaded under the same shorter name.
    handler = {**HANDLERS[1], "exception": "routes.Conflict", "subclasses": [],
               "handler": "routes.other", "file": "apps/web/routes.py"}
    found = outcome(index, Trees(root), [handler], "apps.web.routes.Conflict", None)
    assert found["handler"] == "routes.other"


def test_an_exception_named_by_the_user_is_read_as_a_raise_of_it_is(tmp_path):
    root, index = _repo(tmp_path)
    assert named(index, "Conflict") == "app.errors.Conflict"
    assert named(index, "ValueError") == "builtins.ValueError"
    assert named(index, "HTTPException") == "HTTPException"
    on_value = [{**HANDLERS[1], "exception": "builtins.ValueError", "subclasses": []}]
    assert outcome(index, Trees(root), on_value, named(index, "ValueError"), None)["status"] == 409
    # A class outside the repository given by its bare name is the loaded class of that name.
    found = outcome(index, Trees(root), HANDLERS, "HTTPException", 418)
    assert (found["status"], found["how"]) == (418, "handler")
