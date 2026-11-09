from __future__ import annotations

import json
import os
import sqlite3
import stat
import time

import pytest

from codeterrain import code_static
from codeterrain.cache import Cache
from codeterrain.cli import main
from codeterrain.code_static import (
    CorruptIndex,
    Index,
    UnsafeIndex,
    db_path,
    module_name,
    parse_source,
    references,
    shape_problems,
    update,
)
from codeterrain.config import load_config

ORDERS = '''\
class Service:
    def __init__(self, repo):
        self.repo = repo

    def place(self, order, *, notify=False):
        return self.helper(order)

    def helper(self, order):
        return order

    @classmethod
    def build(cls, repo):
        return cls(repo)


def create(name, qty=1, *, user):
    return Service(None).place(name)
'''

API = '''\
from app.services import create, register
from app.services import orders
from app.services.orders import Service


def get_user():
    return "u"


def endpoint(user=Depends(get_user)):
    create("x", user=user)
    create("x", 2, 3, user=user)
    orders.create("y")
    Service(None).place("o", notify=True)
    Service.build(None)
    svc = make()
    svc.place("o")
    register(create)
    return user
'''

TESTS = '''\
from unittest import mock

from app.services import orders


def test_create(monkeypatch):
    monkeypatch.setattr(orders, "create", fake)
    with mock.patch("app.services.orders.create"):
        orders.create("z", user="u")
'''


def _repo(tmp_path):
    root = tmp_path / "repo"
    files = {
        "app/__init__.py": "",
        "app/services/__init__.py": (
            "from .orders import create\n\n\ndef register(fn):\n    return fn\n"
        ),
        "app/services/orders.py": ORDERS,
        "app/api.py": API,
        "tests/test_orders.py": TESTS,
        # An installed-packages folder (a deployment bundle): not the repository's own code.
        "bundle/vendored-1.0.dist-info/METADATA": "Name: vendored\n",
        "bundle/vendored/orders.py": "def create():\n    pass\n",
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _index(root, cache_dir, force=False):
    return Index(update(Cache(root, cache_dir), load_config(root), force=force))


def test_definitions_in_bulk_pick_what_definition_picks(tmp_path):
    root = tmp_path / "repo"
    for rel, text in {
        "app/x.py": "def f():\n    pass\n\n\ndef f():\n    return 1\n",
        "src/app/x.py": "def f():\n    pass\n",
        "app/y.py": "class C:\n    def m(self):\n        pass\n",
    }.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    index = _index(root, tmp_path / "cache")
    names = ["app.x.f", "app.y.C", "app.y.C.m", "app.missing"]
    assert index.definitions(names) == {
        n: index.definition(n) for n in names if index.definition(n) is not None
    }
    assert index.definitions(names)["app.x.f"][1]["line"] == 5


def test_module_names_drop_src_and_init():
    assert module_name("src/app/x.py") == "app.x"
    assert module_name("app/x.py") == "app.x"
    assert module_name("app/__init__.py") == "app"
    assert module_name("src/app/x.py", src_package=True) == "src.app.x"
    assert module_name("src/__init__.py", src_package=True) == "src"


def test_a_nested_project_s_own_imports_resolve_from_its_folder(tmp_path):
    # A monorepo app with its own pyproject.toml imports itself from its folder; module names
    # still start at the repository root, so two apps' modules never collide.
    root = tmp_path / "repo"
    _write(root, {
        "apps/api/pyproject.toml": "[project]\nname = 'api'\n",
        "apps/api/src/__init__.py": "",
        "apps/api/src/repositories/__init__.py": "",
        "apps/api/src/repositories/factory.py": (
            "class Factory:\n    @staticmethod\n    def audit():\n        pass\n"
        ),
        "apps/api/src/routes/audit.py": (
            "from src.repositories.factory import Factory\n"
            "def show():\n    Factory.audit()\n"
        ),
        "apps/api/app.py": "def audit(x):\n    pass\n",
        "apps/web/pyproject.toml": "[project]\nname = 'web'\n",
        "apps/web/app.py": "def audit():\n    pass\n",
        "apps/web/run.py": "from app import audit\naudit()\n",
        "tools/run.py": "from apps.api.src.routes.audit import show\nshow()\n",
    })
    index = _index(root, tmp_path / "cache")
    factory = "apps.api.src.repositories.factory.Factory.audit"
    assert index.find("Factory.audit") == [factory]
    calls = references(index, [factory])["calls"]
    assert [(c["file"], c["line"]) for c in calls] == [("apps/api/src/routes/audit.py", 3)]
    web = references(index, ["apps.web.app.audit"])["calls"]
    assert [(c["file"], c["problems"]) for c in web] == [("apps/web/run.py", [])]
    assert references(index, ["apps.api.app.audit"])["calls"] == []
    # A path from the repository root still resolves.
    shown = references(index, ["apps.api.src.routes.audit.show"])["calls"]
    assert [c["file"] for c in shown] == ["tools/run.py"]


def test_src_imported_as_a_namespace_resolves_inside_a_nested_project(tmp_path):
    # `apps/api/src` has no __init__.py, yet the app puts `apps/api` on the path and writes
    # `from src.repositories import factory`, as a namespace package.
    root = tmp_path / "repo"
    _write(root, {
        "apps/api/pyproject.toml": "[project]\nname = 'api'\n",
        "apps/api/src/repositories/factory.py": "def audit():\n    pass\n",
        "apps/api/src/routes/audit.py": (
            "from src.repositories.factory import audit\n"
            "def show():\n    audit()\n"
        ),
        "apps/api/tests/test_audit.py": (
            "from unittest.mock import patch\n"
            "def test_it():\n    patch('src.repositories.factory.audit')\n"
        ),
    })
    index = _index(root, tmp_path / "cache")
    target = "apps.api.src.repositories.factory.audit"
    assert index.find("audit") == [target]
    found = references(index, [target])
    assert [(c["file"], c["line"]) for c in found["calls"]] == [("apps/api/src/routes/audit.py", 3)]
    assert [p["line"] for p in found["patches"]] == [3]


def test_a_re_export_inside_a_nested_project_resolves_from_its_folder(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "apps/api/pyproject.toml": "[project]\nname = 'api'\n",
        "apps/api/src/repositories/__init__.py": "from src.repositories.factory import Factory\n",
        "apps/api/src/repositories/factory.py": (
            "class Factory:\n    @staticmethod\n    def audit():\n        pass\n"
        ),
        "apps/api/tests/test_it.py": (
            "from src.repositories import Factory\ndef test_it():\n    Factory.audit()\n"
        ),
    })
    index = _index(root, tmp_path / "cache")
    calls = references(index, ["apps.api.src.repositories.factory.Factory.audit"])["calls"]
    assert [(c["file"], c["line"]) for c in calls] == [("apps/api/tests/test_it.py", 3)]


def test_a_root_level_re_export_is_known_before_a_nested_project_is_tried(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "core.py": "def audit():\n    pass\n",
        "api/__init__.py": "from core import audit\n",
        "apps/web/pyproject.toml": "[project]\nname = 'web'\n",
        "apps/web/api.py": "def audit(x):\n    pass\n",
        "apps/web/run.py": "from api import audit\naudit()\n",
    })
    index = _index(root, tmp_path / "cache")
    calls = references(index, ["core.audit"])["calls"]
    assert [(c["file"], c["problems"]) for c in calls] == [("apps/web/run.py", [])]
    assert references(index, ["apps.web.api.audit"])["calls"] == []


