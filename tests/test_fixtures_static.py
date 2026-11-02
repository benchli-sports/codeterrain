from __future__ import annotations

import textwrap

import pytest

from codeterrain import fixtures_static
from codeterrain.fixtures_static import (
    build,
    dependents,
    normalise_scope,
    parse_source,
    scope_letter,
    select,
)


def _fixture(source: str) -> dict:
    facts = parse_source(textwrap.dedent(source))
    assert len(facts["fixtures"]) == 1
    return facts["fixtures"][0]


@pytest.mark.parametrize(
    ("decorator", "scope", "autouse", "params"),
    [
        ("@pytest.fixture", "function", False, 0),
        ("@pytest.fixture()", "function", False, 0),
        ('@pytest.fixture(scope="session", autouse=True)', "session", True, 0),
        ('@fixture(scope="module")', "module", False, 0),
        ("@pytest_asyncio.fixture(autouse=True, scope='class')", "class", True, 0),
        ("@pytest.fixture(params=[1, 2, 3])", "function", False, 3),
        ("@pytest.fixture(params=make_params())", "function", False, -1),
        ("@pytest.fixture(scope=pick_scope)", "dynamic", False, 0),
        ("@pytest.fixture(autouse=ENABLED)", "function", False, 0),
    ],
)
def test_fixture_decorator_forms(decorator, scope, autouse, params):
    fx = _fixture(f"{decorator}\ndef thing(): ...\n")
    assert (fx["scope"], fx["autouse"], fx["params"]) == (scope, autouse, params)
    assert fx["name"] == "thing"


def test_name_keyword_renames_and_decorated_line_is_the_first_decorator():
    fx = _fixture(
        """
        import pytest

        @pytest.fixture(
            name="public",
        )
        def _private(): ...
        """
    )
    assert (fx["name"], fx["func"], fx["line"]) == ("public", "_private", 4)


def test_argnames_are_the_fixtures_pytest_would_inject():
    fx = _fixture(
        """
        class Holder:
            @pytest.fixture
            def f(self, a, b=1, *args, c, d=2, **kw): ...
        """
    )
    assert fx["argnames"] == ["a", "c"]
    assert fx["cls"] == "Holder"


def test_tests_ignore_parametrize_names_unless_indirect_and_merge_every_usefixtures_source():
    facts = parse_source(
        textwrap.dedent(
            """
            import pytest

            pytestmark = pytest.mark.usefixtures("from_module")

            @pytest.mark.parametrize("n, m", [(1, 2)])
            @pytest.mark.parametrize("viaindirect", [1], indirect=True)
            @pytest.mark.usefixtures("from_decorator")
            def test_a(db, n, m, viaindirect, request):
                request.getfixturevalue("late")
                request.getfixturevalue(computed_name)

            @pytest.mark.usefixtures("from_class")
            class TestB:
                pytestmark = [pytest.mark.usefixtures("from_class_mark")]

                def test_b(self, db): ...

                def helper(self): ...

            class NotATest:
                def test_c(self): ...
            """
        )
    )
    by_name = {t["name"]: t for t in facts["tests"]}
    assert set(by_name) == {"test_a", "TestB::test_b"}
    a = by_name["test_a"]
    assert a["argnames"] == ["db", "viaindirect", "request"]
    assert a["usefixtures"] == ["from_decorator", "from_module"]
    assert a["dyn"] == ["late"]
    b = by_name["TestB::test_b"]
    assert b["usefixtures"] == ["from_class", "from_class_mark", "from_module"]


def test_syntax_error_is_recorded_not_raised():
    facts = parse_source("def broken(:\n")
    assert facts["fixtures"] == [] and "SyntaxError" in facts["error"]


@pytest.mark.parametrize(
    ("text", "word"),
    [("S", "session"), ("m", "module"), ("function", "function"), ("Package", "package")],
)
def test_normalise_scope(text, word):
    assert normalise_scope(text) == word
    assert scope_letter(word) == word[0].upper()


def test_normalise_scope_refuses_unknown():
    with pytest.raises(ValueError, match="unknown scope"):
        normalise_scope("global")


def _by(data, name, file=None):
    rows = [f for f in data["fixtures"] if f["name"] == name and (not file or f["file"] == file)]
    assert len(rows) == 1, rows
    return rows[0]


