from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from codeterrain import _pytest_dump_plugin as dump


def _fd(name, argnames=(), scope="function"):
    def func():
        return None

    return SimpleNamespace(
        argname=name, argnames=tuple(argnames), scope=scope, func=func, params=None
    )


def _names(order):
    return [fd.argname for fd in order]


def test_dependencies_are_set_up_before_the_fixture_that_requests_them():
    db, client = _fd("db", scope="session"), _fd("client", ["db"])
    order = dump.setup_order(["client"], {"client": (client,), "db": (db,)})
    assert _names(order) == ["db", "client"]


def test_closure_order_wins_between_independent_fixtures_and_each_runs_once():
    a, b, shared = _fd("a", ["shared"]), _fd("b", ["shared"]), _fd("shared")
    defs = {"a": (a,), "b": (b,), "shared": (shared,)}
    assert _names(dump.setup_order(["b", "a", "shared"], defs)) == ["shared", "b", "a"]


def test_an_override_that_requests_its_own_name_sets_up_the_parent_first():
    parent = _fd("client", ["db"])
    child = _fd("client", ["client", "flavor"])
    defs = {"client": (parent, child), "db": (_fd("db"),), "flavor": (_fd("flavor"),)}
    order = dump.setup_order(["client"], defs)
    assert [(fd.argname, fd.argnames) for fd in order] == [
        ("db", ()),
        ("client", ("db",)),
        ("flavor", ()),
        ("client", ("client", "flavor")),
    ]


def test_request_and_unknown_names_are_not_fixtures():
    assert dump.setup_order(["request", "n"], {}) == []
    only = _fd("x", ["request"])
    assert _names(dump.setup_order(["x"], {"x": (only,)})) == ["x"]


def test_closure_of_prefers_the_private_fixtureinfo_and_falls_back_to_public_api():
    db = _fd("db")
    private = SimpleNamespace(
        _fixtureinfo=SimpleNamespace(names_closure=["db"], name2fixturedefs={"db": (db,)})
    )
    assert dump.closure_of(private) == (["db"], {"db": (db,)})

    manager = SimpleNamespace(getfixturedefs=lambda name, node: [db] if name == "db" else None)
    public = SimpleNamespace(
        fixturenames=["db", "request"], session=SimpleNamespace(_fixturemanager=manager)
    )
    assert dump.closure_of(public) == (["db", "request"], {"db": (db,)})


def test_location_is_root_relative_posix_inside_the_root_and_absolute_outside(tmp_path):
    here = Path(__file__).resolve()
    fd = _fd("x")
    inside, line = dump._location(fd.func, here.parent)
    assert inside == "test_pytest_dump_plugin.py" and line > 0
    outside, _ = dump._location(fd.func, tmp_path)
    assert outside == here.as_posix()


def test_documents_share_one_closure_between_items_with_the_same_setup(tmp_path):
    db = _fd("db", scope="session")
    items = [
        SimpleNamespace(
            nodeid=f"t.py::test_{i}",
            _fixtureinfo=SimpleNamespace(names_closure=["db"], name2fixturedefs={"db": (db,)}),
        )
        for i in range(3)
    ]
    doc = dump.build_document(items, Path(__file__).parent, 0, "9.9")
    assert doc["schema"] == "codeterrain.order/1"
    assert len(doc["closures"]) == 1 and set(doc["nodes"].values()) == {0}
    assert doc["closures"][0][0]["name"] == "db" and doc["closures"][0][0]["scope"] == "session"


def _fd_in(name, filename, line=1):
    namespace: dict = {}
    code = "\n" * (line - 1) + "def func():\n    return None\n"
    exec(compile(code, filename, "exec"), namespace)  # noqa: S102
    return SimpleNamespace(
        argname=name, argnames=(), scope="function", func=namespace["func"], params=None
    )


def _item(nodeid, *defs):
    return SimpleNamespace(
        nodeid=nodeid,
        _fixtureinfo=SimpleNamespace(
            names_closure=[d.argname for d in defs],
            name2fixturedefs={d.argname: (d,) for d in defs},
        ),
    )


