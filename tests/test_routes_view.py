from __future__ import annotations

import pytest

from codeterrain import routes_view
from codeterrain.cache import Cache
from codeterrain.config import Config
from codeterrain.fixtures_order import ExtractError
from codeterrain.protocol import ROUTES_SCHEMA, ProtocolError
from codeterrain.routes_view import extract, joined, select
from codeterrain.sandbox import SandboxResult


@pytest.fixture(scope="module")
def doc(warm_sample):
    repo, cache = warm_sample
    record = Cache(repo, cache).read_view("routes")
    assert record is not None
    return record.data


def test_extract_refuses_a_document_with_another_schema_version(monkeypatch, tmp_path):
    bad = {"schema": "codeterrain.routes/2", "routes": [], "errors": []}
    monkeypatch.setattr(routes_view, "run_python", lambda *a, **k: SandboxResult(0, "", "", bad))
    with pytest.raises(ProtocolError, match="codeterrain.routes/2"):
        extract(Config(root=tmp_path, apps=("m:app",)), tmp_path, "python")


def test_every_route_field_is_redacted_before_it_is_cached(monkeypatch, tmp_path):
    # Security review r2 P3-3: only import errors and notes were redacted.
    monkeypatch.setenv("TENANT_KEY", "CANARY-tenant-key")
    route = {
        "app": "app.main:app", "method": "GET", "path": "/t/CANARY-tenant-key",
        "handler": "app.main.CANARY-tenant-key", "file": "app/CANARY-tenant-key.py", "line": 1,
        "dependencies": ["dep_CANARY-tenant-key"], "response_model": "M_CANARY-tenant-key",
    }
    found = {"schema": ROUTES_SCHEMA, "routes": [route], "errors": []}
    monkeypatch.setattr(routes_view, "run_python", lambda *a, **k: SandboxResult(0, "", "", found))
    config = Config(root=tmp_path, apps=("app.main:app",), pass_env=("TENANT_KEY",))
    assert "CANARY" not in repr(routes_view.extract(config, tmp_path, "python"))


def test_optional_and_plugin_fields_are_redacted_and_keep_their_types(monkeypatch, tmp_path):
    # A plugin can add fields the protocol allows but the
    # redactor did not name, and the secret reached --json and the cached view verbatim.
    monkeypatch.setenv("TOP_SECRET", "CANARY-super-secret-value")
    route = {
        "app": "a:app", "method": "GET", "path": "/", "handler": "a.h", "file": "a.py",
        "line": 3, "dependencies": [], "response_model": None,
        "extra": {"note": "CANARY-super-secret-value", "CANARY-super-secret-value": [1, True]},
    }
    found = {"schema": ROUTES_SCHEMA, "routes": [route], "errors": [],
             "meta": "CANARY-super-secret-value"}
    monkeypatch.setattr(routes_view, "run_python", lambda *a, **k: SandboxResult(0, "", "", found))
    config = Config(root=tmp_path, apps=("a:app",), pass_env=("TOP_SECRET",))
    doc = routes_view.extract(config, tmp_path, "python")
    assert "CANARY" not in repr(doc)
    assert doc["routes"][0]["line"] == 3 and doc["routes"][0]["extra"]["[REDACTED]"] == [1, True]


def test_extract_needs_apps_and_a_document(monkeypatch, tmp_path):
    with pytest.raises(ExtractError, match="no apps configured"):
        extract(Config(root=tmp_path), tmp_path, "python")
    monkeypatch.setattr(
        routes_view, "run_python", lambda *a, **k: SandboxResult(1, "", "ImportError: x", None)
    )
    with pytest.raises(ExtractError, match=r"exit 1\): ImportError: x"):
        extract(Config(root=tmp_path, apps=("m:app",)), tmp_path, "python")


def test_runner_arguments_carry_apps_plugins_and_moto_flag(monkeypatch, tmp_path):
    seen = {}

    def fake(python, args, config, cache_dir, timeout=0):
        seen["args"] = args
        return SandboxResult(0, "", "", {"schema": ROUTES_SCHEMA, "routes": [], "errors": []})

    monkeypatch.setattr(routes_view, "run_python", fake)
    config = Config(root=tmp_path, apps=("a:x", "b:y"), plugins=("p.q",), moto_mock=True)
    extract(config, tmp_path, "python")
    assert seen["args"] == [
        "-m", "codeterrain._routes_runner", "--apps", "a:x", "b:y",
        "--plugins", "p.q", "--moto-mock",
    ]


def test_sample_routes_document(doc):
    assert doc["schema"] == ROUTES_SCHEMA and doc["errors"] == []
    mine = [r for r in doc["routes"] if r["app"] == "sampleapp.main:app"]
    paths = {(r["method"], r["path"]) for r in mine}
    assert ("GET", "/v1/items/{item_id}/parts") in paths and ("GET", "/legacy/ping") in paths
    item = next(r for r in mine if r["path"] == "/v1/items/{item_id}")
    assert item["file"] == "sampleapp/main.py"
    framework = next(r for r in mine if r["path"] == "/docs")
    assert "site-packages" in framework["file"] and "\\" not in framework["file"]


def test_join_adds_access_and_counts_the_unjoined(doc, warm_sample):
    repo, _ = warm_sample
    config = Config(
        root=repo,
        route_manifest=repo / "docs" / "route-access.json",
        manifest_keys=Config(root=repo).manifest_keys,
    )
    result = joined(doc, config)
    access = {(r["method"], r["path"], r["app"]): r["access"] for r in result.rows}
    assert access[("GET", "/v1/items/{item_id}", "sampleapp.main:app")] == "member"
    assert access[("GET", "/health", "sampleapp.other:other")] == "public"
    assert access[("GET", "/v1/items/{item_id}/parts", "sampleapp.main:app")] is None
    assert [r["path"] for r in result.rows_without_route] == ["/v1/removed"]


def test_without_a_manifest_access_is_empty(doc, tmp_path):
    result = joined(doc, Config(root=tmp_path))
    assert {r["access"] for r in result.rows} == {None} and not result.rows_without_route


def test_select_filters_by_text_app_and_access(doc, warm_sample):
    repo, _ = warm_sample
    config = Config(root=repo, route_manifest=repo / "docs" / "route-access.json")
    rows = joined(doc, config).rows
    assert {r["path"] for r in select(rows, "parts")} == {"/v1/items/{item_id}/parts"}
    assert {r["handler"] for r in select(rows, "AUDIT_LOG")} == {"sampleapp.main.audit_log"}
    assert {r["app"] for r in select(rows, app="other")} == {"sampleapp.other:other"}
    assert {r["path"] for r in select(rows, access="STAFF")} == {"/v1/admin/audit"}