def test_a_long_import_path_is_looked_up_a_bounded_number_of_times(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    dotted = ".".join(f"m{i}" for i in range(2_000))
    _write(root, {
        "apps/web/pyproject.toml": "[project]\nname = 'web'\n",
        "apps/web/run.py": f"from {dotted} import audit\naudit()\n",
        "lib.py": "def audit():\n    pass\n",
    })
    index = _index(root, tmp_path / "cache")
    looked = []
    real = index._known_depth
    monkeypatch.setattr(index, "_known_depth", lambda name: looked.append(name) or real(name))
    assert index.located("apps/web/run.py", f"{dotted}.audit") == f"{dotted}.audit"
    assert looked == []
    assert len(index._known) == 0
    index.located("apps/web/run.py", "a." * 31 + "b")
    assert len(index._known) <= code_static.MAX_IMPORT_PARTS * 3


def test_a_project_marker_added_later_needs_no_rebuild(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    _write(root, {
        "apps/api/app.py": "def create():\n    pass\n",
        "apps/api/use.py": "from app import create\ncreate()\n",
    })
    cache = tmp_path / "cache"
    assert references(_index(root, cache), ["apps.api.app.create"])["calls"] == []
    _write(root, {"apps/api/pyproject.toml": "[project]\nname = 'api'\n"})
    monkeypatch.setattr(code_static, "_facts", lambda *a: pytest.fail("a file was read again"))
    calls = references(_index(root, cache), ["apps.api.app.create"])["calls"]
    assert [c["file"] for c in calls] == ["apps/api/use.py"]


def test_the_project_root_of_a_file_costs_one_lookup_per_folder():
    class Counting(frozenset):
        looked = 0

        def __contains__(self, item):
            Counting.looked += 1
            return super().__contains__(item)

    roots = Counting({f"apps/app{i}" for i in range(10_000)} | {"apps/api"})
    assert code_static.root_of("apps/api/src/routes/audit.py", roots) == "apps/api"
    assert code_static.root_of("tools/run.py", roots) == ""
    assert Counting.looked <= 5


def test_calls_carry_their_shape_and_constructor_receiver():
    facts = parse_source(API)
    assert facts["error"] is None
    shapes = {(c["line"], c["callee"]): c for c in facts["calls"]}
    second = shapes[(12, "create")]
    assert (second["pos"], second["kw"], second["scope"]) == (3, ["user"], "endpoint")
    placed = next(c for c in facts["calls"] if c["attr"] == "place" and c["recv"])
    assert (placed["callee"], placed["recv"], placed["kw"]) == ("", "Service", ["notify"])
    starred = parse_source("f(*a, **k)\n")["calls"][0]
    assert starred["star"] and starred["dstar"] and starred["pos"] == 0


def test_names_passed_as_values_skip_locals_but_keep_self_methods():
    facts = parse_source(
        "def view(x=Depends(get_user)):\n"
        "    y = 1\n"
        "    run(x, y, helper, mod.func)\n"
        "    lambda z: run(z)\n"
        "class A:\n"
        "    def go(self):\n"
        "        run(self, self.step)\n"
    )
    assert sorted((u["name"], u["within"]) for u in facts["uses"]) == [
        ("get_user", "Depends"), ("helper", "run"), ("mod.func", "run"), ("self.step", "run"),
    ]
    assert next(u for u in facts["uses"] if u["name"] == "self.step")["cls"] == "A"


def test_patches_by_string_object_and_monkeypatch():
    facts = parse_source(TESTS)
    assert [(p["line"], p["target"], p["how"]) for p in facts["patches"]] == [
        (7, "orders.create", "monkeypatch.setattr(obj)"),
        (8, "app.services.orders.create", "patch"),
    ]


def test_patch_targets_passed_by_keyword_are_patches():
    facts = parse_source(
        "from unittest.mock import patch\n"
        "import lib\n"
        "@patch(target='lib.create')\n"
        "def test_a(m):\n"
        "    patch.object(target=lib, attribute='create')\n"
        "def test_b(monkeypatch):\n"
        "    monkeypatch.setattr(target='lib.create', value=None)\n"
        "    monkeypatch.setattr(lib, name='create', value=None)\n"
        "    patch.object(lib, *names)\n"
    )
    assert [(p["line"], p["target"], p["how"]) for p in facts["patches"]] == [
        (3, "lib.create", "patch"),
        (5, "lib.create", "patch.object"),
        (7, "lib.create", "monkeypatch.setattr"),
        (8, "lib.create", "monkeypatch.setattr(obj)"),
    ]


def test_a_renamed_import_of_patch_is_a_patch():
    facts = parse_source(
        "from unittest.mock import patch as mock_patch\n"
        "from mock import patch as legacy\n"
        "import lib\n"
        "def test_it():\n"
        "    with mock_patch('lib.create'):\n"
        "        mock_patch.object(lib, 'create')\n"
        "    legacy('lib.create')\n"
        "    other('lib.create')\n"
    )
    assert [(p["line"], p["target"], p["how"]) for p in facts["patches"]] == [
        (5, "lib.create", "patch"),
        (6, "lib.create", "patch.object"),
        (7, "lib.create", "patch"),
    ]


def test_a_parameter_that_hides_a_renamed_patch_is_not_a_patch():
    facts = parse_source(
        "from unittest.mock import patch as mock_patch\n"
        "def test_param(mock_patch):\n"
        "    mock_patch('lib.create')\n"
        "def test_local():\n"
        "    mock_patch = make()\n"
        "    mock_patch.object(lib, 'create')\n"
        "def test_import():\n"
        "    mock_patch('lib.create')\n"
    )
    assert [(p["line"], p["how"]) for p in facts["patches"]] == [(8, "patch")]


def test_a_renamed_patch_counts_only_where_its_import_binds_the_name():
    facts = parse_source(
        "def test_patch():\n"
        "    from unittest.mock import patch as replace\n"
        "    replace('lib.create')\n"
        "def test_other():\n"
        "    from lib import replace\n"
        "    replace('lib.create')\n"
        "def test_module():\n"
        "    replace('lib.create')\n"
        "class T:\n"
        "    from mock import patch as swap\n"
        "    swap('lib.create')\n"
        "    def test_method(self):\n"
        "        swap('lib.create')\n"
    )
    assert [p["line"] for p in facts["patches"]] == [3, 11]


def test_a_parameter_or_other_import_named_patch_is_not_a_patch():
    facts = parse_source(
        "from unittest.mock import patch\n"
        "def test_param(patch):\n"
        "    patch('lib.create')\n"
        "    patch.object(lib, 'create')\n"
        "def test_other():\n"
        "    from requests import patch\n"
        "    patch('lib.create')\n"
        "def test_import():\n"
        "    patch('lib.create')\n"
    )
    assert [p["line"] for p in facts["patches"]] == [9]
    # With no import of `patch` in sight (a star import, say), the spelling still counts.
    assert len(parse_source("from unittest.mock import *\npatch('lib.create')\n")["patches"]) == 1


def test_a_file_that_does_not_parse_is_recorded_with_its_error():
    facts = parse_source("def broken(:\n")
    assert facts["error"].startswith("SyntaxError") and facts["calls"] == []


def test_a_parse_error_is_kept_to_a_bounded_length():
    # A long `# coding:` name is quoted in the error, once per symlink alias.
    facts = parse_source(b"# coding: " + b"x" * 100_000 + b"\npass\n")
    assert facts["error"] and len(facts["error"]) <= code_static.MAX_ERROR_TEXT


def test_a_path_that_is_not_utf8_is_reported_not_a_crash(tmp_path, monkeypatch):
    # On POSIX a file name byte that is not UTF-8 reaches the index as a surrogate
    # escape, which SQLite cannot bind.
    root = _repo(tmp_path)
    real_scan = Cache.scan

    def scan(self, *args, **kwargs):
        found = real_scan(self, *args, **kwargs)
        return {**found, "app/bad-\udcff.py": "0" * 40} if ".py" in args[1] else found

    monkeypatch.setattr(Cache, "scan", scan)
    db = update(Cache(root, tmp_path / "cache"), load_config(root))
    errors = code_static.summary(db)["errors"]
    assert errors == [
        {"file": "app/bad-\\udcff.py", "error": "path is not valid UTF-8; not indexed"}
    ]
    assert Index(db).find("create") == ["app.services.orders.create"]
    # the next update finds the same escaped row and reads nothing again
    db.close()
    db = update(Cache(root, tmp_path / "cache"), load_config(root))
    assert code_static.summary(db)["errors"] == errors
    db.close()


def test_definitions_follow_re_exports_and_skip_installed_packages(tmp_path):
    index = _index(_repo(tmp_path), tmp_path / "cache")
    assert index.find("app.services.create") == ["app.services.orders.create"]
    assert index.find("create") == ["app.services.orders.create"]
    assert index.find("Service.place") == ["app.services.orders.Service.place"]
    assert index.definition("bundle.vendored.orders.create") is None
    rel, d = index.definition("app.services.orders.create")
    assert (rel, d["kind"], d["pos"], d["kwreq"], d["defaults"]) == (
        "app/services/orders.py", "function", ["name", "qty"], ["user"], 1,
    )


def test_references_resolve_calls_report_shape_problems_and_patches(tmp_path):
    index = _index(_repo(tmp_path), tmp_path / "cache")
    found = references(index, ["app.services.orders.create"])
    calls = {(c["file"], c["line"]): c["problems"] for c in found["calls"]}
    assert calls == {
        ("app/api.py", 11): [],
        ("app/api.py", 12): ["1 positional argument(s) too many; keyword-only: user"],
        ("app/api.py", 13): ["missing keyword-only: user"],
        ("tests/test_orders.py", 9): [],
    }
    assert [(u["file"], u["line"], u["within"]) for u in found["uses"]] == [
        ("app/api.py", 18, "register")
    ]
    assert [(p["line"], p["how"], p["test"]) for p in found["patches"]] == [
        (7, "monkeypatch.setattr(obj)", True), (8, "patch", True),
    ]


def test_methods_on_instances_and_classmethods_bind_their_first_parameter(tmp_path):
    index = _index(_repo(tmp_path), tmp_path / "cache")
    place = references(index, ["app.services.orders.Service.place"])
    assert sorted((c["file"], c["line"], tuple(c["problems"])) for c in place["calls"]) == [
        ("app/api.py", 14, ()), ("app/services/orders.py", 17, ()),
    ]
    # `svc = make()`: the receiver's type is unknown, so the call is a by-name candidate only.
    assert [(c["line"], c["callee"]) for c in place["candidates"]] == [(17, "svc.place")]
    build = references(index, ["app.services.orders.Service.build"])
    assert [(c["line"], c["problems"]) for c in build["calls"]] == [(15, [])]
    helper = references(index, ["app.services.orders.Service.helper"])
    assert [(c["line"], c["problems"]) for c in helper["calls"]] == [(6, [])]


def test_shape_problems_leave_star_calls_and_kwargs_definitions_alone():
    definition = {
        "kind": "function", "pos": ["a"], "posonly": 0, "kwonly": [], "kwreq": [], "defaults": 0,
        "star": False, "dstar": True,
    }
    call = {"pos": 0, "kw": ["anything"], "star": False, "dstar": False}
    assert shape_problems(definition, call, False) == ["1 required positional argument(s) missing"]
    assert shape_problems(definition, {**call, "star": True}, False) == []
    assert shape_problems({**definition, "kind": "class"}, call, False) == []


def test_only_changed_files_are_read_again(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _index(root, tmp_path / "cache")
    seen = []
    real = code_static.parse_source
    monkeypatch.setattr(code_static, "parse_source", lambda text: seen.append(text) or real(text))
    index = _index(root, tmp_path / "cache")
    assert seen == []
    (root / "app" / "api.py").write_text("from app.services import create\ncreate()\n", "utf-8")
    (root / "tests" / "test_orders.py").unlink()
    index = _index(root, tmp_path / "cache")
    assert len(seen) == 1
    found = references(index, ["app.services.orders.create"])
    assert [(c["file"], c["line"]) for c in found["calls"]] == [("app/api.py", 2)]
    assert found["patches"] == []


def test_an_index_of_another_format_is_rebuilt(tmp_path):
    root = _repo(tmp_path)
    index = _index(root, tmp_path / "cache")
    index.db.execute("UPDATE meta SET value = 'code-0' WHERE key = 'format'")
    index.db.execute("DELETE FROM defs")
    index.db.close()
    assert _index(root, tmp_path / "cache").find("create") == ["app.services.orders.create"]


def test_a_row_this_version_did_not_write_is_refused(tmp_path, capsys):
    root = _repo(tmp_path)
    index = _index(root, tmp_path / "cache")
    index.db.execute("UPDATE calls SET json = '[1, \"x\"]' WHERE attr = 'create'")
    with pytest.raises(CorruptIndex):
        references(index, ["app.services.orders.create"])
    index.db.close()
    argv = ["--root", str(root), "--cache-dir", str(tmp_path / "cache")]
    assert main(["refs", "app.services.orders.create", *argv]) == 1
    assert "did not write" in capsys.readouterr().err
    assert main(["refresh", "code", *argv]) == 0
    assert main(["refs", "app.services.orders.create", *argv]) == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_the_index_is_private_to_the_user(tmp_path):
    root = _repo(tmp_path)
    _index(root, tmp_path / "cache").db.close()
    assert stat.S_IMODE(db_path(Cache(root, tmp_path / "cache")).stat().st_mode) == 0o600


def test_terrain_def_and_refs(tmp_path, capsys):
    root = _repo(tmp_path)
    argv = ["--root", str(root), "--cache-dir", str(tmp_path / "cache")]
    assert main(["def", "create", *argv]) == 0
    out = capsys.readouterr().out
    assert "app.services.orders.create" in out and "app/services/orders.py:16" in out
    assert main(["refs", "create", *argv]) == 0
    out = capsys.readouterr().out
    assert (
        "4 calls (3 in code, 1 in tests), 2 with a shape problem, 1 used as a value, 2 patched"
        in out
    )
    assert "missing keyword-only: user" in out and "passed to register" in out
    assert main(["refs", "Service.place", "--tests", "--json", *argv]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["view"] == "refs" and doc["rows"] == [] and doc["candidates"] == []
    assert doc["candidate_count"] == 0
    assert main(["refs", "Service.place", "--by-name", "--json", *argv]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert [c["callee"] for c in doc["candidates"]] == ["svc.place"]


def test_an_unknown_name_suggests_similar_definitions(tmp_path, capsys):
    root = _repo(tmp_path)
    argv = ["--root", str(root), "--cache-dir", str(tmp_path / "cache")]
    assert main(["def", "creat", *argv]) == 1
    assert "app.services.orders.create" in capsys.readouterr().err


def test_a_src_folder_that_is_a_package_keeps_its_name(tmp_path):
    root = tmp_path / "repo"
    (root / "src" / "app").mkdir(parents=True)
    (root / "src" / "app" / "x.py").write_text("def run():\n    pass\n", encoding="utf-8")
    (root / "main.py").write_text("from app.x import run\nrun()\n", encoding="utf-8")
    assert _index(root, tmp_path / "cache").find("run") == ["app.x.run"]
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "main.py").write_text("from src.app.x import run\nrun()\n", encoding="utf-8")
    index = _index(root, tmp_path / "cache")
    assert index.find("run") == ["src.app.x.run"]
    assert [c["file"] for c in references(index, ["src.app.x.run"])["calls"]] == ["main.py"]


def test_the_index_is_a_sqlite_database_in_the_cache_dir(tmp_path):
    root = _repo(tmp_path)
    _index(root, tmp_path / "cache").db.close()
    path = db_path(Cache(root, tmp_path / "cache"))
    assert path.parent.parent == tmp_path / "cache" and path.parent.name.startswith("code-")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 5


def _write(root, files):
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def test_calls_through_an_import_under_another_name_are_references(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def create(x):\n    pass\n",
        "use.py": "from lib import create as renamed\nrenamed(1)\nobj.renamed()\n",
        "pkg/__init__.py": "from .core import run as start\n",
        "pkg/core.py": "def run():\n    pass\n",
        "main.py": "from pkg import start as go\ngo()\n",
    })
    index = _index(root, tmp_path / "cache")
    found = references(index, ["lib.create"])
    assert [(c["file"], c["line"]) for c in found["calls"]] == [("use.py", 2)]
    # `obj.renamed()` is not a by-name candidate for `create`: only the alias's file binds it.
    assert found["candidates"] == []
    run = references(index, ["pkg.core.run"])
    assert [(c["file"], c["line"]) for c in run["calls"]] == [("main.py", 2)]


def test_a_parameter_or_local_hides_an_imported_name(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def create(x):\n    pass\n",
        "use.py": (
            "from lib import create\n"
            "def invoke(create):\n"
            "    create(1)\n"
            "def other():\n"
            "    create = make()\n"
            "    create.run()\n"
            "    create(3)\n"
            "create(2)\n"
        ),
    })
    found = references(_index(root, tmp_path / "cache"), ["lib.create"])
    assert [(c["file"], c["line"]) for c in found["calls"]] == [("use.py", 8)]
    assert found["candidates"] == []


def test_an_editable_install_marker_keeps_the_source_tree(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "src/app.egg-info/PKG-INFO": "Name: app\n",
        "src/app/x.py": "def run():\n    pass\n",
        # An egg-info with no source package beside it: an installed dependency.
        "bundle/dep.egg-info/PKG-INFO": "Name: dep\n",
        "bundle/other/x.py": "def run():\n    pass\n",
    })
    assert _index(root, tmp_path / "cache").find("run") == ["app.x.run"]


def test_an_editable_install_whose_distribution_and_package_names_differ_is_kept(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "src/widget_distribution.egg-info/PKG-INFO": "Name: widget-distribution\n",
        "src/widget_distribution.egg-info/top_level.txt": "widget\n",
        "src/widget/__init__.py": "def create(x):\n    pass\n",
        # An egg-info recording the files an install wrote: installed, whatever sits beside it.
        "bundle/dep.egg-info/PKG-INFO": "Name: dep\n",
        "bundle/dep.egg-info/installed-files.txt": "../dep/__init__.py\n",
        "bundle/dep/__init__.py": "def create(x):\n    pass\n",
    })
    assert _index(root, tmp_path / "cache").find("create") == ["widget.create"]