def test_sample_repo_facts(sample_cache, sample_config):
    record, rebuilt = build(sample_cache, sample_config)
    data = record.data
    assert rebuilt and data["errors"] == []
    env = _by(data, "_env_session")
    assert (env["scope"], env["autouse"], env["argnames"]) == ("session", True, [])
    assert _by(data, "db")["argnames"] == ["_env_session"]
    assert _by(data, "flavor")["params"] == 2
    assert _by(data, "renamed")["func"] == "_renamed_fixture"
    assert _by(data, "thing")["scope"] == "class"
    assert _by(data, "_api_module_marker")["scope"] == "module"


def test_overrides_point_at_the_nearest_shadowed_conftest_fixture(sample_cache, sample_config):
    data = build(sample_cache, sample_config)[0].data
    base_client = _by(data, "client", "tests/conftest.py")
    sub_client = _by(data, "client", "tests/api/conftest.py")
    assert base_client["overrides"] == ""
    assert sub_client["overrides"] == f"tests/conftest.py:{base_client['line']}"
    local = _by(data, "mod_cache", "tests/test_root.py")
    parent = _by(data, "mod_cache", "tests/conftest.py")
    assert local["overrides"] == f"tests/conftest.py:{parent['line']}"


def test_select_filters(sample_cache, sample_config):
    data = build(sample_cache, sample_config)[0].data
    assert {f["name"] for f in select(data, "client")} == {"client"}
    assert {f["name"] for f in select(data, autouse=True)} == {
        "_env_session",
        "_reset_state",
        "_clock",
        "_api_module_marker",
    }
    assert {f["name"] for f in select(data, scope="session")} == {"_env_session", "db"}
    assert select(data, "CLIENT") == select(data, "client")


def test_dependents_cover_args_usefixtures_and_getfixturevalue(sample_cache, sample_config):
    data = build(sample_cache, sample_config)[0].data
    renamed = {(d["name"], d["via"]) for d in dependents(data, "renamed")}
    assert renamed == {("test_marked", "usefixtures"), ("test_root", "arg")}
    mod_cache = {(d["name"], d["via"]) for d in dependents(data, "mod_cache")}
    assert ("test_dynamic", "getfixturevalue") in mod_cache
    assert ("thing", "arg") in mod_cache and ("_api_module_marker", "arg") in mod_cache
    assert dependents(data, "does_not_exist") == []


def test_a_signed_view_is_not_checked_again_and_an_edited_one_is(
    sample_cache, sample_config, sample_copy, cache_dir, monkeypatch
):
    from codeterrain.cache import Cache

    assert build(sample_cache, sample_config)[0].verified
    checked = []
    real = fixtures_static._valid_view
    monkeypatch.setattr(fixtures_static, "_valid_view", lambda d: checked.append(1) or real(d))
    record, rebuilt = build(Cache(sample_copy, cache_dir), sample_config)
    assert record.verified and not rebuilt and checked == []
    path = next((cache_dir / "views").glob("fixtures-*.json"))
    path.write_bytes(path.read_bytes().replace(b'"fixtures-1"', b'"fixtures-1" '))
    record, rebuilt = build(Cache(sample_copy, cache_dir), sample_config)
    assert checked == [1] and not rebuilt  # unsigned now: checked, and still valid


def test_warm_build_reads_the_cache_and_an_edit_reparses_only_that_file(
    sample_cache, sample_config, sample_copy, cache_dir, monkeypatch
):
    from codeterrain.cache import Cache

    build(sample_cache, sample_config)
    calls: list[str] = []
    real = fixtures_static.parse_source
    monkeypatch.setattr(
        fixtures_static, "parse_source", lambda text: calls.append(text) or real(text)
    )

    record, rebuilt = build(Cache(sample_copy, cache_dir), sample_config)
    assert not rebuilt and calls == []

    target = sample_copy / "tests" / "conftest.py"
    target.write_text(
        target.read_text(encoding="utf-8") + "\n\n@pytest.fixture\ndef added():\n    return 1\n",
        encoding="utf-8",
        newline="\n",
    )
    record, rebuilt = build(Cache(sample_copy, cache_dir), sample_config)
    assert rebuilt and len(calls) == 1
    assert "added" in {f["name"] for f in record.data["fixtures"]}

    (sample_copy / "tests" / "test_root.py").unlink()
    record, _ = build(Cache(sample_copy, cache_dir), sample_config)
    assert "test_root" not in {t["name"] for t in record.data["tests"]}
    assert len(calls) == 1


