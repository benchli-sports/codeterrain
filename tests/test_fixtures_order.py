from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap

import pytest

from codeterrain import fixtures_order
from codeterrain.config import Config
from codeterrain.fixtures_order import ExtractError, extract, groups, match_nodes, scope_letter
from codeterrain.protocol import ORDER_SCHEMA, ProtocolError
from codeterrain.sandbox import SandboxResult

NODES = [
    "tests/api/test_items.py::test_get_item[a]",
    "tests/api/test_items.py::test_get_item[b]",
    "tests/api/test_items.py::TestThings::test_one",
    "tests/api/test_items.py::test_marked",
    "tests/test_root.py::test_root",
]


@pytest.fixture(scope="module")
def doc(warm_sample):
    repo, cache = warm_sample
    from codeterrain.cache import Cache

    record = Cache(repo, cache).read_view("order")
    assert record is not None
    return record.data


def _pytest(repo, nodeid, flag):
    return subprocess.run(
        [sys.executable, "-m", "pytest", nodeid, flag, "-p", "no:xdist",
         "-p", "no:cacheprovider", "-o", "log_cli=false", "-q"],
        cwd=repo, capture_output=True, text=True, check=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    ).stdout


def located(repo, doc):
    """nodeid -> {name: (file, line)} as ``pytest --fixtures-per-test`` reports each test.

    One pytest run for the whole repo; its sections come in collection order, which is the order
    of ``doc["nodes"]``."""
    out = _pytest(repo, "tests", "--fixtures-per-test")
    sections = []
    for line in out.splitlines():
        if "fixtures used by" in line:
            sections.append({})
            continue
        match = re.match(r"(\w+) -- (.+):(\d+)$", line)
        if match and sections:
            path = match.group(2).replace("\\", "/")
            sections[-1][match.group(1)] = (path, int(match.group(3)))
    assert len(sections) == len(doc["nodes"])
    return dict(zip(doc["nodes"], sections, strict=True))


def assert_locations_match(theirs, closure):
    """Each fixture pytest locates sits where we say. pytest reports co_firstlineno + 1 (the line
    after the first decorator); the document keeps the first decorator line itself."""
    ours = {fx["name"]: (fx["file"], fx["line"] + 1) for fx in closure}
    checked = {name: loc for name, loc in theirs.items() if name in ours and ".." not in loc[0]}
    assert checked, "pytest located none of our fixtures"
    assert {name: ours[name] for name in checked} == checked


def plan(repo, nodeid):
    """(scope letter, name) pairs the first test of ``nodeid`` gets in ``pytest --setup-plan``."""
    out = _pytest(repo, nodeid, "--setup-plan")
    pairs = []
    for line in out.splitlines():
        if "(fixtures used" in line and "::" in line:
            break
        found = re.match(r"\s*SETUP\s+([SPMCF]) (\w+)", line)
        if found:
            pairs.append((found.group(1), found.group(2)))
    return pairs


@pytest.fixture(scope="module")
def sample_locations(warm_sample, doc):
    return located(warm_sample[0], doc)


@pytest.mark.parametrize("nodeid", NODES)
def test_order_equals_pytest_setup_plan(warm_sample, doc, sample_locations, nodeid):
    repo, _ = warm_sample
    ours = [
        (scope_letter(fx["scope"]), fx["name"])
        for fx in doc["closures"][doc["nodes"][nodeid]]
    ]
    assert ours == plan(repo, nodeid)
    assert len(ours) >= 4
    assert_locations_match(sample_locations[nodeid], doc["closures"][doc["nodes"][nodeid]])


def test_document_is_a_valid_order_v1_with_locations(doc):
    assert doc["schema"] == ORDER_SCHEMA and doc["errors"] == 0
    closure = doc["closures"][doc["nodes"][NODES[0]]]
    assert closure[0] == {
        "name": "_env_session", "scope": "session", "file": "tests/conftest.py", "line": 4,
        "params": False,
    }
    assert any(fx["params"] for fx in closure if fx["name"] == "flavor")