@pytest.mark.skipif(os.name == "nt", reason="needs privilege")
def test_package_metadata_is_never_read_through_a_link(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("widget\n")
    root = tmp_path / "repo"
    _write(root, {
        "src/widget_distribution.egg-info/PKG-INFO": "Name: widget-distribution\n",
        "src/widget/__init__.py": "def create(x):\n    pass\n",
    })
    os.symlink(outside, root / "src/widget_distribution.egg-info/top_level.txt")
    # The link out of the checkout is not followed, so nothing names `widget` as the package.
    assert _index(root, tmp_path / "cache").find("create") == []


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a FIFO")
def test_package_metadata_that_is_not_a_regular_file_is_not_read(tmp_path):
    egg_info = tmp_path / "widget.egg-info"
    egg_info.mkdir()
    os.mkfifo(egg_info / "top_level.txt")  # opening it to read would block
    assert code_static._top_level(egg_info) == set()
    os.mkfifo(egg_info / "installed-files.txt")
    assert not code_static._regular(egg_info, "installed-files.txt")


def test_changing_test_paths_reclassifies_files_already_read(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def create(x):\n    pass\n",
        "checks/probe.py": "from lib import create\ncreate(1)\n",
    })
    found = references(_index(root, tmp_path / "cache"), ["lib.create"])
    assert [c["test"] for c in found["calls"]] == [False]
    (root / "pyproject.toml").write_text(
        '[tool.codeterrain]\ntest_paths = ["checks"]\n', encoding="utf-8"
    )
    found = references(_index(root, tmp_path / "cache"), ["lib.create"])
    assert [c["test"] for c in found["calls"]] == [True]


def test_an_import_inside_a_function_binds_only_there(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def create(x):\n    pass\n",
        "other.py": "def create(x, y):\n    pass\n",
        "use.py": (
            "def one():\n    from lib import create\n    create(1)\n"
            "def two():\n    from other import create\n    create(1, 2)\n"
            "def three():\n    from lib import create\n    def inner():\n        create(2)\n"
            "class Box:\n    from other import create\n    def m(self):\n        create(1)\n"
        ),
    })
    index = _index(root, tmp_path / "cache")
    lib = references(index, ["lib.create"])
    assert [(c["line"], c["problems"]) for c in lib["calls"]] == [(3, []), (10, [])]
    # A class-body import is not visible in the class's methods.
    other = references(index, ["other.create"])
    assert [(c["line"], c["problems"]) for c in other["calls"]] == [(6, [])]


def test_a_positional_only_parameter_is_not_filled_by_keyword():
    facts = parse_source("def f(a, /, b=1, **kw):\n    pass\n")
    (d,) = facts["defs"]
    assert (d["sig"], d["posonly"]) == ("(a, /, b=…, **kw)", 1)
    definition = {**d, "kind": "function"}
    call = {"pos": 0, "kw": ["a"], "star": False, "dstar": False}
    assert shape_problems(definition, call, False) == ["1 required positional argument(s) missing"]
    assert shape_problems({**definition, "dstar": False}, call, False) == [
        "positional-only passed by keyword: a", "1 required positional argument(s) missing",
    ]


def test_a_keyword_for_a_parameter_given_positionally_does_not_fill_another():
    definition = {
        "kind": "function", "pos": ["a", "b"], "posonly": 0, "kwonly": [], "kwreq": [],
        "defaults": 0, "star": False, "dstar": False,
    }
    call = {"pos": 1, "kw": ["a"], "star": False, "dstar": False}
    assert shape_problems(definition, call, False) == [
        "multiple values for: a", "1 required positional argument(s) missing",
    ]
    assert shape_problems(definition, {**call, "kw": ["b"]}, False) == []


def _victim_db(path):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE files (secret TEXT)")
    db.execute("INSERT INTO files VALUES ('kept')")
    db.commit()
    db.close()


@pytest.mark.parametrize(
    "link",
    [
        pytest.param(
            os.symlink, marks=pytest.mark.skipif(os.name == "nt", reason="needs privilege")
        ),
        os.link,
    ],
)
def test_a_linked_index_is_refused_and_its_target_left_alone(tmp_path, capsys, link):
    root = _repo(tmp_path)
    cache_dir = tmp_path / "cache"
    victim = tmp_path / "victim.sqlite3"
    _victim_db(victim)
    path = db_path(Cache(root, cache_dir))
    path.parent.mkdir(parents=True)
    link(victim, path)
    with pytest.raises(UnsafeIndex):
        _index(root, cache_dir)
    assert main(["def", "create", "--root", str(root), "--cache-dir", str(cache_dir)]) == 1
    assert "--cache-dir" in capsys.readouterr().err
    assert sqlite3.connect(victim).execute("SELECT secret FROM files").fetchall() == [("kept",)]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_an_index_in_a_folder_others_can_change_is_refused(tmp_path):
    root = _repo(tmp_path)
    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o777)
    with pytest.raises(UnsafeIndex, match="another user can change"):
        _index(root, shared / "cache")
    os.chmod(shared, 0o1777)  # sticky, as /tmp: others cannot swap the user's entries
    assert _index(root, shared / "cache").find("create") == ["app.services.orders.create"]


def test_a_nested_scope_does_not_hide_an_imported_name(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def create(x):\n    pass\n",
        "use.py": (
            "from lib import create\n"
            "def outer():\n"
            "    def inner(create):\n        pass\n"
            "    [create for create in range(2)]\n"
            "    create(1)\n"
            "    return lambda create: create\n"
        ),
    })
    found = references(_index(root, tmp_path / "cache"), ["lib.create"])
    assert [(c["file"], c["line"]) for c in found["calls"]] == [("use.py", 6)]


def test_local_names_are_collected_without_rewalking_nested_scopes(monkeypatch):
    import ast

    source = "f = " + "lambda: " * 60 + "[" + ", ".join(f"x{i}" for i in range(2000)) + "]\n"
    nodes = sum(1 for _ in ast.walk(ast.parse(source)))
    real, seen = ast.iter_child_nodes, []
    monkeypatch.setattr(ast, "iter_child_nodes", lambda node: seen.append(1) or real(node))
    assert parse_source(source)["error"] is None
    assert len(seen) < 3 * nodes


def test_a_member_of_a_re_exported_module_resolves(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "pkg/__init__.py": "from . import implementation as api\n",
        "pkg/implementation.py": (
            "class Service:\n    @staticmethod\n    def create(x):\n        pass\n"
        ),
        "main.py": "from pkg import api\napi.Service.create(1)\n",
    })
    found = references(_index(root, tmp_path / "cache"), ["pkg.implementation.Service.create"])
    assert [(c["file"], c["line"], c["problems"]) for c in found["calls"]] == [("main.py", 2, [])]


def test_a_patch_of_a_renamed_export_is_a_patch_site(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def create(x):\n    pass\n",
        "pkg/__init__.py": "from lib import create as renamed\n",
        "tests/test_it.py": (
            "from unittest import mock\n\ndef test_it():\n"
            "    with mock.patch(\"pkg.renamed\"):\n        pass\n"
        ),
    })
    found = references(_index(root, tmp_path / "cache"), ["lib.create"])
    assert [(p["file"], p["line"]) for p in found["patches"]] == [("tests/test_it.py", 4)]


def test_files_are_never_read_whole(tmp_path, monkeypatch):
    from pathlib import Path

    root = tmp_path / "repo"
    _write(root, {"small.py": "def fits():\n    pass\n", "big.py": "def huge():\n    pass\n" * 9})
    monkeypatch.setattr(code_static, "MAX_FILE_BYTES", 100)

    real = Path.read_bytes

    def whole(self):
        assert self.suffix != ".py", f"read whole: {self}"
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", whole)
    index = _index(root, tmp_path / "cache")
    assert index.find("fits") == ["small.fits"]
    assert index.find("huge") == []


def test_comprehension_targets_and_match_captures_hide_an_imported_name(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def create(x):\n    pass\n",
        "use.py": (
            "from lib import create\n"
            "[create(1, 2) for create in callbacks]\n"
            "[x for x in create(1)]\n"
            "def f(event):\n"
            "    match event:\n"
            "        case {\"callback\": create}:\n"
            "            create(1, 2)\n"
        ),
    })
    found = references(_index(root, tmp_path / "cache"), ["lib.create"])
    # Only the first iterable, which runs outside the comprehension, calls the import.
    assert [(c["file"], c["line"]) for c in found["calls"]] == [("use.py", 3)]


def test_a_lambda_default_runs_outside_the_lambda(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def create(x):\n    pass\n",
        "use.py": "from lib import create\nf = lambda create=create(1): create(2)\n",
    })
    found = references(_index(root, tmp_path / "cache"), ["lib.create"])
    assert [(c["line"], c["shape"]) for c in found["calls"]] == [(2, "1 positional")]


@pytest.mark.skipif(os.name == "nt", reason="POSIX links")
def test_a_link_that_wins_the_race_to_create_the_index_is_refused(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    victim = tmp_path / "victim.sqlite3"
    _victim_db(victim)

    def race(path, data):
        os.symlink(victim, path)  # another user creates the name first
        raise FileExistsError(path)

    monkeypatch.setattr(code_static, "_create_private", race)
    with pytest.raises(UnsafeIndex):
        _index(root, tmp_path / "cache")
    assert sqlite3.connect(victim).execute("SELECT secret FROM files").fetchall() == [("kept",)]


def test_a_dependency_named_in_an_annotation_is_a_use(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "deps.py": "def get_user():\n    pass\n",
        "api.py": (
            "from typing import Annotated\nfrom fastapi import Depends\nfrom deps import get_user\n"
            "def endpoint(user: Annotated[str, Depends(get_user)]) -> None:\n    pass\n"
        ),
    })
    found = references(_index(root, tmp_path / "cache"), ["deps.get_user"])
    assert [(u["file"], u["line"], u["within"]) for u in found["uses"]] == [
        ("api.py", 4, "Depends")
    ]