def test_same_fixture_names_defined_in_different_files_or_lines_do_not_share_a_closure(tmp_path):
    items = [
        _item("a", _fd_in("client", str(tmp_path / "conftest.py"))),
        _item("b", _fd_in("client", str(tmp_path / "sub" / "conftest.py"))),
        _item("c", _fd_in("client", str(tmp_path / "conftest.py"), line=9)),
        _item("d", _fd_in("client", str(tmp_path / "conftest.py"))),
    ]
    doc = dump.build_document(items, tmp_path, 0, "9.9")
    assert len(doc["closures"]) == 3
    assert doc["nodes"]["a"] == doc["nodes"]["d"]
    assert len({doc["nodes"][n] for n in "abc"}) == 3
    assert doc["closures"][doc["nodes"]["b"]][0]["file"] == "sub/conftest.py"
    assert doc["closures"][doc["nodes"]["c"]][0]["line"] == 9


class _Config:
    def __init__(self, **options):
        self.options = options

    def getoption(self, name, default=None):
        return self.options.get(name, default)


@pytest.mark.parametrize(
    ("options", "flag"),
    [
        ({"xmlpath": "out.xml"}, "--junitxml"),
        ({"htmlpath": "r.html"}, "--html"),
        ({"cov_report": {"html": None}}, "--cov-report"),
        ({"cov_report": {"term": None, "xml:cov.xml": None}}, "--cov-report"),
        ({"cov_report": ["json"]}, "--cov-report"),
        ({"log_file": "pytest.log"}, "--log-file"),
        ({"debug": "pytestdebug.log"}, "--debug"),
        ({"report_log": "events.jsonl"}, "--report-log"),
        ({"json_report": True}, "--json-report"),
        ({"allure_report_dir": "allure"}, "--alluredir"),
    ],
)
def test_respect_addopts_refuses_options_that_write_report_files(monkeypatch, options, flag):
    monkeypatch.setenv("CODETERRAIN_RESPECT_ADDOPTS", "1")
    with pytest.raises(pytest.UsageError, match=f"{flag}.*respect_addopts = false"):
        dump.pytest_configure(_Config(**options))


@pytest.mark.parametrize(
    "options",
    [{}, {"cov_report": {}}, {"cov_report": {"term-missing": None}}, {"cov_report": ["term"]},
     {"xmlpath": None, "htmlpath": ""}, {"debug": False, "log_file": None, "json_report": False}],
)
def test_respect_addopts_allows_runs_without_report_files(monkeypatch, options):
    monkeypatch.setenv("CODETERRAIN_RESPECT_ADDOPTS", "1")
    dump.pytest_configure(_Config(**options))


def test_the_refusal_also_runs_on_the_early_namespace_before_pytest_opens_its_files(
    monkeypatch,
):
    monkeypatch.setenv("CODETERRAIN_RESPECT_ADDOPTS", "1")
    early = SimpleNamespace(known_args_namespace=SimpleNamespace(debug="pytestdebug.log"))
    with pytest.raises(pytest.UsageError, match="--debug"):
        dump.pytest_load_initial_conftests(early, None, [])
    dump.pytest_load_initial_conftests(
        SimpleNamespace(known_args_namespace=SimpleNamespace(debug=False)), None, []
    )
    dump.pytest_load_initial_conftests(SimpleNamespace(), None, [])


def test_the_refusal_applies_to_pytest_args_without_respect_addopts_too(monkeypatch):
    # Security review P3-6: a user's pytest_args must not reopen the writers either.
    monkeypatch.delenv("CODETERRAIN_RESPECT_ADDOPTS", raising=False)
    with pytest.raises(pytest.UsageError, match="--junitxml.*remove them from pytest_args"):
        dump.pytest_configure(_Config(xmlpath="out.xml"))
    dump.pytest_configure(_Config())