def test_match_nodes_exact_then_prefix_for_params_classes_and_files(doc):
    assert match_nodes(doc, NODES[0]) == [NODES[0]]
    assert match_nodes(doc, "tests/api/test_items.py::test_get_item") == NODES[:2]
    assert match_nodes(doc, "tests\\api\\test_items.py::TestThings") == [NODES[2]]
    assert len(match_nodes(doc, "tests/test_root.py")) == 1
    assert match_nodes(doc, "tests/api/test_items.py::test_get") == []


def test_groups_collapse_nodes_that_share_a_setup(doc):
    found = groups(doc, match_nodes(doc, "tests/api/test_items.py::test_get_item"))
    assert [g["nodes"] for g in found] == [NODES[:2]]
    mixed = groups(doc, [NODES[0], NODES[3], NODES[4]])
    assert len(mixed) == 3


def _fake_run(result, returncode=0, stderr=""):
    def run(python, args, config, cache_dir, timeout=0):
        return SandboxResult(returncode, "", stderr, result)

    return run


def test_extract_refuses_a_document_with_another_schema_version(monkeypatch, tmp_path):
    bad = {"schema": "codeterrain.order/2", "closures": [], "nodes": {}, "errors": 0}
    monkeypatch.setattr(fixtures_order, "run_python", _fake_run(bad))
    with pytest.raises(ProtocolError, match="codeterrain.order/2"):
        extract(Config(root=tmp_path), tmp_path, "python")


def test_extract_without_a_document_reports_pytest_output(monkeypatch, tmp_path):
    monkeypatch.setattr(fixtures_order, "run_python", _fake_run(None, 4, "usage: boom"))
    with pytest.raises(ExtractError, match=r"exit 4\): usage: boom"):
        extract(Config(root=tmp_path), tmp_path, "python")


def test_node_ids_and_fixture_fields_are_redacted_before_they_are_cached(monkeypatch, tmp_path):
    # Security review r2 P3-3: a parametrize id built from a pass_env value was printed and
    # cached in plaintext; the order view was never redacted.
    secret = "postgres://u:CANARY-dbpw@db.example.internal/x"
    monkeypatch.setenv("DATABASE_URL", secret)
    fixture = {"name": f"fx_{secret}", "scope": "function", "file": "tests/conftest.py", "line": 1}
    found = {
        "schema": ORDER_SCHEMA,
        "closures": [[fixture]],
        "nodes": {f"tests/test_p.py::test_db[{secret}]": 0},
        "errors": 0,
    }
    monkeypatch.setattr(fixtures_order, "run_python", _fake_run(found))
    doc = extract(Config(root=tmp_path, pass_env=("DATABASE_URL",)), tmp_path, "python")
    assert "CANARY" not in repr(doc)
    assert match_nodes(doc, "tests/test_p.py::test_db") == ["tests/test_p.py::test_db[[REDACTED]]"]


def test_collection_errors_do_not_hide_the_tests_that_did_collect(sample_copy, cache_dir):
    (sample_copy / "tests" / "test_broken.py").write_text("import not_a_module\n", encoding="utf-8")
    config = Config(root=sample_copy, test_paths=("tests",))
    found = extract(config, cache_dir, sys.executable)
    assert found["errors"] >= 1
    assert "tests/test_root.py::test_root" in found["nodes"]


def test_pytest_args_from_config_sit_between_the_fixed_flags_and_the_test_paths(
    monkeypatch, tmp_path
):
    seen = {}

    def fake(python, args, config, cache_dir, timeout=0):
        seen["args"] = args
        return SandboxResult(0, "", "", {"schema": ORDER_SCHEMA, "closures": [], "nodes": {},
                                         "errors": 0})

    monkeypatch.setattr(fixtures_order, "run_python", fake)
    config = Config(root=tmp_path, test_paths=("t1", "t2"), pytest_args=("-o", "addopts="))
    extract(config, tmp_path, "python")
    assert seen["args"][:4] == ["-m", "pytest", "--collect-only", "-q"]
    assert "no:cacheprovider" in seen["args"]
    assert seen["args"][-4:] == ["-o", "addopts=", "t1", "t2"]