def test_a_method_on_a_module_level_variable_is_a_by_name_candidate(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "class Store:\n    def save(self, x):\n        pass\n",
        "use.py": "from factory import make\nsvc = make()\nsvc.save(1)\n",
    })
    found = references(_index(root, tmp_path / "cache"), ["lib.Store.save"])
    assert found["calls"] == []
    assert [(c["file"], c["line"]) for c in found["candidates"]] == [("use.py", 3)]


def test_the_implementation_wins_over_its_overload_stubs(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": (
            "from typing import overload\n"
            "@overload\ndef parse(x): ...\n"
            "@overload\ndef parse(x, y): ...\n"
            "def parse(x, y=0):\n    pass\n"
        ),
        "use.py": "from lib import parse\nparse('x', 1)\n",
    })
    index = _index(root, tmp_path / "cache")
    rel, d = index.definition("lib.parse")
    assert (d["line"], d["sig"]) == (6, "(x, y=…)")
    found = references(index, ["lib.parse"])
    assert [(c["line"], c["problems"]) for c in found["calls"]] == [(2, [])]


def test_an_inner_import_wins_over_an_outer_local_and_a_walrus_binds_outside(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def create(x):\n    pass\n",
        "use.py": (
            "from lib import create\n"
            "def outer(create):\n"
            "    def inner():\n"
            "        from lib import create\n"
            "        create(1)\n"
            "def other(callbacks):\n"
            "    values = [(create := cb) for cb in callbacks]\n"
            "    create(1, 2)\n"
        ),
    })
    found = references(_index(root, tmp_path / "cache"), ["lib.create"])
    assert [(c["file"], c["line"]) for c in found["calls"]] == [("use.py", 5)]


def test_refs_keeps_at_most_the_limit_and_counts_the_rest(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def f():\n    pass\n",
        "use.py": "from lib import f\n" + "f()\n" * 5,
        "tests/test_use.py": "from lib import f\nf()\n",
    })
    index = _index(root, tmp_path / "cache")
    rows = index.calls_named({"f"})
    assert not isinstance(rows, list)  # decoded one at a time, never all at once
    found = references(index, ["lib.f"], limit=2)
    assert len(found["calls"]) == 2 and found["truncated"]
    assert (found["counts"]["calls"], found["counts"]["calls_in_tests"]) == (6, 1)
    code_only = references(index, ["lib.f"], keep=lambda row: not row["test"], limit=2)
    assert code_only["counts"]["calls"] == 5


def test_an_import_after_a_definition_of_the_same_name_wins(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def create(x):\n    pass\n",
        "use.py": "def create(x, y):\n    pass\nfrom lib import create\ncreate(1)\n",
        "main.py": "from use import create\ncreate(2)\n",
    })
    found = references(_index(root, tmp_path / "cache"), ["lib.create"])
    assert [(c["file"], c["line"], c["problems"]) for c in found["calls"]] == [
        ("main.py", 2, []), ("use.py", 4, []),
    ]


def test_a_name_shared_by_many_definitions_is_cut_with_a_count(tmp_path, capsys):
    root = tmp_path / "repo"
    _write(root, {f"m{i:03}.py": "class C:\n    def f(self):\n        pass\n" for i in range(105)})
    index = _index(root, tmp_path / "cache")
    found = index.find("f")
    assert len(found) == code_static.MAX_MATCHES and found[0] == "m000.C.f"
    assert index.count_matches("f") == 105
    index.db.close()
    assert main(["def", "f", "--root", str(root), "--cache-dir", str(tmp_path / "cache")]) == 0
    assert "105 definitions match f; the first 100 are used" in capsys.readouterr().err


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_no_one_else_can_plant_files_beside_the_index(tmp_path):
    root = _repo(tmp_path)
    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o1777)  # an accepted sticky cache, as /tmp
    _index(root, shared / "cache").db.close()
    own = db_path(Cache(root, shared / "cache")).parent
    assert stat.S_IMODE(own.stat().st_mode) == 0o700  # SQLite's journal is created here
    os.chmod(own, 0o1777)  # sticky is not enough here: others could plant the journal
    with pytest.raises(UnsafeIndex, match="another user can change"):
        _index(root, shared / "cache")


def test_a_bom_or_a_coding_line_is_honoured(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "bom.py").write_bytes(b"\xef\xbb\xbfdef with_bom(x):\n    pass\n")
    (root / "latin.py").write_bytes(
        b"# -*- coding: latin-1 -*-\ndef caf\xe9(x):\n    '''\xe9t\xe9'''\n"
    )
    index = _index(root, tmp_path / "cache")
    assert index.find("with_bom") == ["bom.with_bom"]
    rel, d = index.definition("latin.café")
    assert d["doc"] == "été"


def test_a_class_body_call_means_the_class_s_own_definition(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def create(x):\n    pass\n",
        "use.py": (
            "from lib import create\n"
            "class C:\n"
            "    def create(x, y):\n        pass\n"
            "    create(1, 2)\n"
            "    def m(self):\n        create(3)\n"
        ),
    })
    index = _index(root, tmp_path / "cache")
    # A method does not see the class body's names: `create(3)` is the import.
    assert [c["line"] for c in references(index, ["lib.create"])["calls"]] == [7]
    own = references(index, ["use.C.create"])
    assert [(c["line"], c["problems"]) for c in own["calls"]] == [(5, [])]


def test_an_object_patch_through_a_parameter_is_not_the_import(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def create(x):\n    pass\n",
        "tests/test_it.py": (
            "import lib\nfrom unittest.mock import patch\n"
            "def test_param(lib):\n    patch.object(lib, 'create')\n"
            "def test_import():\n    patch.object(lib, 'create')\n"
        ),
    })
    found = references(_index(root, tmp_path / "cache"), ["lib.create"])
    assert [p["line"] for p in found["patches"]] == [6]


def test_a_patched_line_is_looked_up_without_collecting_every_patch(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def create(x):\n    pass\n",
        "tests/test_it.py": (
            "import lib\nfrom lib import create\n"
            "def test_it(monkeypatch):\n"
            "    monkeypatch.setattr(lib, 'create', create)\n"
            "    register(create)\n"
        ),
    })
    index = _index(root, tmp_path / "cache")
    assert index.patched_at("tests/test_it.py", 4, {"create"}, {"lib.create"})
    assert not index.patched_at("tests/test_it.py", 5, {"create"}, {"lib.create"})
    found = references(index, ["lib.create"])
    assert [p["line"] for p in found["patches"]] == [4]
    assert [u["line"] for u in found["uses"]] == [5]


def test_an_inherited_method_called_on_self_is_a_by_name_candidate(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": (
            "class Base:\n    def save(self, x):\n        pass\n"
            "class Child(Base):\n    def run(self):\n        self.save(1)\n"
        ),
    })
    found = references(_index(root, tmp_path / "cache"), ["lib.Base.save"])
    assert found["calls"] == []
    assert [(c["line"], c["callee"]) for c in found["candidates"]] == [(6, "self.save")]


def test_an_inherited_method_called_on_a_constructed_instance_is_a_by_name_candidate(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": (
            "class Base:\n    def save(self, x):\n        pass\n"
            "class Child(Base):\n    pass\n"
            "Child().save(1)\n"
        ),
    })
    found = references(_index(root, tmp_path / "cache"), ["lib.Base.save"])
    assert found["calls"] == []
    assert [c["line"] for c in found["candidates"]] == [6]


def test_an_inherited_member_called_on_a_class_is_a_by_name_candidate(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": (
            "class Base:\n    @classmethod\n    def build(cls, x):\n        pass\n"
            "class Child(Base):\n    pass\n"
        ),
        "use.py": "import lib\nfrom lib import Child\nChild.build(1)\nlib.Child.build(2)\n",
    })
    found = references(_index(root, tmp_path / "cache"), ["lib.Base.build"])
    assert found["calls"] == []
    assert [c["line"] for c in found["candidates"]] == [3, 4]


def test_a_global_declaration_means_the_module_binding(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def create(x):\n    pass\n",
        "other.py": "def create(x):\n    pass\n",
        "use.py": (
            "from lib import create\n"
            "def outer(create):\n"
            "    def inner():\n        global create\n        create(1)\n"
            "def scoped():\n"
            "    from other import create\n"
            "    def inner():\n        global create\n        create(2)\n"
        ),
    })
    index = _index(root, tmp_path / "cache")
    assert [c["line"] for c in references(index, ["lib.create"])["calls"]] == [5, 10]
    assert references(index, ["other.create"])["calls"] == []


def test_a_patched_line_reads_only_that_line(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    lines = "".join(f"    monkeypatch.setattr(lib, 'create', fake{i})\n" for i in range(50))
    _write(root, {
        "lib.py": "def create(x):\n    pass\n",
        "tests/test_it.py": f"import lib\ndef test_it(monkeypatch):\n{lines}",
    })
    index = _index(root, tmp_path / "cache")
    decoded = []
    real = code_static._row

    def row(text, types):
        decoded.append(types is code_static.PATCH_TYPES)
        return real(text, types)

    monkeypatch.setattr(code_static, "_row", row)
    assert index.patched_at("tests/test_it.py", 30, {"create"}, {"lib.create"})
    assert decoded.count(True) == 1  # the one patch row on that line


def test_a_scoped_binding_is_found_without_scanning_every_import(tmp_path):
    root = tmp_path / "repo"
    funcs = "".join(
        f"def f{i}():\n    from lib import create as c{i}\n    def g():\n        c{i}(1)\n"
        for i in range(200)
    )
    _write(root, {"lib.py": "def create(x):\n    pass\n", "use.py": funcs})
    index = _index(root, tmp_path / "cache")
    assert len(references(index, ["lib.create"])["calls"]) == 200

    index._binds.clear()
    assert index.binding("use.py", "f150.g", "c150") == "lib.create"
    # Only the name's rows in the scope and the scopes enclosing it are read and kept.
    assert index._binds == {
        ("use.py", "f150.g", "c150", 0): {"f150": (True, "lib.create", 602)}
    }


def test_refs_keeps_no_more_bindings_than_it_looks_up(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    unrelated = "".join(f"import m{i}\n" for i in range(300))
    _write(root, {
        "lib.py": "def create(x):\n    pass\n",
        **{f"use{n}.py": unrelated + "from lib import create\ncreate(1)\n" for n in range(5)},
    })
    index = _index(root, tmp_path / "cache")
    assert len(references(index, ["lib.create"])["calls"]) == 5
    assert sum(len(found) for found in index._binds.values()) <= 5
    monkeypatch.setattr(code_static, "MAX_CACHED", 2)
    index._binds.clear()
    assert len(references(index, ["lib.create"])["calls"]) == 5
    assert len(index._binds) <= 2


def test_module_level_code_means_the_binding_of_its_own_moment(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def create(x):\n    pass\n",
        "use.py": (
            "from lib import create\n"
            "create = create(1)\n"
            "create(2)\n"
        ),
        "wrap.py": (
            "from lib import create\n"
            "create = cache(create)\n"
            "class C:\n    value = create(3)\n"
        ),
        "later.py": (
            "create(4)\n"
            "from lib import create\n"
            "def run():\n    create(5)\n"
        ),
    })
    index = _index(root, tmp_path / "cache")
    found = references(index, ["lib.create"])
    assert [(c["file"], c["line"]) for c in found["calls"]] == [("later.py", 4), ("use.py", 2)]
    assert [(u["file"], u["line"]) for u in found["uses"]] == [("wrap.py", 2)]
    assert [c["line"] for c in references(index, ["use.create"])["calls"]] == [3]
    assert [c["line"] for c in references(index, ["wrap.create"])["calls"]] == [4]


def test_a_module_level_loop_or_with_target_replaces_an_import(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def create(x):\n    pass\n",
        "use.py": (
            "from lib import create\n"
            "for create in callbacks:\n"
            "    create(1, 2)\n"
            "async def main():\n    pass\n"
            "with opener() as (create, other):\n"
            "    create(3)\n"
        ),
    })
    index = _index(root, tmp_path / "cache")
    assert references(index, ["lib.create"])["calls"] == []
    assert [c["line"] for c in references(index, ["use.create"])["calls"]] == [3, 7]
    assert index.definition("use.other")[1]["kind"] == "variable"


