from __future__ import annotations

import copy

import pytest

from codeterrain.protocol import ORDER_SCHEMA, ROUTE_KEYS, ROUTES_SCHEMA, ProtocolError, check

FIXTURE = {"name": "db", "scope": "session", "file": "tests/conftest.py", "line": 3}
ROUTE = {
    "app": "m:app",
    "method": "GET",
    "path": "/x",
    "handler": "m.x",
    "file": "m.py",
    "line": 1,
    "dependencies": [],
    "response_model": None,
}
VALID = {
    ORDER_SCHEMA: {
        "schema": ORDER_SCHEMA,
        "closures": [[FIXTURE]],
        "nodes": {"tests/t.py::test": 0},
        "errors": 0,
    },
    ROUTES_SCHEMA: {"schema": ROUTES_SCHEMA, "routes": [ROUTE], "errors": []},
}


@pytest.mark.parametrize("schema", [ORDER_SCHEMA, ROUTES_SCHEMA])
def test_valid_documents_pass_through_unchanged(schema):
    assert check(VALID[schema], schema) == VALID[schema]


@pytest.mark.parametrize("schema", [ORDER_SCHEMA, ROUTES_SCHEMA])
@pytest.mark.parametrize("found", ["codeterrain.x/2", "codeterrain.x/0", None, ""])
def test_unknown_schema_version_is_refused_with_both_versions_named(schema, found):
    doc = {**copy.deepcopy(VALID[schema]), "schema": found}
    with pytest.raises(ProtocolError) as err:
        check(doc, schema)
    assert repr(found) in str(err.value) and repr(schema) in str(err.value)
    assert "extractor-protocol.md" in str(err.value)


def test_a_document_for_the_other_view_is_refused():
    with pytest.raises(ProtocolError, match="unsupported extractor schema"):
        check(VALID[ROUTES_SCHEMA], ORDER_SCHEMA)


@pytest.mark.parametrize("payload", [[], "text", None, 3])
def test_non_object_documents_are_refused(payload):
    with pytest.raises(ProtocolError, match="not a JSON object"):
        check(payload, ROUTES_SCHEMA)


@pytest.mark.parametrize(
    ("schema", "mutate", "message"),
    [
        (ORDER_SCHEMA, lambda d: d.pop("closures"), "closures"),
        (ORDER_SCHEMA, lambda d: d.update(nodes=[]), "nodes"),
        (ORDER_SCHEMA, lambda d: d["closures"][0][0].pop("scope"), "each fixture needs"),
        (ORDER_SCHEMA, lambda d: d["nodes"].update(bad=5), "does not exist"),
        (ROUTES_SCHEMA, lambda d: d.pop("routes"), "routes"),
        (ROUTES_SCHEMA, lambda d: d["routes"][0].pop("handler"), "each route needs"),
    ],
)
def test_structural_damage_is_refused(schema, mutate, message):
    doc = copy.deepcopy(VALID[schema])
    mutate(doc)
    with pytest.raises(ProtocolError, match=message):
        check(doc, schema)


ERROR = {"app": "m:app", "error": "Traceback...", "chain": ["ValueError: boom"]}
WITH_ERROR = {**VALID[ROUTES_SCHEMA], "errors": [ERROR]}


def _removals():
    cases = []

    def add(schema, label, remove):
        cases.append(pytest.param(schema, remove, id=f"{schema.split('.')[1][:5]}-{label}"))

    for key in ("schema", "closures", "nodes", "errors"):
        add(ORDER_SCHEMA, key, lambda d, k=key: d.pop(k))
    for key in ("name", "scope", "file", "line"):
        add(ORDER_SCHEMA, f"fixture.{key}", lambda d, k=key: d["closures"][0][0].pop(k))
    for key in ("schema", "routes", "errors"):
        add(ROUTES_SCHEMA, key, lambda d, k=key: d.pop(k))
    for key in ROUTE_KEYS:
        add(ROUTES_SCHEMA, f"route.{key}", lambda d, k=key: d["routes"][0].pop(k))
    for key in ("app", "error"):
        add(ROUTES_SCHEMA, f"error.{key}", lambda d, k=key: d["errors"][0].pop(k))
    return cases


@pytest.mark.parametrize(("schema", "remove"), _removals())
def test_removing_any_required_key_is_refused(schema, remove):
    doc = copy.deepcopy(WITH_ERROR if schema == ROUTES_SCHEMA else VALID[schema])
    remove(doc)
    with pytest.raises(ProtocolError):
        check(doc, schema)


