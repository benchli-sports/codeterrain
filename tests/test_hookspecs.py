from __future__ import annotations

import pluggy
import pytest

from codeterrain import hookspecs
from codeterrain.hookspecs import hookimpl
from codeterrain.plugins import fastapi_routes


class Custom:
    @hookimpl
    def iter_routes(self, app):
        if getattr(app, "kind", "") != "custom":
            return None
        return [{"method": "GET", "path": "/c", "handler": "c.h", "file": "", "line": 1,
                 "dependencies": [], "response_model": None}]


class Unknown:
    kind = "unknown"


class Kind:
    def __init__(self, kind):
        self.kind = kind


@pytest.fixture
def manager():
    manager = pluggy.PluginManager(hookspecs.PROJECT)
    manager.add_hookspecs(hookspecs)
    manager.register(fastapi_routes)
    return manager


def test_iter_routes_is_first_result_so_a_custom_plugin_can_claim_its_apps(manager):
    manager.register(Custom())
    found = manager.hook.iter_routes(app=Kind("custom"))
    assert [r["path"] for r in found] == ["/c"]


def test_iter_routes_yields_none_when_no_plugin_knows_the_app(manager):
    manager.register(Custom())
    assert manager.hook.iter_routes(app=Unknown()) is None


def test_tests_for_is_a_seam_only_no_builtin_implements_it(manager):
    assert manager.hook.tests_for(file="pkg/mod.py") == []
    manager.register(type("T", (), {"tests_for": hookimpl(lambda self, file: [f"t::{file}"])})())
    assert manager.hook.tests_for(file="pkg/mod.py") == [["t::pkg/mod.py"]]


def test_the_core_modules_never_import_extractor_code():
    import ast
    import pathlib

    package = pathlib.Path(hookspecs.__file__).parent
    extractor_modules = {"_pytest_dump_plugin", "_routes_runner", "fastapi_routes"}
    core = [
        p
        for p in package.glob("*.py")
        if p.name not in {"_pytest_dump_plugin.py", "_routes_runner.py", "hookspecs.py"}
    ]
    assert core
    for path in core:
        imported = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[-1])
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.Import):
                imported.update(a.name.split(".")[-1] for a in node.names)
        assert not imported & extractor_modules, path.name