def test_signature_parameters_count_toward_the_fact_cap(monkeypatch):
    monkeypatch.setattr(code_static, "MAX_FILE_FACTS", 10)
    params = ", ".join(f"p{i}" for i in range(40))
    facts = parse_source(f"def f({params}):\n    pass\n")
    assert facts["error"] == "more than 10 facts; not indexed" and facts["defs"] == []
    assert parse_source("def f(a, *b, c, **d):\n    pass\n")["error"] is None


def test_a_call_before_a_later_import_means_the_file_s_own_definition(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def create(x):\n    pass\n",
        "use.py": (
            "def create(x, y):\n    pass\n"
            "create(1, 2)\n"
            "from lib import create\n"
            "create(3)\n"
        ),
    })
    index = _index(root, tmp_path / "cache")
    assert [(c["line"], c["problems"]) for c in references(index, ["use.create"])["calls"]] == [
        (3, [])
    ]
    assert [(c["line"], c["problems"]) for c in references(index, ["lib.create"])["calls"]] == [
        (5, [])
    ]


def test_an_instance_method_called_through_cls_takes_its_instance_explicitly(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "use.py": (
            "class C:\n"
            "    def save(self, x):\n        pass\n"
            "    @classmethod\n"
            "    def make(cls):\n"
            "        cls.save(cls(), 1)\n"
            "        cls.save(1)\n"
        ),
    })
    found = references(_index(root, tmp_path / "cache"), ["use.C.save"])
    assert [(c["line"], c["problems"]) for c in found["calls"]] == [
        (6, []), (7, ["1 required positional argument(s) missing"]),
    ]


def test_shape_checks_build_each_signature_once_and_name_few_names(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    params = ", ".join(f"k{i}" for i in range(500))
    _write(root, {
        "use.py": f"def f(*, {params}):\n    pass\n" + "f()\n" * 50 + "f(1, k0=1, zz=2)\n",
    })
    index = _index(root, tmp_path / "cache")
    built = []
    real = code_static.signature_shape
    monkeypatch.setattr(
        code_static, "signature_shape", lambda d, b: built.append(1) or real(d, b)
    )
    calls = references(index, ["use.f"])["calls"]
    assert len(calls) == 51 and len(built) == 1
    names = ", ".join(f"k{i}" for i in range(10))
    assert calls[0]["problems"] == [f"missing keyword-only: {names} and 490 more"]
    assert calls[-1]["problems"] == [
        f"1 positional argument(s) too many; keyword-only: {names} and 490 more",
        "unknown keyword(s): zz",
        "missing keyword-only: " + ", ".join(f"k{i}" for i in range(1, 11)) + " and 489 more",
    ]


def test_shape_problems_count_positions_filled_by_position_or_keyword():
    definition = {
        "kind": "function", "pos": ["a", "b", "c", "d"], "posonly": 2, "kwonly": [],
        "kwreq": [], "defaults": 1, "star": False, "dstar": False,
    }
    call = {"pos": 1, "kw": ["c"], "star": False, "dstar": False}
    assert shape_problems(definition, call, False) == ["1 required positional argument(s) missing"]
    assert shape_problems(definition, {**call, "kw": ["b", "c"]}, False) == [
        "positional-only passed by keyword: b", "1 required positional argument(s) missing",
    ]
    assert shape_problems(definition, {**call, "pos": 3, "kw": ["c"]}, False) == [
        "multiple values for: c"
    ]
    assert shape_problems(definition, {**call, "pos": 2}, False) == []
    # Bound: `a` is passed implicitly, so `b` is the one positional-only parameter left.
    assert shape_problems(definition, {**call, "pos": 0, "kw": ["b"]}, True) == [
        "positional-only passed by keyword: b", "2 required positional argument(s) missing",
    ]


def test_destructuring_targets_keep_source_order():
    facts = parse_source("a, (b, *c), [d, (e, f)], *g = x\n")
    assert [d["name"] for d in facts["defs"]] == ["a", "b", "c", "d", "e", "f", "g"]


def test_a_call_through_another_module_s_variable_is_a_by_name_candidate(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "store.py": "class Store:\n    def save(self, x):\n        pass\n\n\n"
                    "def make():\n    return Store()\n",
        "state.py": "from store import make\nsvc = make()\n",
        "use.py": "import state\nstate.svc.save(1)\n",
    })
    found = references(_index(root, tmp_path / "cache"), ["store.Store.save"])
    assert found["calls"] == []
    assert [(c["file"], c["line"]) for c in found["candidates"]] == [("use.py", 2)]


@pytest.mark.parametrize(
    "made",
    ["svc = Store()", "svc: Store = make()", "svc = store.Store()", "svc = make()"],
)
def test_a_variable_made_by_its_class_resolves_its_members(tmp_path, made):
    # `settings = Settings()` in one module, `settings.save()` and `settings.TABLE` elsewhere:
    # the variable is an instance of the class, so both are exact; a factory's result is not.
    root = tmp_path / "repo"
    _write(root, {
        "store.py": "class Store:\n    TABLE = 't'\n\n    def save(self, x):\n        pass\n\n\n"
                    "def make():\n    return Store()\n",
        "state.py": f"import store\nfrom store import Store, make\n{made}\n",
        "use.py": "import state\nstate.svc.save(1)\nprint(state.svc.TABLE)\n",
    })
    index = _index(root, tmp_path / "cache")
    calls = references(index, ["store.Store.save"])["calls"]
    uses = references(index, ["store.Store.TABLE"])["uses"]
    exact = made != "svc = make()"
    assert [(c["file"], c["line"]) for c in calls] == ([("use.py", 2)] if exact else [])
    assert [(u["file"], u["line"]) for u in uses] == ([("use.py", 3)] if exact else [])


def test_a_class_body_instance_resolves_its_members(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "m.py": (
            "class Settings:\n    TABLE = 't'\n\n\n"
            "class Model:\n    settings = Settings()\n    TABLE_NAME = settings.TABLE\n\n\n"
            "print(Model.settings.TABLE, Model.TABLE_NAME)\n"
        ),
    })
    found = references(_index(root, tmp_path / "cache"), ["m.Settings.TABLE"], follow=True)
    assert [(u["line"], u["how"], u.get("via")) for u in found["uses"]] == [
        (7, "assign", None), (10, "arg", None), (10, "arg", "TABLE_NAME"),
    ]


def test_variables_made_from_each_other_stop(tmp_path):
    root = tmp_path / "repo"
    _write(root, {"m.py": "a = b.Make()\nb = a.Make()\na.save()\n"})
    found = references(_index(root, tmp_path / "cache"), ["m.a.save"])
    assert found["calls"] == []


def test_value_uses_beyond_call_arguments(tmp_path):
    # From the field: a setting read into a module constant, put in a set, returned, compared;
    # every one is a use, and a local of the same name is not.
    root = tmp_path / "repo"
    _write(root, {
        "config.py": "class Settings:\n    TABLE = 't'\n\n\nsettings = Settings()\n",
        "model.py": (
            "from config import settings\n"
            "TABLE_NAME = settings.TABLE\n"
            "ALLOWED = {settings.TABLE, 'x'}\n"
            "ORDER = [settings.TABLE]\n"
            "BY = {settings.TABLE: 1}\n"
            "def pick(t=settings.TABLE):\n"
            "    if t == settings.TABLE:\n"
            "        return settings.TABLE\n"
            "    label = f'{settings.TABLE}'\n"
            "    return label or settings.TABLE\n"
            "def local(settings):\n"
            "    return settings.TABLE\n"
        ),
    })
    found = references(_index(root, tmp_path / "cache"), ["config.Settings.TABLE"])
    assert [(u["line"], u["how"], u["within"]) for u in found["uses"]] == [
        (2, "assign", "TABLE_NAME"), (3, "member", "set"), (4, "member", "list"),
        (5, "member", "dict"), (6, "default", "pick"), (7, "compare", ""),
        (8, "return", ""), (9, "read", ""), (10, "read", ""),
    ]


def test_refs_follows_a_variable_assigned_from_the_target(tmp_path, capsys):
    root = tmp_path / "repo"
    _write(root, {
        "config.py": "class Settings:\n    TABLE = 't'\n\n\nsettings = Settings()\n",
        "model.py": (
            "from config import settings\n"
            "TABLE_NAME = settings.TABLE\n"
            "def write(db):\n"
            "    db.put(TABLE_NAME)\n"
            "def read(db):\n"
            "    table = settings.TABLE\n"
            "    return db.get(table)\n"
        ),
    })
    index = _index(root, tmp_path / "cache")
    found = references(index, ["config.Settings.TABLE"], follow=True)
    assert [(u["line"], u.get("via")) for u in found["uses"]] == [
        (2, None), (6, None), (4, "TABLE_NAME"),
    ]
    # Without ``follow`` (``impact`` asks this way), only the target's own sites.
    plain = references(index, ["config.Settings.TABLE"])
    assert [u["line"] for u in plain["uses"]] == [2, 6]
    argv = ["--root", str(root), "--cache-dir", str(tmp_path / "cache")]
    assert main(["refs", "Settings.TABLE", *argv]) == 0
    out = capsys.readouterr().out
    assert "3 used as a value" in out
    assert "assigned to TABLE_NAME" in out and "assigned to table" in out
    line = next(x for x in out.splitlines() if x.startswith("model.py:4 "))
    assert "passed to db.put" in line and "via TABLE_NAME" in line


def test_every_other_read_is_a_plain_read(tmp_path):
    # An `if` test, an operand, a loop's iterable, an f-string, a subscript key, `and`/`or`:
    # each reads the name; a callee and an argument are kept once, as the call and the argument.
    root = tmp_path / "repo"
    _write(root, {
        "m.py": "FLAG = 1\nTABLES = []\n",
        "use.py": (
            "import m\n"
            "if m.FLAG:\n"
            "    x = 1 + m.FLAG\n"
            "for t in m.TABLES:\n"
            "    print(f'{m.FLAG}', {}[m.FLAG], m.FLAG or 2, -m.FLAG)\n"
        ),
    })
    found = references(_index(root, tmp_path / "cache"), ["m.FLAG"])
    assert [(u["line"], u["how"], u["within"]) for u in found["uses"]] == [
        (2, "read", ""), (3, "read", ""), (5, "read", ""), (5, "read", ""), (5, "read", ""),
        (5, "read", ""),
    ]
    tables = references(_index(root, tmp_path / "cache"), ["m.TABLES"])
    assert [(u["line"], u["how"]) for u in tables["uses"]] == [(4, "read")]