def test_an_ini_log_file_set_by_an_override_is_refused_early_and_late(monkeypatch):
    monkeypatch.delenv("CODETERRAIN_RESPECT_ADDOPTS", raising=False)
    config = _Config()
    config.getini = lambda name: "x.log" if name == "log_file" else ""
    with pytest.raises(pytest.UsageError, match="log_file"):
        dump.pytest_configure(config)
    early = SimpleNamespace(known_args_namespace=SimpleNamespace(), getini=config.getini)
    with pytest.raises(pytest.UsageError, match="log_file"):
        dump.pytest_load_initial_conftests(early, None, [])
    config.getini = lambda name: ""
    dump.pytest_configure(config)


def test_a_dependency_only_the_overridden_parent_requests_is_resolved_through_the_manager():
    shared = _fd("shared", scope="session")
    parent = _fd("client", ["shared"])
    child = _fd("client", ["client"])
    names, defs = ["client"], {"client": (parent, child)}
    assert _names(dump.setup_order(names, defs)) == ["client", "client"]
    resolved = dump.setup_order(names, defs, lambda name: (shared,) if name == "shared" else ())
    assert _names(resolved) == ["shared", "client", "client"]


def test_documents_ask_the_fixture_manager_for_names_a_pytest_8_closure_leaves_out(tmp_path):
    shared = _fd_in("shared", str(tmp_path / "conftest.py"))
    parent = _fd_in("client", str(tmp_path / "conftest.py"), line=4)
    parent.argnames = ("shared",)
    child = _fd_in("client", str(tmp_path / "sub" / "conftest.py"))
    child.argnames = ("client",)
    asked = []

    def getfixturedefs(name, node):
        asked.append(name)
        return [shared] if name == "shared" else None

    item = SimpleNamespace(
        nodeid="t",
        session=SimpleNamespace(_fixturemanager=SimpleNamespace(getfixturedefs=getfixturedefs)),
        _fixtureinfo=SimpleNamespace(
            names_closure=["client"], name2fixturedefs={"client": (parent, child)}
        ),
    )
    doc = dump.build_document([item], tmp_path, 0, "8.4")
    assert [fx["name"] for fx in doc["closures"][0]] == ["shared", "client", "client"]
    assert asked == ["shared"]


def test_an_override_reaching_its_own_name_through_a_helper_still_gets_the_parent():
    outer = _fd("foo")
    helper = _fd("helper", ["foo"])
    override = _fd("foo", ["helper"])
    defs = {"foo": (outer, override), "helper": (helper,)}
    order = dump.setup_order(["foo"], defs)
    assert [(fd.argname, fd.argnames) for fd in order] == [
        ("foo", ()),
        ("helper", ("foo",)),
        ("foo", ("helper",)),
    ]


def test_three_levels_of_override_each_reach_the_next_one_up_through_their_helpers():
    outermost = _fd("foo")
    helper2, outer = _fd("helper2", ["foo"]), _fd("foo", ["helper2"])
    helper, override = _fd("helper", ["foo"]), _fd("foo", ["helper"])
    defs = {
        "foo": (outermost, outer, override),
        "helper": (helper,),
        "helper2": (helper2,),
    }
    order = dump.setup_order(["foo"], defs)
    assert [(fd.argname, fd.argnames) for fd in order] == [
        ("foo", ()),
        ("helper2", ("foo",)),
        ("foo", ("helper2",)),
        ("helper", ("foo",)),
        ("foo", ("helper",)),
    ]


def test_a_request_for_a_name_beyond_its_last_override_level_is_left_out():
    only = _fd("foo", ["helper"])
    helper = _fd("helper", ["foo"])
    order = dump.setup_order(["foo"], {"foo": (only,), "helper": (helper,)})
    assert _names(order) == ["helper", "foo"]


def test_a_name_that_finished_is_not_resolved_again_by_a_later_request():
    outer, override = _fd("foo"), _fd("foo", ["foo"])
    later = _fd("later", ["foo"])
    defs = {"foo": (outer, override), "later": (later,)}
    order = dump.setup_order(["foo", "later"], defs)
    assert [(fd.argname, fd.argnames) for fd in order] == [
        ("foo", ()),
        ("foo", ("foo",)),
        ("later", ("foo",)),
    ]