@pytest.mark.parametrize(
    ("schema", "mutate"),
    [
        (ORDER_SCHEMA, lambda d: d.update(errors=True)),
        (ORDER_SCHEMA, lambda d: d.update(errors="0")),
        (ORDER_SCHEMA, lambda d: d.update(closures=[{}])),
        (ORDER_SCHEMA, lambda d: d["closures"][0].append("db")),
        (ORDER_SCHEMA, lambda d: d["closures"][0][0].update(name=3)),
        (ORDER_SCHEMA, lambda d: d["closures"][0][0].update(scope=None)),
        (ORDER_SCHEMA, lambda d: d["closures"][0][0].update(file=["x"])),
        (ORDER_SCHEMA, lambda d: d["closures"][0][0].update(line="3")),
        (ORDER_SCHEMA, lambda d: d["closures"][0][0].update(line=True)),
        (ORDER_SCHEMA, lambda d: d["closures"][0][0].update(line=1.5)),
        (ORDER_SCHEMA, lambda d: d["nodes"].update(t=True)),
        (ORDER_SCHEMA, lambda d: d["nodes"].update(t="0")),
        (ORDER_SCHEMA, lambda d: d["nodes"].update(t=-1)),
        (ORDER_SCHEMA, lambda d: d["nodes"].update(t=1)),
        (ROUTES_SCHEMA, lambda d: d.update(routes={})),
        (ROUTES_SCHEMA, lambda d: d.update(errors={})),
        (ROUTES_SCHEMA, lambda d: d["routes"].append("GET /x")),
        (ROUTES_SCHEMA, lambda d: d["routes"][0].update(app=1)),
        (ROUTES_SCHEMA, lambda d: d["routes"][0].update(method=None)),
        (ROUTES_SCHEMA, lambda d: d["routes"][0].update(path=["/x"])),
        (ROUTES_SCHEMA, lambda d: d["routes"][0].update(handler=2)),
        (ROUTES_SCHEMA, lambda d: d["routes"][0].update(file=None)),
        (ROUTES_SCHEMA, lambda d: d["routes"][0].update(line="1")),
        (ROUTES_SCHEMA, lambda d: d["routes"][0].update(line=False)),
        (ROUTES_SCHEMA, lambda d: d["routes"][0].update(dependencies="dep")),
        (ROUTES_SCHEMA, lambda d: d["routes"][0].update(dependencies=["a", 1])),
        (ROUTES_SCHEMA, lambda d: d["routes"][0].update(response_model=5)),
        (ROUTES_SCHEMA, lambda d: d["routes"][0].update(also_head="yes")),
        (ROUTES_SCHEMA, lambda d: d["errors"].append("boom")),
        (ROUTES_SCHEMA, lambda d: d["errors"][0].update(app=None)),
        (ROUTES_SCHEMA, lambda d: d["errors"][0].update(error=["x"])),
        (ROUTES_SCHEMA, lambda d: d["errors"][0].update(chain="ValueError")),
        (ROUTES_SCHEMA, lambda d: d["errors"][0].update(chain=["ok", 3])),
    ],
)
def test_a_wrong_type_anywhere_is_refused(schema, mutate):
    doc = copy.deepcopy(WITH_ERROR if schema == ROUTES_SCHEMA else VALID[schema])
    mutate(doc)
    with pytest.raises(ProtocolError):
        check(doc, schema)


def test_the_documents_the_extractors_really_write_are_accepted_in_full():
    doc = copy.deepcopy(WITH_ERROR)
    doc["routes"][0]["response_model"] = "list[Item]"
    doc["routes"][0]["dependencies"] = ["m.dep"]
    doc["notes"] = ["warning: x"]
    assert check(doc, ROUTES_SCHEMA) == doc
    order = copy.deepcopy(VALID[ORDER_SCHEMA])
    order["closures"][0][0]["params"] = True
    order["closures"].append([])
    order["nodes"]["tests/t.py::other"] = 1
    assert check(order, ORDER_SCHEMA) == order


@pytest.mark.parametrize(
    ("schema", "mutate", "message"),
    [
        (ORDER_SCHEMA, lambda d: d.update(closures=[[["name", "scope", "file", "line"]]]),
         "closure entry must be an object"),
        (ROUTES_SCHEMA, lambda d: d.update(routes=[list(ROUTE_KEYS)]), "route must be an object"),
        (ROUTES_SCHEMA, lambda d: d.update(errors=[["app", "error"]]), "error must be an object"),
    ],
    ids=["closure-entry", "route", "error"],
)
def test_an_entry_that_is_a_list_of_the_key_names_is_not_an_object(schema, mutate, message):
    doc = copy.deepcopy(VALID[schema])
    mutate(doc)
    with pytest.raises(ProtocolError, match=message):
        check(doc, schema)