def test_only_a_direct_last_assignment_is_followed(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "m.py": (
            "VALUE = 2\n"
            "TOTAL = 10\n"
            "TOTAL += VALUE\n"  # a computed value, not another name
            "A = B = VALUE\n"  # both are aliases
            "LATER = VALUE\n"
            "LATER = 3\n"  # rebound: LATER no longer means VALUE
            "WIDE = (\n    VALUE\n)\n"
            "GROWN = VALUE\n"
            "GROWN += 1\n"  # rebound by the augmented assignment
            "print(TOTAL, A, B, LATER, WIDE, GROWN)\n"
        ),
    })
    found = references(_index(root, tmp_path / "cache"), ["m.VALUE"], follow=True)
    assert [(u["line"], u["how"], u["within"], u.get("via")) for u in found["uses"]] == [
        (3, "augment", "TOTAL", None), (4, "assign", "A", None), (4, "assign", "B", None),
        (5, "assign", "LATER", None), (8, "assign", "WIDE", None), (10, "assign", "GROWN", None),
        (12, "arg", "print", "A"), (12, "arg", "print", "B"), (12, "arg", "print", "WIDE"),
    ]
    assert not found["truncated"]


def test_aliases_past_the_bound_mark_the_result_cut(tmp_path):
    root = tmp_path / "repo"
    n = code_static.MAX_FOLLOWED + 1
    body = "".join(f"A{i} = VALUE\nprint(A{i})\n" for i in range(n))
    _write(root, {"m.py": f"VALUE = 2\n{body}"})
    found = references(_index(root, tmp_path / "cache"), ["m.VALUE"], follow=True)
    assert len([u for u in found["uses"] if "via" in u]) == code_static.MAX_FOLLOWED
    assert found["truncated"]


def test_a_function_aliased_in_a_class_body_is_not_checked(tmp_path):
    # `emit = send` in a class makes `Client().emit()` pass the instance as `value`.
    root = tmp_path / "repo"
    _write(root, {
        "m.py": "def send(value):\n    pass\n\n\nclass Client:\n    emit = send\n\n\n"
                "Client().emit()\n",
    })
    found = references(_index(root, tmp_path / "cache"), ["m.send"], follow=True)
    assert [(c["line"], c["via"], c["problems"]) for c in found["calls"]] == [(9, "emit", [])]
    assert found["counts"]["unchecked"] == 1 and found["counts"]["problems"] == 0


def test_an_alias_s_totals_are_counted_past_the_listing_limit(tmp_path):
    root = tmp_path / "repo"
    _write(root, {"m.py": "def send(value):\n    pass\n\n\nALIAS = send\n" + "ALIAS()\n" * 5})
    found = references(_index(root, tmp_path / "cache"), ["m.send"], limit=2, follow=True)
    assert len(found["calls"]) == 2
    assert found["counts"]["calls"] == 5 and found["counts"]["problems"] == 5
    assert found["truncated"]


def test_a_followed_alias_is_found_when_only_tests_are_listed(tmp_path):
    # The alias is assigned in production code; `--tests` lists only the test that reads it.
    root = tmp_path / "repo"
    _write(root, {
        "m.py": "VALUE = 'x'\nALIAS = VALUE\n",
        "test_m.py": "from m import ALIAS\n\n\ndef test_it(x):\n    assert x == ALIAS\n",
    })
    found = references(
        _index(root, tmp_path / "cache"), ["m.VALUE"], lambda row: row["test"], follow=True
    )
    assert [(u["file"], u["line"], u.get("via")) for u in found["uses"]] == [
        ("test_m.py", 5, "ALIAS"),
    ]


@pytest.mark.parametrize(
    ("target", "problems", "unchecked"),
    [
        ("m.send", ["missing keyword-only: token", "1 required positional argument(s) missing"], 0),
        ("m.Box", ["1 required positional argument(s) missing"], 0),
        ("m.Box.put", [], 1),  # bound or not depends on what was assigned: not checked
    ],
)
def test_a_call_through_a_followed_alias_is_checked_against_the_target(
    tmp_path, target, problems, unchecked
):
    root = tmp_path / "repo"
    _write(root, {
        "m.py": (
            "def send(value, *, token):\n    pass\n\n\n"
            "class Box:\n    def __init__(self, size):\n        pass\n\n"
            "    def put(self, item):\n        pass\n\n\n"
            f"ALIAS = {target.removeprefix('m.')}\n"
            "ALIAS()\n"
        ),
    })
    found = references(_index(root, tmp_path / "cache"), [target], follow=True)
    assert [(c["line"], c["via"], c["problems"]) for c in found["calls"]] == [
        (14, "ALIAS", problems),
    ]
    assert found["counts"]["problems"] == bool(problems)
    assert found["counts"]["unchecked"] == unchecked


def test_a_module_level_walrus_replaces_an_import(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def create(x):\n    pass\n",
        "use.py": (
            "from lib import create\n"
            "if (create := wrap(create)):\n"
            "    create(1, 2)\n"
            "values = [(seen := v) for v in range(3)]\n"
        ),
    })
    index = _index(root, tmp_path / "cache")
    found = references(index, ["lib.create"])
    assert found["calls"] == [] and [u["line"] for u in found["uses"]] == [2]
    assert [c["line"] for c in references(index, ["use.create"])["calls"]] == [3]
    assert index.definition("use.seen")[1]["kind"] == "variable"


def test_a_line_s_patches_are_read_once_and_only_for_the_name(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    line = "; ".join(["patch.object(other, 'create', create)"] * 30)
    _write(root, {
        "lib.py": "def create(x):\n    pass\n",
        "other.py": "def create(x):\n    pass\n",
        "test_use.py": (
            "from unittest.mock import patch\nimport other\nfrom lib import create\n"
            + "patch.object(other, 'save', 1); " * 30 + line + "\n"
        ),
    })
    index = _index(root, tmp_path / "cache")
    decoded = []
    real = code_static._row
    monkeypatch.setattr(
        code_static, "_row",
        lambda text, types: decoded.append(types is code_static.PATCH_TYPES) or real(text, types),
    )
    found = references(index, ["lib.create"])
    assert len(found["uses"]) == 30  # the line patches other.create, not lib.create
    # Each `create` patch row once as a patch site and once for the line; `save` rows never.
    assert decoded.count(True) == 30 + 30


def test_signature_shapes_share_one_budget_of_parameters():
    shapes = code_static._Shapes(30, 100)
    definition = {
        "kind": "function", "pos": [f"p{i}" for i in range(20)], "posonly": 0,
        "kwonly": [], "kwreq": [], "defaults": 0, "star": False, "dstar": False,
    }
    first = shapes.get(("m0.f", False, False), definition, False)
    for n in range(1, 4):
        assert shapes.get((f"m{n}.f", False, False), definition, False)["required"] == 20
        assert shapes.size <= 30 and len(shapes.kept) == 1
    # The kept shape is never dropped: alternating calls do not rebuild it.
    assert shapes.get(("m0.f", False, False), definition, False) is first
    # 80 parameters built so far; the rest of the work fits one more shape, then none.
    assert shapes.get(("m1.f", False, False), definition, False) is not None
    assert shapes.get(("m2.f", False, False), definition, False) is None
    assert shapes.get(("m0.f", False, False), definition, False) is first


def test_calls_past_the_shape_work_are_listed_unchecked(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    params = ", ".join(f"p{i}" for i in range(20))
    _write(root, {
        f"m{n}.py": f"def f({params}):\n    pass\nf()\nf()\n" for n in range(3)
    })
    index = _index(root, tmp_path / "cache")
    monkeypatch.setattr(code_static, "MAX_SHAPE_PARAMS", 0)
    monkeypatch.setattr(code_static, "MAX_SHAPE_WORK", 50)
    found = references(index, [f"m{n}.f" for n in range(3)])
    missing = ["20 required positional argument(s) missing"]
    assert [c["problems"] for c in found["calls"]] == [missing, missing] + [[]] * 4
    assert found["counts"]["unchecked"] == 4


def test_a_module_level_object_patch_means_the_binding_of_its_line(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def create(x):\n    pass\n",
        "other.py": "def create(x):\n    pass\n",
        "test_use.py": (
            "from unittest.mock import patch\n"
            "import lib as api\n"
            "patcher = patch.object(api, 'create')\n"
            "import other as api\n"
            "def test_it():\n    patch.object(api, 'create')\n"
        ),
    })
    index = _index(root, tmp_path / "cache")
    assert [p["line"] for p in references(index, ["lib.create"])["patches"]] == [3]
    assert [p["line"] for p in references(index, ["other.create"])["patches"]] == [6]


def test_repeated_fact_strings_count_toward_a_byte_budget(monkeypatch):
    monkeypatch.setattr(code_static, "MAX_FILE_FACT_BYTES", 1_000)
    name = "f" * 200
    facts = parse_source(f"def {name}():\n" + "    x()\n" * 10)
    assert facts["error"] == "more than 1000 bytes of facts; not indexed"
    assert facts["calls"] == [] and facts["defs"] == []
    assert parse_source(f"def {name}():\n" + "    x()\n" * 2)["error"] is None


def test_refs_keeps_sites_within_a_byte_budget(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    scope = "s" * 500
    _write(root, {
        "lib.py": "def create(x):\n    pass\n",
        **{
            f"use{n}.py": f"from lib import create\ndef {scope}():\n" + "    create(1)\n" * 10
            for n in range(3)
        },
    })
    index = _index(root, tmp_path / "cache")
    monkeypatch.setattr(code_static, "MAX_LISTED_BYTES", 6_000)
    found = references(index, ["lib.create"])
    assert found["counts"]["calls"] == 30 and found["truncated"]
    assert 0 < len(found["calls"]) <= 12  # about 500 bytes a site
    assert sum(code_static._text_bytes(c) for c in found["calls"]) <= 6_000


def test_an_eager_module_level_comprehension_means_the_binding_of_its_moment(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def create(x):\n    pass\n",
        "use.py": (
            "from lib import create\n"
            "results = [create(1) for _ in range(2)]\n"
            "table = {k: create(k) for k in range(2)}\n"
            "later = (create(2) for _ in range(2))\n"
            "create = None\n"
        ),
    })
    found = references(_index(root, tmp_path / "cache"), ["lib.create"])
    # The generator runs after `create = None`: it is not a call of the import.
    assert [c["line"] for c in found["calls"]] == [2, 3]


def test_matches_are_cut_by_the_size_of_their_definitions(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    params = ", ".join(f"p{i}" for i in range(200))
    _write(root, {f"m{n}.py": f"def f({params}):\n    pass\n" for n in range(5)})
    index = _index(root, tmp_path / "cache")
    assert len(index.find("f")) == 5 and not index.find_cut
    monkeypatch.setattr(code_static, "MAX_MATCH_BYTES", 5_000)
    found = index.find("f")
    assert 1 <= len(found) < 5 and index.find_cut
    assert index.find("m3.f") == ["m3.f"]  # a full name is never cut


def test_a_function_import_the_function_also_assigns_is_left_unresolved(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def create(x):\n    pass\n",
        "use.py": (
            "def run(callback):\n"
            "    from lib import create\n"
            "    create = callback\n"
            "    create(1, 2)\n"
            "def plain():\n"
            "    from lib import create\n"
            "    create(3)\n"
        ),
    })
    index = _index(root, tmp_path / "cache")
    assert [c["line"] for c in references(index, ["lib.create"])["calls"]] == [7]


def test_a_function_that_imports_a_name_from_two_places_leaves_it_unresolved(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def create(x):\n    pass\n",
        "other.py": "def create(x, y):\n    pass\n",
        "use.py": (
            "def run():\n"
            "    from lib import create\n"
            "    create(1)\n"
            "    from other import create\n"
            "    create(1, 2)\n"
            "def again():\n"
            "    from lib import create\n"
            "    from lib import create\n"
            "    create(3)\n"
        ),
    })
    index = _index(root, tmp_path / "cache")
    assert [c["line"] for c in references(index, ["lib.create"])["calls"]] == [9]
    assert references(index, ["other.create"])["calls"] == []