def test_partial_indirect():
    facts = parse_source(
        textwrap.dedent(
            """
            import pytest

            @pytest.mark.parametrize("a, b, c", [(1, 2, 3)], indirect=["b"])
            def test_listed(a, b, c): ...

            @pytest.mark.parametrize("a, b", [(1, 2)], indirect=True)
            def test_all(a, b): ...

            @pytest.mark.parametrize("a, b", [(1, 2)], indirect=False)
            def test_none(a, b): ...

            @pytest.mark.parametrize("a, b", [(1, 2)], indirect=("a",))
            def test_tuple(a, b): ...

            @pytest.mark.parametrize("a, b", [(1, 2)], indirect=SOME_NAMES)
            def test_unknown(a, b): ...
            """
        )
    )
    args = {t["name"]: t["argnames"] for t in facts["tests"]}
    assert args == {
        "test_listed": ["b"],
        "test_all": ["a", "b"],
        "test_none": [],
        "test_tuple": ["a"],
        "test_unknown": ["a", "b"],
    }


def test_cached_facts_missing_a_field_are_reparsed_not_a_key_error(
    sample_cache, sample_config, sample_copy, cache_dir
):
    import json

    from codeterrain.cache import Cache

    build(sample_cache, sample_config)
    path = cache_dir / "objects-static.json"
    raw = json.loads(path.read_bytes())
    for facts in raw["objects"].values():
        for fx in facts["fixtures"]:
            fx.pop("dyn", None)
        for test in facts["tests"]:
            test.pop("dyn", None)
    path.write_bytes(json.dumps(raw).encode("utf-8"))
    view = cache_dir / "views" / f"fixtures-{sample_cache.root_id}.json"
    view_raw = json.loads(view.read_bytes())
    view_raw.pop("built_at")
    view.write_bytes(json.dumps(view_raw).encode("utf-8"))

    record, rebuilt = build(Cache(sample_copy, cache_dir), sample_config)
    assert rebuilt
    assert all("dyn" in fx for fx in record.data["fixtures"])
    assert len(record.data["fixtures"]) == 12


def test_a_cached_view_without_the_expected_lists_is_rebuilt(
    sample_cache, sample_config, sample_copy, cache_dir
):
    from codeterrain.cache import Cache

    record, _ = build(sample_cache, sample_config)
    sample_cache.write_view("fixtures", {"fixtures": []}, record.fingerprint)
    again, rebuilt = build(Cache(sample_copy, cache_dir), sample_config)
    assert rebuilt and len(again.data["fixtures"]) == 12


FIXTURE_KEYS = ("name", "func", "scope", "autouse", "params", "argnames", "line", "cls", "dyn")
TEST_KEYS = ("name", "line", "argnames", "usefixtures", "dyn", "parametrized")
WRONG = {
    "name": 5, "func": 5, "scope": 5, "autouse": "yes", "params": "3", "argnames": "x",
    "line": "1", "cls": 5, "dyn": "x", "usefixtures": "x", "parametrized": "x",
}


def _damage(kind, facts, key=None):
    """One way for a cached per-file entry (or a view's data) to be wrong."""
    if kind == "delete-fixture-key":
        for fx in facts["fixtures"]:
            fx.pop(key, None)
    elif kind == "wrong-fixture-type":
        for fx in facts["fixtures"]:
            fx[key] = WRONG[key]
    elif kind == "delete-test-key":
        for test in facts["tests"]:
            test.pop(key, None)
    elif kind == "wrong-test-type":
        for test in facts["tests"]:
            test[key] = WRONG[key]
    elif kind == "list-of-ints":
        for fx in facts["fixtures"]:
            fx["argnames"] = [1, 2]
    elif kind == "bool-line":
        for test in facts["tests"]:
            test["line"] = True
    else:
        facts[kind] = "oops"


DAMAGES = (
    [("delete-fixture-key", k) for k in FIXTURE_KEYS]
    + [("wrong-fixture-type", k) for k in FIXTURE_KEYS]
    + [("delete-test-key", k) for k in TEST_KEYS]
    + [("wrong-test-type", k) for k in TEST_KEYS]
    + [("list-of-ints", None), ("bool-line", None), ("fixtures", None), ("tests", None)]
)