SHAPES = {
    "tests/__init__.py": "",
    "tests/conftest.py": """
        import pytest

        @pytest.fixture(scope="session")
        def s_plain(): return 1

        @pytest.fixture(scope="session", autouse=True)
        def s_auto(): return 1

        @pytest.fixture(scope="package")
        def p_fix(s_plain): return 1

        @pytest.fixture(scope="module")
        def m_fix(): return 1

        @pytest.fixture(autouse=True)
        def f_auto(m_fix): return 1

        @pytest.fixture
        def f_needs_session(s_plain): return 1

        @pytest.fixture(params=[1, 2])
        def f_param(request): return request.param

        @pytest.fixture(scope="module", params=["x", "y"])
        def m_param(request): return request.param

        @pytest.fixture
        def indirect_fx(request): return request.param

        @pytest.fixture
        def client(s_plain): return 1

        @pytest.fixture
        def late(): return 1

        @pytest.fixture
        def uses_late_dynamically(request):
            return request.getfixturevalue("late")
    """,
    "tests/sub/__init__.py": "",
    "tests/sub/conftest.py": """
        import pytest

        @pytest.fixture
        def client(client, f_param): return client

        @pytest.fixture(autouse=True, scope="module")
        def sub_auto(): return 1
    """,
    "tests/sub/test_sub.py": """
        def test_override(client): pass
    """,
    "tests/test_shapes.py": """
        import pytest

        pytestmark = pytest.mark.usefixtures("p_fix")

        @pytest.fixture
        def m_fix(m_fix, f_needs_session):
            return m_fix

        @pytest.mark.parametrize("n", [1, 2])
        def test_direct(n, f_needs_session): pass

        @pytest.mark.parametrize("indirect_fx", [5], indirect=True)
        def test_indirect(indirect_fx): pass

        @pytest.mark.parametrize("indirect_fx", [6], indirect=True, scope="module")
        def test_indirect_scoped(indirect_fx): pass

        def test_builtin(tmp_path, monkeypatch, capsys): pass

        def test_mparam(m_param, f_needs_session): pass

        def test_dynamic(uses_late_dynamically): pass

        def test_order_args(f_param, client, s_plain): pass

        def test_request_only(request): pass

        class TestC:
            @pytest.fixture(autouse=True)
            def class_auto(self, m_fix): return 1

            @pytest.fixture(scope="class")
            def class_scoped(self, s_plain): return 1

            def test_one(self, class_scoped, f_param): pass
    """,
    "tests/chain/__init__.py": "",
    "tests/chain/conftest.py": """
        import pytest

        @pytest.fixture
        def foo(): return 0
    """,
    "tests/chain/test_two.py": """
        import pytest

        @pytest.fixture
        def helper(foo): return foo

        @pytest.fixture
        def foo(helper): return helper

        def test_two(foo): pass
    """,
    "tests/chain/inner/__init__.py": "",
    "tests/chain/inner/conftest.py": """
        import pytest

        @pytest.fixture
        def helper2(foo): return foo

        @pytest.fixture
        def foo(helper2): return helper2
    """,
    "tests/chain/inner/test_three.py": """
        import pytest

        @pytest.fixture
        def helper(foo): return foo

        @pytest.fixture
        def foo(helper): return helper

        def test_three(foo): pass
    """,
}


@pytest.fixture(scope="module")
def shapes(tmp_path_factory):
    """A throwaway repo of fixture shapes the sample repo lacks, and its extracted document."""
    base = tmp_path_factory.mktemp("shapes")
    repo = base / "repo"
    for rel, text in SHAPES.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text), encoding="utf-8", newline="\n")
    from codeterrain.config import resolve_root

    repo = resolve_root(repo)
    doc = extract(Config(root=repo), base / "cache", sys.executable)
    return repo, doc, located(repo, doc)


SHAPE_NODES = [
    "tests/sub/test_sub.py::test_override[1]",
    "tests/sub/test_sub.py::test_override[2]",
    "tests/test_shapes.py::test_direct[1]",
    "tests/test_shapes.py::test_indirect[5]",
    "tests/test_shapes.py::test_indirect_scoped[6]",
    "tests/test_shapes.py::test_builtin",
    "tests/test_shapes.py::test_mparam[x]",
    "tests/test_shapes.py::test_mparam[y]",
    "tests/test_shapes.py::test_dynamic",
    "tests/test_shapes.py::test_order_args[1]",
    "tests/test_shapes.py::test_order_args[2]",
    "tests/test_shapes.py::test_request_only",
    "tests/test_shapes.py::TestC::test_one[1]",
    "tests/test_shapes.py::TestC::test_one[2]",
    "tests/test_shapes.py::test_direct[2]",
    "tests/chain/test_two.py::test_two",
    "tests/chain/inner/test_three.py::test_three",
]