def test_a_value_passed_under_a_global_declaration_means_the_module_binding(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def create(x):\n    pass\n",
        "other.py": "def create(x):\n    pass\n",
        "use.py": (
            "from lib import create\n"
            "def outer():\n"
            "    from other import create\n"
            "    def inner():\n        global create\n        register(create)\n"
        ),
    })
    index = _index(root, tmp_path / "cache")
    assert [u["line"] for u in references(index, ["lib.create"])["uses"]] == [6]
    assert references(index, ["other.create"])["uses"] == []


def test_a_comprehension_in_a_class_body_skips_the_class_s_names(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def create(x):\n    pass\n",
        "use.py": (
            "from lib import create\n"
            "class C:\n"
            "    def create(x, y):\n        pass\n"
            "    items = [create(1) for _ in range(1)]\n"
        ),
    })
    index = _index(root, tmp_path / "cache")
    found = references(index, ["lib.create"])
    assert [(c["line"], c["scope"], c["problems"]) for c in found["calls"]] == [
        (5, "C.<listcomp>", [])
    ]
    assert references(index, ["use.C.create"])["calls"] == []


def test_aliases_are_capped_and_queried_in_batches(tmp_path):
    root = tmp_path / "repo"
    aliases = "".join(f"from lib import create as a{i}\na{i}(1)\n" for i in range(50))
    _write(root, {"lib.py": "def create(x):\n    pass\n", "use.py": aliases})
    index = _index(root, tmp_path / "cache")
    statements = []
    index.db.set_trace_callback(statements.append)
    found = references(index, ["lib.create"])
    index.db.set_trace_callback(None)
    assert len(found["calls"]) == 50 and not found["truncated"]
    assert sum(1 for s in statements if "binds WHERE alias" in s) <= 3  # one per round
    assert sum(1 for s in statements if "FROM calls" in s) == 1
    assert len(index.aliases({"create"}, limit=10)) == 10


def test_a_lambda_in_a_class_body_skips_the_class_s_names(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def create(x):\n    pass\n",
        "use.py": (
            "from lib import create\n"
            "class C:\n"
            "    def create(x, y):\n        pass\n"
            "    callback = staticmethod(lambda: create(1))\n"
        ),
    })
    index = _index(root, tmp_path / "cache")
    found = references(index, ["lib.create"])
    assert [(c["line"], c["scope"], c["problems"]) for c in found["calls"]] == [
        (5, "C.<lambda>", [])
    ]
    assert references(index, ["use.C.create"])["calls"] == []


def test_on_windows_an_index_outside_the_profile_goes_to_the_user_cache(tmp_path, monkeypatch):
    from pathlib import Path

    root = _repo(tmp_path)
    monkeypatch.setattr(code_static, "ON_WINDOWS", True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(code_static, "user_cache_dir", lambda r: tmp_path / "home" / "cache")
    outside = db_path(Cache(root, tmp_path / "shared"))
    assert outside.parent.parent == tmp_path / "home" / "cache"
    inside = db_path(Cache(root, tmp_path / "home" / "mine"))
    assert inside.parent.parent == tmp_path / "home" / "mine"


def test_an_index_swapped_while_sqlite_opens_it_is_refused(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    cache_dir = tmp_path / "cache"
    _index(root, cache_dir).db.close()
    victim = tmp_path / "victim.sqlite3"
    _victim_db(victim)
    path = db_path(Cache(root, cache_dir))
    real = sqlite3.connect

    def swap_then_connect(target, *args, **kwargs):
        os.unlink(target)
        os.link(victim, target)  # another user swaps in a link after the last check
        return real(target, *args, **kwargs)

    monkeypatch.setattr(code_static.sqlite3, "connect", swap_then_connect)
    with pytest.raises(UnsafeIndex, match="changed while it was opened"):
        _index(root, cache_dir)
    monkeypatch.setattr(code_static.sqlite3, "connect", real)
    assert sqlite3.connect(victim).execute("SELECT secret FROM files").fetchall() == [("kept",)]
    assert path.exists()


def test_refs_reads_at_most_the_examined_budget(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    _write(root, {"lib.py": "def f():\n    pass\n", "use.py": "from lib import f\n" + "f()\n" * 5})
    index = _index(root, tmp_path / "cache")
    monkeypatch.setattr(code_static, "MAX_EXAMINED", 3)
    found = references(index, ["lib.f"])
    assert found["counts"]["calls"] == 3 and found["truncated"]


def test_a_class_body_call_before_the_class_defines_the_name_is_the_import(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def create(x):\n    pass\n",
        "use.py": (
            "from lib import create\n"
            "class C:\n"
            "    value = create(1)\n"
            "    def create(x, y):\n        pass\n"
        ),
    })
    index = _index(root, tmp_path / "cache")
    assert [(c["line"], c["problems"]) for c in references(index, ["lib.create"])["calls"]] == [
        (3, [])
    ]
    assert references(index, ["use.C.create"])["calls"] == []


def test_a_class_body_import_after_the_class_s_definition_replaces_it(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def create(x):\n    pass\n",
        "use.py": (
            "class C:\n"
            "    def create(x, y):\n        pass\n"
            "    first = create(1, 2)\n"
            "    from lib import create\n"
            "    second = create(1)\n"
        ),
    })
    index = _index(root, tmp_path / "cache")
    assert [(c["line"], c["problems"]) for c in references(index, ["lib.create"])["calls"]] == [
        (6, [])
    ]
    assert [c["line"] for c in references(index, ["use.C.create"])["calls"]] == [4]


def test_a_class_body_call_before_the_class_s_import_is_the_enclosing_binding(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def create(x):\n    pass\n",
        "other.py": "def create(x):\n    pass\n",
        "use.py": (
            "from lib import create\n"
            "class C:\n"
            "    first = create(1)\n"
            "    from other import create\n"
            "    second = create(2)\n"
        ),
    })
    index = _index(root, tmp_path / "cache")
    assert [c["line"] for c in references(index, ["lib.create"])["calls"]] == [3]
    assert [c["line"] for c in references(index, ["other.create"])["calls"]] == [5]


def test_an_object_patch_under_a_global_declaration_means_the_module_binding(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def create(x):\n    pass\n",
        "other.py": "def create(x):\n    pass\n",
        "tests/test_it.py": (
            "import lib as api\nfrom unittest.mock import patch\n"
            "def outer():\n"
            "    import other as api\n"
            "    def inner():\n        global api\n        patch.object(api, 'create')\n"
        ),
    })
    index = _index(root, tmp_path / "cache")
    assert [p["line"] for p in references(index, ["lib.create"])["patches"]] == [7]
    assert references(index, ["other.create"])["patches"] == []


def test_a_destructuring_assignment_binds_every_name_it_unpacks(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def create(x):\n    pass\n",
        "use.py": (
            "from lib import create\n"
            "create, other = (lambda x, y: None), 1\n"
            "[a, (b, *rest)] = 1, (2, 3)\n"
            "create(1, 2)\n"
        ),
    })
    index = _index(root, tmp_path / "cache")
    assert references(index, ["lib.create"])["calls"] == []
    assert [c["line"] for c in references(index, ["use.create"])["calls"]] == [4]
    for name in ("create", "other", "a", "b", "rest"):
        assert index.definition(f"use.{name}")[1]["kind"] == "variable"


def test_a_signature_of_many_keyword_only_parameters_reads_in_linear_time():
    names = [f"k{i}" for i in range(40_000)]
    source = "def f(*, " + ", ".join(names) + "):\n    pass\n"
    started = time.perf_counter()
    facts = parse_source(source)
    assert time.perf_counter() - started < 5
    assert facts["defs"][0]["kwreq"] == names
    definition = facts["defs"][0]
    call = {"pos": 0, "kw": names, "star": False, "dstar": False}
    started = time.perf_counter()
    assert shape_problems(definition, call, False) == []
    assert time.perf_counter() - started < 1


def test_a_dense_file_is_reported_not_indexed(monkeypatch):
    monkeypatch.setattr(code_static, "MAX_FILE_TOKENS", 100)
    facts = parse_source("f()\n" * 40)
    assert facts["error"] == "more than 100 tokens; not indexed" and facts["calls"] == []
    monkeypatch.setattr(code_static, "MAX_FILE_TOKENS", 10_000)
    monkeypatch.setattr(code_static, "MAX_FILE_FACTS", 10)
    facts = parse_source("f()\n" * 40)
    assert facts["error"] == "more than 10 facts; not indexed"
    assert parse_source("f()\n" * 5)["error"] is None


def test_one_call_of_many_arguments_counts_toward_the_fact_cap(monkeypatch):
    monkeypatch.setattr(code_static, "MAX_FILE_FACTS", 10)
    facts = parse_source("import x\nf(" + ", ".join(["x"] * 40) + ")\n")
    assert facts["error"] == "more than 10 facts; not indexed"
    assert facts["uses"] == []
    facts = parse_source("f(" + ", ".join(f"k{i}=1" for i in range(40)) + ")\n")
    assert facts["error"] == "more than 10 facts; not indexed"
    assert parse_source("import x\nf(x, x, x)\n")["error"] is None


@pytest.mark.parametrize(
    "source",
    [
        "x = " * 40 + "0\n",
        "a: int = 0\n" * 40,
        "def f():\n    pass\n" * 40,
        "class C:\n    pass\n" * 40,
        "from lib import " + ", ".join(f"n{i}" for i in range(40)) + "\n",
        "import " + ", ".join(f"m{i}" for i in range(40)) + "\n",
        "from unittest.mock import patch\n" + "patch('lib.f')\n" * 20,
    ],
    ids=["chained-assignment", "annotated", "functions", "classes", "from-import", "import",
         "patches"],
)
def test_every_kind_of_fact_counts_toward_the_fact_cap(monkeypatch, source):
    monkeypatch.setattr(code_static, "MAX_FILE_FACTS", 10)
    facts = parse_source(source)
    assert facts["error"] == "more than 10 facts; not indexed"
    assert facts["defs"] == facts["imports"] == facts["calls"] == facts["patches"] == []


def test_a_class_records_whether_a_decorator_generates_its_init(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "import dataclasses\nfrom dataclasses import dataclass\n"
                  "from dataclasses import dataclass as dc\n"
                  "@dataclass\nclass A:\n    pass\n"
                  "@dataclasses.dataclass(frozen=True)\nclass B:\n    pass\n"
                  "@dataclass(init=False)\nclass C:\n    pass\n"
                  "class D:\n    pass\n"
                  "@dc\nclass E:\n    pass\n",
    })
    index = _index(root, tmp_path / "cache")
    assert [index.definition(f"lib.{c}")[1]["gen"] for c in "ABCDE"] == [
        True, True, False, False, True,
    ]


def test_a_followed_alias_skips_import_time_reads_before_its_assignment(tmp_path):
    # Before `ALIAS = VALUE` runs, module code and class bodies read the old value; a
    # function body runs later, whatever its line.
    root = tmp_path / "repo"
    _write(root, {
        "m.py": (
            "VALUE = 2\n"
            "ALIAS = 3\n"
            "print(ALIAS)\n"  # 3 here
            "class Early:\n    seen = [ALIAS]\n"  # 3 here too
            "def later():\n    return ALIAS\n"  # VALUE when called
            "ALIAS = VALUE\n"
            "print(ALIAS)\n"
            "class Late:\n    def m(self):\n        print([x for x in ALIAS])\n"
        ),
    })
    found = references(_index(root, tmp_path / "cache"), ["m.VALUE"], follow=True)
    assert [(u["line"], u["how"], u.get("via")) for u in found["uses"]] == [
        (8, "assign", None), (7, "return", "ALIAS"), (9, "arg", "ALIAS"),
        (12, "read", "ALIAS"),
    ]
    assert found["counts"]["uses"] == 4