@pytest.mark.parametrize(("kind", "key"), DAMAGES, ids=[f"{k}-{v}" for k, v in DAMAGES])
def test_cached_per_file_facts_of_the_wrong_shape_or_type_are_reparsed_never_an_error(
    sample_cache, sample_config, sample_copy, cache_dir, kind, key
):
    import json

    from codeterrain.cache import Cache

    baseline, _ = build(sample_cache, sample_config)
    path = cache_dir / "objects-static.json"
    raw = json.loads(path.read_bytes())
    for facts in raw["objects"].values():
        _damage(kind, facts, key)
    path.write_bytes(json.dumps(raw).encode("utf-8"))
    view = cache_dir / "views" / f"fixtures-{sample_cache.root_id}.json"
    view_raw = json.loads(view.read_bytes())
    view_raw["fingerprint"] = {}
    view.write_bytes(json.dumps(view_raw).encode("utf-8"))

    record, rebuilt = build(Cache(sample_copy, cache_dir), sample_config)
    assert rebuilt and record.data == baseline.data


@pytest.mark.parametrize(
    "damage",
    [
        lambda d: d.pop("errors"),
        lambda d: d.update(fixtures="oops"),
        lambda d: d["fixtures"][0].pop("file"),
        lambda d: d["fixtures"][0].pop("overrides"),
        lambda d: d["fixtures"][0].update(line="1"),
        lambda d: d["tests"][0].pop("file"),
        lambda d: d["tests"][0].update(argnames="x"),
        lambda d: d.update(errors=[{"file": "a.py"}]),
        lambda d: d.update(tests=[None]),
    ],
    ids=["no-errors", "fixtures-str", "fixture-no-file", "fixture-no-overrides",
         "fixture-line-str", "test-no-file", "test-argnames-str", "error-no-text", "test-none"],
)
def test_a_cached_view_with_damaged_entries_is_rebuilt(
    sample_cache, sample_config, sample_copy, cache_dir, damage
):
    import copy

    from codeterrain.cache import Cache

    baseline, _ = build(sample_cache, sample_config)
    broken = copy.deepcopy(baseline.data)
    damage(broken)
    sample_cache.write_view("fixtures", broken, baseline.fingerprint)
    again, rebuilt = build(Cache(sample_copy, cache_dir), sample_config)
    assert rebuilt and again.data == baseline.data


def _inherited_source(source, indirect):
    mark = f'pytest.mark.parametrize("x", [2]{", indirect=True" if indirect else ""})'
    head = "import pytest\n\n"
    methods = "    def test_m(self, x, y): ...\n"
    return head + {
        "class-decorator": f"@{mark}\nclass TestK:\n{methods}",
        "class-pytestmark": f"class TestK:\n    pytestmark = {mark}\n\n{methods}",
        "class-pytestmark-list": (
            f'class TestK:\n    pytestmark = [pytest.mark.usefixtures("u"), {mark}]\n\n{methods}'
        ),
        "module-list": (
            f'pytestmark = [pytest.mark.usefixtures("u"), {mark}]\n\n'
            f"def test_f(x, y): ...\n\nclass TestK:\n{methods}"
        ),
        "module-single": f"pytestmark = {mark}\n\ndef test_f(x, y): ...\n\nclass TestK:\n{methods}",
    }[source]


@pytest.mark.parametrize("indirect", [False, True], ids=["direct", "indirect"])
@pytest.mark.parametrize(
    "source",
    ["class-decorator", "class-pytestmark", "class-pytestmark-list", "module-list",
     "module-single"],
)
def test_parametrization_inherited_from_a_class_or_module_mark_supplies_the_argument(
    source, indirect
):
    facts = parse_source(_inherited_source(source, indirect))
    expected = ["x", "y"] if indirect else ["y"]
    assert [t["argnames"] for t in facts["tests"]] == [expected] * len(facts["tests"])
    assert len(facts["tests"]) == (2 if source.startswith("module") else 1)
    wanted_use = ["u"] if source.endswith("list") else []
    assert all(t["usefixtures"] == wanted_use for t in facts["tests"])