def test_the_shape_repo_collects_exactly_the_probed_nodes(shapes):
    _, doc, _ = shapes
    assert doc["errors"] == 0 and sorted(doc["nodes"]) == sorted(SHAPE_NODES)


#: Where the document deliberately differs from ``--setup-plan``: for an indirect parametrize with
#: ``scope=``, the plan prints the fixture's declared scope, but pytest runs it once at the given
#: scope (``request.scope == "module"``, shared by the tests of the module).
RUNTIME_SCOPE = {"tests/test_shapes.py::test_indirect_scoped[6]": {"indirect_fx": "M"}}


@pytest.mark.parametrize("nodeid", SHAPE_NODES)
def test_every_fixture_shape_matches_pytest_in_order_scope_and_location(shapes, nodeid):
    repo, doc, locations = shapes
    closure = doc["closures"][doc["nodes"][nodeid]]
    ours = [(scope_letter(fx["scope"]), fx["name"]) for fx in closure]
    runtime = RUNTIME_SCOPE.get(nodeid, {})
    assert ours == [(runtime.get(name, letter), name) for letter, name in plan(repo, nodeid)]
    assert_locations_match(locations[nodeid], closure)


def test_the_same_fixture_names_defined_in_other_files_do_not_share_a_closure(shapes):
    _, doc, _ = shapes
    sub = doc["closures"][doc["nodes"]["tests/sub/test_sub.py::test_override[1]"]]
    other = doc["closures"][doc["nodes"]["tests/test_shapes.py::test_order_args[1]"]]
    clients = [fx for closure in (sub, other) for fx in closure if fx["name"] == "client"]
    assert {fx["file"] for fx in clients} == {"tests/conftest.py", "tests/sub/conftest.py"}
    assert doc["nodes"]["tests/sub/test_sub.py::test_override[1]"] != doc["nodes"][
        "tests/test_shapes.py::test_order_args[1]"
    ]


def _capture_args(monkeypatch, tmp_path, **config):
    seen = {}

    def fake(python, args, config, cache_dir, timeout=0):
        seen["args"] = args
        return SandboxResult(0, "", "", {"schema": ORDER_SCHEMA, "closures": [], "nodes": {},
                                         "errors": 0})

    monkeypatch.setattr(fixtures_order, "run_python", fake)
    extract(Config(root=tmp_path, **config), tmp_path, "python")
    return seen["args"]


def _pairs(args):
    return list(zip(args, args[1:], strict=False))


def test_extraction_switches_the_projects_addopts_off_and_keeps_log_cli_pinned(
    monkeypatch, tmp_path
):
    pairs = _pairs(_capture_args(monkeypatch, tmp_path))
    assert ("-o", "addopts=") in pairs and ("-o", "log_cli=false") in pairs
    assert ("-o", "log_file=") in pairs


def test_respect_addopts_leaves_the_projects_options_in_force(monkeypatch, tmp_path):
    args = _capture_args(monkeypatch, tmp_path, respect_addopts=True)
    assert ("-o", "addopts=") not in _pairs(args) and ("-o", "log_cli=false") in _pairs(args)
    assert ("-o", "log_file=") in _pairs(args)


def test_a_report_option_in_the_projects_addopts_never_writes_into_the_target(
    sample_copy, cache_dir
):
    (sample_copy / "pytest.ini").write_text(
        "[pytest]\naddopts = --junitxml=report.xml\n", encoding="utf-8", newline="\n"
    )
    before = sorted(p.name for p in sample_copy.iterdir())
    found = extract(Config(root=sample_copy), cache_dir, sys.executable)
    assert "tests/test_root.py::test_root" in found["nodes"]
    assert sorted(p.name for p in sample_copy.iterdir()) == before
    with pytest.raises(ExtractError, match="--junitxml"):
        extract(Config(root=sample_copy, respect_addopts=True), cache_dir, sys.executable)
    assert sorted(p.name for p in sample_copy.iterdir()) == before