def test_a_read_after_an_assignment_on_the_same_line_sees_that_assignment(tmp_path):
    # After `ALIAS = VALUE;` on one line, the read holds VALUE; a read inside a right-hand
    # side on one line (`ALIAS = ALIAS + 1`) still holds the binding from before.
    root = tmp_path / "repo"
    _write(root, {
        "m.py": (
            "VALUE = 2\n"
            "ALIAS = 3\n"
            "ALIAS = VALUE; print(ALIAS)\n"  # VALUE
            "ALIAS = 3; print(ALIAS)\n"  # 3
            "ALIAS = VALUE\n"
            "ALIAS = [ALIAS]; ALIAS = 3\n"  # VALUE inside the list
            "ALIAS = VALUE\n"
        ),
    })
    found = references(_index(root, tmp_path / "cache"), ["m.VALUE"], follow=True)
    assert sorted((u["line"], u.get("via")) for u in found["uses"] if "via" in u) == [
        (3, "ALIAS"), (6, "ALIAS"),
    ]


@pytest.mark.parametrize(
    ("source", "via_lines"),
    [
        # Two assignments on one line: the read after both holds the second one, 3.
        ("VALUE = 2\nALIAS = 0\nALIAS = VALUE; ALIAS = 3; print(ALIAS)\nALIAS = VALUE\n", []),
        # The iterable runs before the loop target is bound: it still holds VALUE.
        ("VALUE = 2\nALIAS = VALUE\nfor ALIAS in [ALIAS]:\n    pass\nALIAS = VALUE\n", [3]),
        # So does the value of `:=`.
        ("VALUE = 2\nALIAS = VALUE\n(ALIAS := [ALIAS])\nALIAS = VALUE\n", [3]),
        # The last assignment shares its line with one that is not the last: not followed.
        ("VALUE = 2\nALIAS = VALUE; ALIAS = 3\nprint(ALIAS)\n", []),
    ],
)
def test_a_binding_takes_effect_where_python_binds_it(tmp_path, source, via_lines):
    # A binding's position is where it takes effect, by line and column, not
    # where its target is written.
    root = tmp_path / "repo"
    _write(root, {"m.py": source})
    found = references(_index(root, tmp_path / "cache"), ["m.VALUE"], follow=True)
    assert sorted(u["line"] for u in found["uses"] if "via" in u) == via_lines


def test_an_import_time_read_after_an_earlier_assignment_from_the_target_counts(tmp_path):
    # Between two `ALIAS = VALUE`, the alias already holds VALUE; after `ALIAS = 3` it does not.
    root = tmp_path / "repo"
    _write(root, {
        "m.py": (
            "VALUE = 2\n"
            "ALIAS = VALUE\n"
            "print(ALIAS)\n"  # VALUE
            "ALIAS = 3\n"
            "print(ALIAS)\n"  # 3
            "ALIAS = VALUE\n"
            # A right-hand side reads the binding from before its own assignment: VALUE here.
            "ALIAS = (\n    ALIAS + 1\n)\n"
            "ALIAS = VALUE\n"
        ),
    })
    found = references(_index(root, tmp_path / "cache"), ["m.VALUE"], follow=True)
    assert sorted((u["line"], u.get("via")) for u in found["uses"] if "via" in u) == [
        (3, "ALIAS"), (8, "ALIAS"),
    ]


def test_a_patch_written_before_an_alias_assignment_is_still_followed(tmp_path):
    # `patch("m.ALIAS")` looks the target up when the test runs, after `ALIAS = VALUE`.
    root = tmp_path / "repo"
    _write(root, {
        "m.py": (
            "from unittest.mock import patch\n"
            "VALUE = 2\n"
            "@patch('m.ALIAS')\ndef test_value(mock_value):\n    pass\n"
            "ALIAS = VALUE\n"
        ),
    })
    found = references(_index(root, tmp_path / "cache"), ["m.VALUE"], follow=True)
    assert [(p["line"], p.get("via")) for p in found["patches"]] == [(3, "ALIAS")]


def test_the_module_path_each_name_is_stored_under_counts_toward_the_byte_budget(
    tmp_path, monkeypatch
):
    # The same small file under a long path stores the path once per definition.
    monkeypatch.setattr(code_static, "MAX_FILE_FACT_BYTES", 600)
    root = tmp_path / "repo"
    deep = "/".join("p" * 8 for _ in range(5))
    body = "".join(f"v{i} = 1\n" for i in range(20))
    _write(root, {"short.py": body, f"{deep}/long.py": body})
    db = update(Cache(root, tmp_path / "cache"), load_config(root))
    assert code_static.summary(db)["errors"] == [{
        "file": f"{deep}/long.py",
        "error": "more than 600 bytes of names under its module path; not indexed",
    }]
    assert Index(db).definition("short.v19") is not None


def test_a_file_s_project_root_is_looked_up_once_for_many_calls(tmp_path, monkeypatch):
    # A file deep below a project marker, with many calls to resolve, paid for its depth
    # once per resolved binding.
    root = tmp_path / "repo"
    deep = "/".join(f"d{i}" for i in range(30))
    _write(root, {
        "apps/api/pyproject.toml": "[project]\nname = 'api'\n",
        "apps/api/lib.py": "def f():\n    pass\n",
        f"apps/api/{deep}/use.py": "from lib import f\n" + "f()\n" * 200,
    })
    index = _index(root, tmp_path / "cache")
    looked = []
    real = code_static.root_of
    monkeypatch.setattr(
        code_static, "root_of", lambda rel, roots: looked.append(rel) or real(rel, roots)
    )
    calls = references(index, ["apps.api.lib.f"], limit=500)["calls"]
    assert len(calls) == 200
    assert looked.count(f"apps/api/{deep}/use.py") == 1


def test_the_definition_cache_is_bounded_by_the_size_of_what_it_holds(tmp_path, monkeypatch):
    # Candidate receivers with huge signatures each kept their decoded definition.
    monkeypatch.setattr(code_static, "MAX_CACHED_DEF_BYTES", 5_000)
    root = tmp_path / "repo"
    params = ", ".join(f"p{i}" for i in range(300))
    _write(root, {"lib.py": "".join(f"def f{i}({params}):\n    pass\n" for i in range(10))})
    index = _index(root, tmp_path / "cache")
    for i in range(10):
        assert index.definition(f"lib.f{i}")[1]["name"] == f"f{i}"
        assert index._defs_bytes <= 5_000
    assert len(index._defs) < 10


def test_importers_lists_each_import_of_a_module_or_a_name_from_it(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "app/__init__.py": "",
        "app/orders.py": "import json\nfrom app.util import helper\ndef create():\n    pass\n",
        "app/util.py": "def helper():\n    pass\n",
        "app/sub/__init__.py": "",
        "app/sub/x.py": "def g():\n    pass\n",
        "app/views.py": (
            "from app import orders\nfrom app.orders import create as make\n"
            "def show():\n    from app.sub.x import g\n"
        ),
        "tests/test_orders.py": "import app.orders\n",
    })
    index = _index(root, tmp_path / "cache")
    assert index.modules_named("orders") == [("app.orders", "app/orders.py")]
    assert [(r["file"], r["line"], r["scope"], r["imports"], r["as"], r["test"])
            for r in index.importers("app.orders")] == [
        ("app/views.py", 1, "", "app.orders", "orders", False),
        ("app/views.py", 2, "", "app.orders.create", "make", False),
    ]
    # `import app.orders` binds `app`, so it counts for the package, not the module.
    assert {(r["file"], r["imports"]) for r in index.importers("app")} >= {
        ("app/views.py", "app.orders"), ("tests/test_orders.py", "app"),
    }
    # A submodule's import counts for its package, and the module's own definitions do not.
    assert [r["imports"] for r in index.imports_of("app/views.py")] == [
        "app.orders", "app.orders.create", "app.sub.x.g",
    ]
    assert [r["imports"] for r in index.imports_of("app/orders.py")] == ["json", "app.util.helper"]


def test_importers_keep_package_imports_and_resolve_a_nested_project_s_own(tmp_path):
    # `from . import orders` in a package binds `app.orders` without defining it; a nested
    # project imports its own modules by their name from its folder.
    root = tmp_path / "repo"
    _write(root, {
        "app/__init__.py": "from . import orders\n",
        "app/orders.py": "def create():\n    pass\n",
        "apps/web/pyproject.toml": "[project]\nname = 'web'\n",
        "apps/web/lib.py": "def work():\n    pass\n",
        "apps/web/run.py": "from lib import work\n",
    })
    index = _index(root, tmp_path / "cache")
    assert [(r["file"], r["imports"]) for r in index.importers("app.orders")] == [
        ("app/__init__.py", "app.orders"),
    ]
    assert [r["imports"] for r in index.imports_of("app/__init__.py")] == ["app.orders"]
    assert [(r["file"], r["imports"]) for r in index.importers("apps.web.lib")] == [
        ("apps/web/run.py", "apps.web.lib.work"),
    ]
    assert [r["imports"] for r in index.imports_of("apps/web/run.py")] == ["apps.web.lib.work"]


def test_importers_count_star_imports_and_namespace_packages(tmp_path):
    # `from m import *` depends on m though its names are known only at run time; a package
    # without `__init__.py` is named by the modules below it.
    root = tmp_path / "repo"
    _write(root, {
        "app/orders.py": "def create():\n    pass\n",  # no app/__init__.py
        "app/settings.py": "DEBUG = False\n",
        "consumer.py": "from app.settings import *\nfrom app.orders import create\ncreate()\n",
    })
    index = _index(root, tmp_path / "cache")
    assert [(r["imports"], r["as"]) for r in index.imports_of("consumer.py")] == [
        ("app.settings.*", "*"), ("app.orders.create", "create"),
    ]
    assert [r["file"] for r in index.importers("app.settings")] == ["consumer.py"]
    assert index.modules_named("app") == [("app", "app/")]
    assert [r["imports"] for r in index.importers("app")] == ["app.settings.*", "app.orders.create"]
    # the star binding resolves nothing: the call still resolves through its own import
    assert [c["file"] for c in references(index, ["app.orders.create"])["calls"]] == ["consumer.py"]


def test_environment_reads_resolve_to_os_through_the_files_imports(tmp_path):
    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    (root / "app" / "__init__.py").write_text("", encoding="utf-8")
    (root / "app" / "env.py").write_text(
        "import os\n"
        "from os import environ, getenv\n"
        "from os import environ as env\n\n\n"
        "def reads(environ_param):\n"
        '    a = os.environ["TOKEN"]\n'
        '    b = os.environ.get("TOKEN")\n'
        '    c = getenv("TOKEN", "x")\n'
        '    d = "TOKEN" in env\n'
        '    os.environ["TOKEN"] = "1"\n'
        '    del environ["TOKEN"]\n'
        # A parameter, a dict of another name and a dynamic name are not `os`'s.
        '    e = environ_param["TOKEN"]\n'
        '    f = {"TOKEN": 1}.get("TOKEN")\n'
        "    g = os.environ[a]\n"
        "    return a, b, c, d, e, f, g\n\n\n"
        "def shadowed(environ):\n"
        '    return environ["TOKEN"]\n',
        encoding="utf-8",
    )
    index = _index(root, tmp_path / "cache")
    found = [(r["line"], r["how"], r["scope"]) for r in index.env_reads("TOKEN")]
    assert found == [
        (7, "read", "reads"), (8, "get", "reads"), (9, "get", "reads"), (10, "in", "reads"),
        (11, "set", "reads"), (12, "delete", "reads"),
    ]
    assert index.env_reads("OTHER") == []


def test_a_format_change_drops_every_table_it_rebuilds(tmp_path):
    # A reset that kept a table would leave rows pointing at file ids the rebuild reuses.
    created = {
        line.split()[5] for line in code_static._SCHEMA.splitlines()
        if line.startswith("CREATE TABLE")
    }
    assert created <= set(code_static._TABLES)
