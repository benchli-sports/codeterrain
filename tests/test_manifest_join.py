from __future__ import annotations

import json

import pytest

from codeterrain.config import DEFAULT_MANIFEST_KEYS
from codeterrain.plugins.manifest_join import ManifestError, join, load_rows

KEYS = dict(DEFAULT_MANIFEST_KEYS)


def route(method, path, app="svc.main:app"):
    return {"method": method, "path": path, "app": app}


def row(method, path, access, apps=None):
    out = {"method": method, "path": path, "access": access}
    if apps is not None:
        out["apps"] = apps
    return out


def test_joins_on_method_and_path_and_reports_both_unmatched_sides():
    result = join(
        [route("GET", "/a"), route("POST", "/a"), route("GET", "/only-code")],
        [row("get", "/a", "member"), row("POST", "/a", "manager"), row("GET", "/d", "public")],
        KEYS,
    )
    assert [r["access"] for r in result.rows] == ["member", "manager", None]
    assert [r["path"] for r in result.routes_without_row] == ["/only-code"]
    assert [r["path"] for r in result.rows_without_route] == ["/d"]


def test_a_row_naming_apps_only_applies_to_those_apps():
    rows = [row("GET", "/x", "public", ["svc.oauth"]), row("GET", "/x", "member", ["svc.main"])]
    both = [route("GET", "/x", "svc.main:app"), route("GET", "/x", "svc.oauth:app")]
    result = join(both, rows, KEYS)
    assert [r["access"] for r in result.rows] == ["member", "public"]
    lonely = join([route("GET", "/x", "svc.other:app")], rows, KEYS)
    assert lonely.rows[0]["access"] is None and len(lonely.rows_without_route) == 2


def test_rows_without_an_apps_field_apply_to_every_app():
    result = join([route("GET", "/x", "a:app"), route("GET", "/x", "b:app")],
                  [row("GET", "/x", "public")], KEYS)
    assert [r["access"] for r in result.rows] == ["public", "public"]


def test_custom_key_names_are_honoured():
    keys = {**KEYS, "access": "klass", "rows": "items"}
    doc = {"items": [{"method": "GET", "path": "/z", "klass": "staff"}]}
    assert join([route("GET", "/z")], doc["items"], keys).rows[0]["access"] == "staff"


def test_load_rows_accepts_object_with_rows_key_or_a_bare_list(tmp_path):
    rows = [row("GET", "/a", "public")]
    obj = tmp_path / "o.json"
    obj.write_text(json.dumps({"routes": rows}), encoding="utf-8")
    bare = tmp_path / "b.json"
    bare.write_text(json.dumps(rows), encoding="utf-8")
    assert load_rows(obj, KEYS) == load_rows(bare, KEYS) == rows


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (None, "not readable"),
        ("{nope", "not JSON"),
        ('{"other": []}', "no list under 'routes'"),
        ('{"routes": [{"method": "GET", "path": "/a"}]}', "'access'"),
    ],
)
def test_bad_manifests_are_refused_with_the_reason(tmp_path, content, message):
    path = tmp_path / "m.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    with pytest.raises(ManifestError, match=message):
        load_rows(path, KEYS)


def test_starlette_converters_do_not_split_a_route_from_its_manifest_row():
    result = join(
        [route("GET", "/v/by-slug/{slug:path}"), route("GET", "/v/{id}/x")],
        [row("GET", "/v/by-slug/{slug}", "authenticated"), row("GET", "/v/{id:int}/x", "public")],
        KEYS,
    )
    assert [r["access"] for r in result.rows] == ["authenticated", "public"]
    assert result.routes_without_row == [] and result.rows_without_route == []


@pytest.mark.parametrize("content", ["3", '"text"', "null", "true"])
def test_a_manifest_root_that_is_neither_object_nor_list_is_refused(tmp_path, content):
    path = tmp_path / "m.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ManifestError, match="object or a list"):
        load_rows(path, KEYS)


@pytest.mark.parametrize("field", ["method", "path"])
def test_non_string_method_or_path_in_a_row_is_refused(tmp_path, field):
    bad = {"method": "GET", "path": "/a", "access": "public", field: 7}
    path = tmp_path / "m.json"
    path.write_text(json.dumps([bad]), encoding="utf-8")
    with pytest.raises(ManifestError, match="must be a string"):
        load_rows(path, KEYS)


def test_apps_given_as_a_string_is_one_module_matched_exactly_not_by_substring():
    rows = [row("GET", "/x", "public", "svc.main")]
    result = join(
        [route("GET", "/x", "svc.main:app"), route("GET", "/x", "svc.main_two:app"),
         route("GET", "/x", "svc:app")],
        rows,
        KEYS,
    )
    assert [r["access"] for r in result.rows] == ["public", None, None]


def test_an_empty_apps_list_matches_no_app():
    result = join([route("GET", "/x")], [row("GET", "/x", "public", [])], KEYS)
    assert result.rows[0]["access"] is None
    assert len(result.rows_without_route) == 1


def test_the_first_matching_row_wins_and_the_later_duplicate_stays_unused():
    result = join(
        [route("GET", "/x")],
        [row("GET", "/x", "first"), row("GET", "/x", "second")],
        KEYS,
    )
    assert result.rows[0]["access"] == "first"
    assert [r["access"] for r in result.rows_without_route] == ["second"]


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"apps": 3}, "apps"),
        ({"apps": None}, "apps"),
        ({"apps": {"a": 1}}, "apps"),
        ({"apps": ["api.main", 4]}, "apps"),
        ({"access": 5}, "access"),
        ({"access": ["public"]}, "access"),
        ({"access": None}, "access"),
    ],
    ids=["apps-int", "apps-null", "apps-object", "apps-list-of-mixed", "access-int",
         "access-list", "access-null"],
)
def test_apps_and_access_of_the_wrong_type_are_refused_when_the_manifest_loads(
    tmp_path, extra, message
):
    bad = {"method": "GET", "path": "/a", "access": "public", **extra}
    path = tmp_path / "m.json"
    path.write_text(json.dumps([bad]), encoding="utf-8")
    with pytest.raises(ManifestError, match=message):
        load_rows(path, KEYS)


@pytest.mark.parametrize("apps", ["api.main", ["api.main", "api.admin"], []])
def test_apps_as_a_string_or_a_list_of_strings_loads(tmp_path, apps):
    good = {"method": "GET", "path": "/a", "access": "public", "apps": apps}
    path = tmp_path / "m.json"
    path.write_text(json.dumps([good]), encoding="utf-8")
    assert load_rows(path, KEYS) == [good]