def _tree(root):
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))


INI_WRITERS = {
    "log_file": "log_file = pytest.log\nlog_file_level = DEBUG\nlog_file_mode = a\n",
    "cache_dir": "cache_dir = .pcache\n",
    "junit": "junit_family = xunit2\njunit_suite_name = suite\njunit_logging = all\n",
    "tmp_retention": "tmp_path_retention_policy = all\ntmp_path_retention_count = 9\n",
}


@pytest.mark.parametrize("respect", [False, True], ids=["default", "respect_addopts"])
@pytest.mark.parametrize("option", sorted(INI_WRITERS))
def test_an_ini_option_that_would_write_a_file_leaves_the_target_unchanged_in_pytest_ini(
    sample_copy, cache_dir, option, respect
):
    (sample_copy / "pytest.ini").write_text(
        "[pytest]\npythonpath = .\ntestpaths = tests\n" + INI_WRITERS[option],
        encoding="utf-8",
        newline="\n",
    )
    before = _tree(sample_copy)
    found = extract(Config(root=sample_copy, respect_addopts=respect), cache_dir, sys.executable)
    assert "tests/test_root.py::test_root" in found["nodes"]
    assert _tree(sample_copy) == before


@pytest.mark.parametrize("respect", [False, True], ids=["default", "respect_addopts"])
def test_an_ini_log_file_in_pyproject_leaves_the_target_unchanged(
    sample_copy, cache_dir, respect
):
    pyproject = sample_copy / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    pyproject.write_text(
        text.replace(
            'testpaths = ["tests"]\n',
            'testpaths = ["tests"]\nlog_file = "pytest.log"\nlog_file_level = "DEBUG"\n',
            1,
        ),
        encoding="utf-8",
        newline="\n",
    )
    assert "log_file" in pyproject.read_text(encoding="utf-8")
    before = _tree(sample_copy)
    found = extract(Config(root=sample_copy, respect_addopts=respect), cache_dir, sys.executable)
    assert "tests/test_root.py::test_root" in found["nodes"]
    assert _tree(sample_copy) == before


@pytest.mark.parametrize(
    ("addopts", "names"),
    [
        ("--debug", ["--debug"]),
        ("--log-file=pytest.log", ["--log-file"]),
        ("--debug --log-file=pytest.log", ["--debug", "--log-file"]),
    ],
    ids=["debug", "log-file", "both"],
)
def test_respect_addopts_refuses_debug_and_log_file_before_pytest_writes_them(
    sample_copy, cache_dir, addopts, names
):
    (sample_copy / "pytest.ini").write_text(
        f"[pytest]\naddopts = {addopts}\n", encoding="utf-8", newline="\n"
    )
    before = _tree(sample_copy)
    with pytest.raises(ExtractError) as raised:
        extract(Config(root=sample_copy, respect_addopts=True), cache_dir, sys.executable)
    assert all(name in str(raised.value) for name in names)
    assert _tree(sample_copy) == before


@pytest.mark.parametrize(
    "pytest_args",
    [("-o", "log_file=x.log"), ("-o", "log_file=x.log", "-o", "log_file_level=INFO"),
     ("--junitxml=out.xml",)],
    ids=["ini-log-file", "ini-log-file-level", "junitxml"],
)
def test_user_pytest_args_that_write_files_are_refused_and_nothing_is_written(
    sample_copy, cache_dir, pytest_args
):
    # Security review P3-6: a later -o log_file=... beat codeterrain's own -o log_file=.
    before = _tree(sample_copy)
    with pytest.raises(ExtractError, match="remove them from pytest_args"):
        extract(Config(root=sample_copy, pytest_args=pytest_args), cache_dir, sys.executable)
    assert _tree(sample_copy) == before