def test_an_inherited_parametrize_means_the_fixture_of_that_name_has_no_such_dependent():
    source = (
        "import pytest\n\n\n@pytest.fixture\ndef x(): ...\n\n\n"
        '@pytest.mark.parametrize("x", [2])\nclass TestK:\n    def test_m(self, x): ...\n'
        '\n\n@pytest.mark.parametrize("x", [2], indirect=True)\nclass TestI:\n'
        "    def test_i(self, x): ...\n"
    )
    facts = parse_source(source)
    data = {
        "fixtures": [{**f, "file": "t.py"} for f in facts["fixtures"]],
        "tests": [{**t, "file": "t.py"} for t in facts["tests"]],
    }
    assert [d["name"] for d in dependents(data, "x")] == ["TestI::test_i"]


def _dependents_of_x(source):
    facts = parse_source(source)
    data = {
        "fixtures": [{**f, "file": "t.py"} for f in facts["fixtures"]],
        "tests": [{**t, "file": "t.py"} for t in facts["tests"]],
    }
    return [d["name"] for d in dependents(data, "x")]


_KEYWORD_HEAD = "import pytest\n\n\n@pytest.fixture\ndef x(): ...\n\n\n"


def test_tests_and_fixtures_in_nested_test_classes_are_found_with_inherited_marks():
    # pytest collects TestOuter::TestInner::test_nested and runs its
    # fixtures, but the static facts skipped the inner class, so `terrain fixture` missed it.
    source = (
        "import pytest\n\n\n"
        '@pytest.mark.usefixtures("outer_use")\n'
        "class TestOuter:\n"
        '    @pytest.mark.parametrize("p", [1])\n'
        "    class TestInner:\n"
        "        @pytest.fixture\n"
        "        def inner_fx(self): ...\n\n"
        "        def test_nested(self, db, p): ...\n\n"
        "        class TestDeeper:\n"
        "            def test_deep(self, db, p): ...\n\n"
        "    class Helper:\n"
        "        def test_not_collected(self, db): ...\n\n\n"
        "class Plain:\n"
        "    class TestInside:\n"
        "        def test_not_collected(self, db): ...\n"
    )
    facts = parse_source(source)
    tests = {t["name"]: t for t in facts["tests"]}
    assert sorted(tests) == [
        "TestOuter::TestInner::TestDeeper::test_deep",
        "TestOuter::TestInner::test_nested",
    ]
    nested = tests["TestOuter::TestInner::test_nested"]
    assert nested["argnames"] == ["db"] and nested["usefixtures"] == ["outer_use"]
    assert [(f["name"], f["cls"]) for f in facts["fixtures"]] == [
        ("inner_fx", "TestOuter::TestInner")
    ]


def test_a_usefixtures_name_that_is_directly_parametrized_is_not_a_dependency():
    # pytest supplies the parameter instead of running the same-named
    # fixture, but usefixtures("x") still listed the test as a dependent of x, from the test's own
    # marks and from inherited module and class marks alike.
    source = (
        _KEYWORD_HEAD
        + '@pytest.mark.usefixtures("x")\n@pytest.mark.parametrize("x", [1])\n'
        + "def test_own(x): ...\n\n\n"
        + '@pytest.mark.usefixtures("x")\nclass TestC:\n'
        + '    @pytest.mark.parametrize("x", [1])\n    def test_inherited(self): ...\n\n\n'
        + '@pytest.mark.usefixtures("x")\n@pytest.mark.parametrize("x", [1], indirect=True)\n'
        + "def test_indirect(x): ...\n\n\n"
        + '@pytest.mark.usefixtures("x")\ndef test_plain(): ...\n'
    )
    assert _dependents_of_x(source) == ["test_indirect", "test_plain"]


@pytest.mark.parametrize(
    ("mark", "dependent"),
    [
        ('argnames="x", argvalues=[2]', False),
        ('argvalues=[2], argnames="x"', False),
        ('argnames=["x", "y"], argvalues=[(1, 2)]', False),
        ('argnames=("x", "y"), argvalues=[(1, 2)]', False),
        ('argnames="x, y", argvalues=[(1, 2)]', False),
        ('argnames="x", argvalues=[2], indirect=True', True),
        ('argnames="x", argvalues=[2], indirect=["x"]', True),
        ('argnames=["x", "y"], argvalues=[(1, 2)], indirect=["y"]', False),
        ('argnames="y", argvalues=[2]', True),
    ],
    ids=["str", "reordered", "list", "tuple", "comma", "indirect-true", "indirect-list",
         "indirect-other-name", "other-name"],
)
def test_a_parametrize_with_keyword_argnames_supplies_the_argument(mark, dependent):
    source = _KEYWORD_HEAD + f"@pytest.mark.parametrize({mark})\ndef test_t(x, y=0): ...\n"
    assert (_dependents_of_x(source) == ["test_t"]) is dependent