def test_fixture_files_stay_relative_to_the_repo_root_when_pytest_roots_in_a_subdirectory(
    sample_copy, cache_dir
):
    service = sample_copy / "svc"
    (service / "tests").mkdir(parents=True)
    (service / "pytest.ini").write_text("[pytest]\n", encoding="utf-8", newline="\n")
    (service / "tests" / "conftest.py").write_text(
        "import pytest\n\n\n@pytest.fixture\ndef thing():\n    return 1\n",
        encoding="utf-8",
        newline="\n",
    )
    (service / "tests" / "test_a.py").write_text(
        "def test_a(thing):\n    assert thing\n", encoding="utf-8", newline="\n"
    )
    doc = extract(Config(root=sample_copy, test_paths=("svc/tests",)), cache_dir, sys.executable)
    (node,) = [n for n in doc["nodes"] if n.endswith("test_a.py::test_a")]
    closure = doc["closures"][doc["nodes"][node]]
    assert [fx["file"] for fx in closure if fx["name"] == "thing"] == ["svc/tests/conftest.py"]


MARK_SOURCES = ["class-decorator", "class-pytestmark", "class-pytestmark-list", "module-list",
                "module-single"]


def _marks_repo(base):
    """One test file per (source of an inherited parametrize, indirect or not), each requesting
    a local fixture ``x`` that records that its body ran, and the mark either supplies ``x``
    directly (the fixture body is bypassed) or feeds it through the fixture."""
    from test_fixtures_static import _inherited_source

    from codeterrain.config import resolve_root

    repo = base / "repo"
    files = {}
    for source in MARK_SOURCES:
        for indirect in (False, True):
            name = f"tests/test_{source.replace('-', '_')}_{'ind' if indirect else 'dir'}.py"
            text = _inherited_source(source, indirect)
            text += (
                "\n\nimport os\n\n\n@pytest.fixture\ndef x(request):\n"
                '    open(os.environ["X_RAN"], "a").write("x")\n'
                '    return getattr(request, "param", 0)\n'
                "\n\n@pytest.fixture\ndef y():\n    return 1\n\n\n@pytest.fixture\n"
                "def u():\n    return 1\n"
            )
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
            files[(source, indirect)] = name
    return resolve_root(repo), files


@pytest.mark.parametrize("indirect", [False, True], ids=["direct", "indirect"])
@pytest.mark.parametrize("source", MARK_SOURCES)
def test_static_dependents_of_a_parametrized_name_agree_with_what_pytest_runs(
    tmp_path, source, indirect
):
    from codeterrain.fixtures_static import parse_source

    repo, files = _marks_repo(tmp_path)
    name = files[(source, indirect)]
    facts = parse_source((repo / name).read_text(encoding="utf-8"))
    method = next(t for t in facts["tests"] if t["name"].startswith("TestK::"))
    marker = tmp_path / "x-ran"
    subprocess.run(
        [sys.executable, "-m", "pytest", f"{name}::TestK::test_m", "-q", "-p", "no:cacheprovider"],
        cwd=repo, capture_output=True, text=True, check=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "X_RAN": str(marker)},
    )
    fixture_ran = marker.exists()
    assert ("x" in method["argnames"]) is fixture_ran is indirect


@pytest.mark.parametrize(
    ("mark", "indirect"),
    [
        ('argnames="x", argvalues=[2]', False),
        ('argnames=["x"], argvalues=[(2,)]', False),
        ('argnames="x", argvalues=[2], indirect=True', True),
    ],
    ids=["str", "list", "indirect"],
)
def test_static_dependents_of_keyword_argnames_agree_with_what_pytest_runs(
    tmp_path, mark, indirect
):
    from codeterrain.fixtures_static import parse_source

    repo = tmp_path / "repo"
    repo.mkdir()
    text = (
        "import os\n\nimport pytest\n\n\n@pytest.fixture\ndef x(request):\n"
        '    open(os.environ["X_RAN"], "a").write("x")\n'
        '    return getattr(request, "param", 0)\n\n\n'
        f"@pytest.mark.parametrize({mark})\ndef test_k(x): ...\n"
    )
    (repo / "test_k.py").write_text(text, encoding="utf-8", newline="\n")
    (test,) = parse_source(text)["tests"]
    marker = tmp_path / "x-ran"
    subprocess.run(
        [sys.executable, "-m", "pytest", "test_k.py", "-q", "-p", "no:cacheprovider"],
        cwd=repo, capture_output=True, text=True, check=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "X_RAN": str(marker)},
    )
    assert ("x" in test["argnames"]) is marker.exists() is indirect