@pytest.mark.parametrize(
    ("mark", "dependent"),
    [
        ('"x", [2], True', True),
        ('"x", [2], ["x"]', True),
        ('"x", [2], ("x",)', True),
        ('"x, y", [(1, 2)], ["y"]', False),
        ('"x", [2], False', False),
        ('"x", [2]', False),
    ],
    ids=["true", "list", "tuple", "other-name", "false", "absent"],
)
def test_a_positional_indirect_argument_sends_the_name_through_the_fixture(mark, dependent):
    # `parametrize("x", [2], True)` is indirect, so the test does run
    # fixture x and must stay listed as a dependent.
    source = _KEYWORD_HEAD + f"@pytest.mark.parametrize({mark})\ndef test_t(x, y=0): ...\n"
    assert (_dependents_of_x(source) == ["test_t"]) is dependent


def test_a_class_level_keyword_argnames_parametrize_is_inherited_by_its_methods():
    source = (
        _KEYWORD_HEAD
        + '@pytest.mark.parametrize(argnames="x", argvalues=[2])\nclass TestK:\n'
        + "    def test_m(self, x): ...\n"
        + '\n\n@pytest.mark.parametrize(argnames="x", argvalues=[2], indirect=True)\n'
        + "class TestI:\n    def test_i(self, x): ...\n"
        + "\n\nclass TestP:\n"
        + '    pytestmark = pytest.mark.parametrize(argnames="x", argvalues=[2])\n\n'
        + "    def test_p(self, x): ...\n"
    )
    assert _dependents_of_x(source) == ["TestI::test_i"]


@pytest.mark.parametrize(
    "source",
    ["x = " + " + ".join(['"a"'] * 100_000) + "\n", "x = " + "-" * 200000 + "1\n"],
    ids=["deep-concatenation", "deep-unary"],
)
def test_a_file_that_exhausts_the_parser_is_a_recorded_error(source):
    # Security review P3-2: RecursionError / MemoryError from ast.parse crashed the whole tier.
    facts = parse_source(source)
    assert facts["fixtures"] == [] and facts["error"].startswith(("RecursionError", "MemoryError"))


def test_marks_are_kept_per_module_class_and_test_method():
    # What a subclass in another file needs to give an inherited test its marks: each thing's
    # own marks, not merged; plain (uncollected) classes included.
    facts = parse_source(textwrap.dedent("""
        import pytest
        pytestmark = pytest.mark.usefixtures("db")

        @pytest.mark.parametrize("n", [1])
        class Contract:
            @pytest.mark.usefixtures("resource")
            def test_it(self, n):
                pass

            def helper(self):
                pass

        class TestOuter:
            pytestmark = [pytest.mark.usefixtures("outer")]

            class TestInner:
                def test_plain(self):
                    pass
    """))
    assert facts["marks"] == [
        {"name": "", "use": ["db"], "direct": []},
        {"name": "Contract", "use": [], "direct": ["n"]},
        {"name": "Contract::test_it", "use": ["resource"], "direct": []},
        {"name": "TestOuter", "use": ["outer"], "direct": []},
    ]


def test_unlisted_test_trees_names_top_level_directories_the_view_does_not_read(tmp_path):
    from codeterrain.config import load_config

    def touch(rel):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

    touch("tests/conftest.py")
    touch("tests_live/conftest.py")  # a second tree with its own conftest chain
    touch("e2e/test_flow.py")  # test modules without a conftest
    touch("app/main.py")  # code, not tests
    touch("deep/pkg/test_x.py")  # only the directory's own entries are looked at
    touch(".hidden/conftest.py")
    touch("node_modules/conftest.py")
    config = load_config(tmp_path)
    assert fixtures_static.unlisted_test_trees(config) == ["e2e", "tests_live"]
    (tmp_path / "codeterrain.toml").write_text(
        'test_paths = ["tests", "tests_live", "e2e"]\n', encoding="utf-8"
    )
    assert fixtures_static.unlisted_test_trees(load_config(tmp_path)) == []
    (tmp_path / "codeterrain.toml").write_text('test_paths = ["."]\n', encoding="utf-8")
    assert fixtures_static.unlisted_test_trees(load_config(tmp_path)) == []
