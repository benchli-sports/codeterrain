from __future__ import annotations

import json

from codeterrain import fixtures_static, impact
from codeterrain.cache import Cache
from codeterrain.cli import main
from codeterrain.code_static import Index, update
from codeterrain.config import load_config


def _write(root, files):
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _impact(root, cache_dir, name, depth=impact.DEFAULT_DEPTH):
    cache, config = Cache(root, cache_dir), load_config(root)
    index = Index(update(cache, config))
    fixtures = fixtures_static.build(cache, config)[0].data
    return impact.impact(index, fixtures, index.find(name), depth)


def _repo(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "app/__init__.py": "",
        "app/core.py": "def check(x):\n    pass\n",
        "app/service.py": (
            "from app.core import check\n"
            "class Service:\n"
            "    def place(self):\n"
            "        def inner():\n"
            "            check(1)\n"
            "        inner()\n"
            "def run():\n"
            "    Service().place()\n"
        ),
        "tests/conftest.py": (
            "import pytest\n"
            "from app.core import check\n"
            "@pytest.fixture\n"
            "def checked():\n"
            "    check(2)\n"
            "@pytest.fixture\n"
            "def wrapped(checked):\n"
            "    return 1\n"
        ),
        "tests/test_core.py": (
            "from unittest.mock import patch\n"
            "from app.core import check\n"
            "from app.service import run\n"
            "def test_direct():\n"
            "    check(3)\n"
            "class TestRun:\n"
            "    def test_through_run(self):\n"
            "        run()\n"
            "def test_by_fixture(wrapped):\n"
            "    pass\n"
            "def test_patched():\n"
            "    with patch('app.core.check'):\n"
            "        run()\n"
            "def test_unrelated():\n"
            "    pass\n"
        ),
    })
    return root


def test_impact_lists_the_tests_that_reach_a_name_through_callers_and_fixtures(tmp_path):
    found = _impact(_repo(tmp_path), tmp_path / "cache", "check")
    reached = {t["test"]: (t["depth"], t["via"]) for t in found["tests"]}
    assert reached == {
        "tests/test_core.py::test_direct": (1, ["app.core.check"]),
        # A call inside a nested function counts as a call from the method holding it.
        "tests/test_core.py::TestRun::test_through_run": (
            3, ["app.service.run", "app.service.Service.place", "app.core.check"],
        ),
        # Through `checked`, requested by `wrapped`, requested by the test.
        "tests/test_core.py::test_by_fixture": (
            1, ["fixture wrapped", "fixture checked", "app.core.check"],
        ),
    }
    # The test that patches it does not run the real one, so it is listed apart.
    assert [p["test"] for p in found["patched"]] == ["tests/test_core.py::test_patched"]
    assert found["counts"]["functions"] == 2 and found["counts"]["beyond_depth"] == 0


def test_impact_stops_at_the_depth_and_counts_what_lies_beyond(tmp_path):
    found = _impact(_repo(tmp_path), tmp_path / "cache", "check", depth=2)
    assert "tests/test_core.py::TestRun::test_through_run" not in {
        t["test"] for t in found["tests"]
    }
    assert found["counts"]["beyond_depth"] == 1 and found["depth"] == 2


def test_impact_counts_what_a_static_walk_cannot_follow(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def f():\n    pass\n",
        "use.py": (
            "from lib import f\n"
            "f()\n"  # at import time
            "def register(cb):\n    pass\n"
            "register(f)\n"  # passed as a value
            "def g(obj):\n    obj.f()\n"  # by name, on a receiver of unknown type
        ),
    })
    counts = _impact(root, tmp_path / "cache", "lib.f")["counts"]
    assert (counts["module_level"], counts["values"], counts["by_name"]) == (1, 1, 1)


def test_impact_follows_a_bounded_number_of_callers(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def f():\n    pass\n",
        "use.py": "from lib import f\n" + "".join(f"def g{i}():\n    f()\n" for i in range(5)),
    })
    monkeypatch.setattr(impact, "MAX_FOLLOWED", 2)
    found = _impact(root, tmp_path / "cache", "lib.f")
    assert found["counts"]["functions"] == 2 and found["truncated"]


def test_terrain_impact(tmp_path, capsys):
    root = _repo(tmp_path)
    argv = ["--root", str(root), "--cache-dir", str(tmp_path / "cache")]
    assert main(["impact", "check", *argv]) == 0
    out = capsys.readouterr().out
    assert "3 tests reach app.core.check (2 directly), through 2 callers within 3 levels" in out
    assert "1 test patches it" in out
    assert "app.service.run <- app.service.Service.place <- app.core.check" in out
    assert main(["impact", "check", "--json", "--depth", "1", *argv]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["view"] == "impact" and doc["depth"] == 1 and doc["targets"] == ["app.core.check"]
    assert {r["test"] for r in doc["rows"]} == {
        "tests/test_core.py::test_direct", "tests/test_core.py::test_by_fixture",
    }
    assert main(["impact", "nothing_like_it", *argv]) == 1


def test_an_autouse_fixture_reaches_every_test_it_runs_for(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def f():\n    pass\n",
        "tests/conftest.py": (
            "import pytest\nfrom lib import f\n"
            "@pytest.fixture\ndef base():\n    f()\n"
            "@pytest.fixture(autouse=True)\ndef everywhere(base):\n    pass\n"
        ),
        "tests/test_a.py": "def test_it():\n    pass\n",
        "tests/sub/test_b.py": (
            "import pytest\nfrom lib import f\n"
            "class TestB:\n"
            "    @pytest.fixture(autouse=True)\n    def mine(self):\n        f()\n"
            "    def test_in_class(self):\n        pass\n"
            "def test_outside():\n    pass\n"
        ),
        "other/test_c.py": "def test_not_below_the_conftest():\n    pass\n",
    })
    found = _impact(root, tmp_path / "cache", "lib.f")
    how = {t["test"]: t["how"] for t in found["tests"]}
    # Both autouse fixtures run for the class's method; either one is a right answer.
    assert how.pop("tests/sub/test_b.py::TestB::test_in_class") in {
        "autouse fixture everywhere", "autouse fixture mine",
    }
    assert how == {
        "tests/test_a.py::test_it": "autouse fixture everywhere",
        "tests/sub/test_b.py::test_outside": "autouse fixture everywhere",
    }


def test_a_call_in_a_class_body_runs_at_import_not_through_the_class(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def f():\n    return 1\nclass Foo:\n    x = f()\n",
        "tests/test_foo.py": "from lib import Foo\ndef test_it():\n    Foo()\n",
    })
    found = _impact(root, tmp_path / "cache", "lib.f")
    assert found["tests"] == [] and found["counts"]["module_level"] == 1


def test_a_patch_decorator_belongs_to_the_test_it_decorates(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def f():\n    pass\n",
        "tests/test_p.py": (
            "from unittest.mock import patch\nimport lib\n"
            "@patch('lib.f')\ndef test_top(m):\n    lib.f()\n"
            "class TestM:\n"
            "    @patch('lib.f')\n    def test_method(self, m):\n        lib.f()\n"
            "    def test_real(self):\n        lib.f()\n"
            "@patch('lib.f')\nclass TestAll:\n"
            "    def test_one(self, m):\n        lib.f()\n"
            "    def test_two(self, m):\n        lib.f()\n"
        ),
    })
    found = _impact(root, tmp_path / "cache", "lib.f")
    assert [t["test"] for t in found["tests"]] == ["tests/test_p.py::TestM::test_real"]
    assert [p["test"] for p in found["patched"]] == [
        "tests/test_p.py::TestAll::test_one", "tests/test_p.py::TestAll::test_two",
        "tests/test_p.py::TestM::test_method", "tests/test_p.py::test_top",
    ]


def test_a_long_fixture_chain_keeps_a_bounded_path_per_test(tmp_path):
    root = tmp_path / "repo"
    chain = "".join(
        f"@pytest.fixture\ndef fx{i}(fx{i - 1}):\n    pass\n" for i in range(1, 40)
    )
    _write(root, {
        "lib.py": "def f():\n    pass\n",
        "tests/conftest.py": (
            "import pytest\nfrom lib import f\n@pytest.fixture\ndef fx0():\n    f()\n" + chain
        ),
        "tests/test_x.py": "def test_it(fx39):\n    pass\n",
    })
    (row,) = _impact(root, tmp_path / "cache", "lib.f")["tests"]
    assert len(row["via"]) == impact.MAX_VIA
    assert row["via"][0] == "fixture fx39" and row["via"][-1] == "lib.f" and "…" in row["via"]


def test_an_autouse_fixture_in_a_class_reaches_only_its_methods(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def f():\n    pass\n",
        "tests/test_b.py": (
            "import pytest\nfrom lib import f\n"
            "class TestB:\n"
            "    @pytest.fixture(autouse=True)\n    def mine(self):\n        f()\n"
            "    def test_in_class(self):\n        pass\n"
            "def test_outside():\n    pass\n"
        ),
    })
    found = _impact(root, tmp_path / "cache", "lib.f")
    assert [(t["test"], t["how"]) for t in found["tests"]] == [
        ("tests/test_b.py::TestB::test_in_class", "autouse fixture mine"),
    ]


def test_a_fixture_reaches_only_the_tests_pytest_gives_it_to(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def f():\n    pass\n",
        "tests/test_a.py": (
            "import pytest\nfrom lib import f\n"
            "@pytest.fixture\ndef resource():\n    f()\n"
            "def test_a(resource):\n    pass\n"
        ),
        "tests/test_b.py": (
            "import pytest\n@pytest.fixture\ndef resource():\n    pass\n"
            "def test_b(resource):\n    pass\n"
        ),
        # A conftest fixture of the same name, overridden in test_b's module but not here.
        "tests/conftest.py": (
            "import pytest\nfrom lib import f\n@pytest.fixture\ndef shared():\n    f()\n"
        ),
        "tests/test_c.py": "def test_c(shared):\n    pass\n",
        "tests/test_d.py": (
            "import pytest\n@pytest.fixture\ndef shared():\n    pass\n"
            "def test_d(shared):\n    pass\n"
        ),
        "other/test_e.py": "def test_e(shared):\n    pass\n",
    })
    found = _impact(root, tmp_path / "cache", "lib.f")
    assert [t["test"] for t in found["tests"]] == [
        "tests/test_a.py::test_a", "tests/test_c.py::test_c",
    ]


def test_a_fixture_overriding_its_own_name_reaches_through_the_one_it_overrides(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def f():\n    pass\n",
        "tests/conftest.py": (
            "import pytest\nfrom lib import f\n@pytest.fixture\ndef db():\n    f()\n"
        ),
        "tests/test_x.py": (
            "import pytest\n@pytest.fixture\ndef db(db):\n    return db\n"
            "def test_x(db):\n    pass\n"
        ),
    })
    found = _impact(root, tmp_path / "cache", "lib.f")
    assert [(t["test"], t["via"]) for t in found["tests"]] == [
        ("tests/test_x.py::test_x", ["fixture db", "fixture db", "lib.f"]),
    ]


def test_a_fixture_chain_resolves_its_dependencies_for_the_consuming_test(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def f():\n    pass\n",
        "tests/conftest.py": (
            "import pytest\nfrom lib import f\n@pytest.fixture\ndef base():\n    f()\n"
            "@pytest.fixture\ndef wrapper(base):\n    return base\n"
        ),
        "tests/test_a.py": "def test_a(wrapper):\n    pass\n",
        # Here pytest gives `wrapper` this module's `base`, which does not call f.
        "tests/test_b.py": (
            "import pytest\n@pytest.fixture\ndef base():\n    pass\n"
            "def test_b(wrapper):\n    pass\n"
        ),
    })
    found = _impact(root, tmp_path / "cache", "lib.f")
    assert [(t["test"], t["via"]) for t in found["tests"]] == [
        ("tests/test_a.py::test_a", ["fixture wrapper", "fixture base", "lib.f"]),
    ]


def test_a_fixture_in_a_nested_class_reaches_its_tests(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def f():\n    pass\n",
        "tests/test_n.py": (
            "import pytest\nfrom lib import f\n"
            "class TestOuter:\n"
            "    @pytest.fixture\n    def outer(self):\n        f()\n"
            "    class TestInner:\n"
            "        @pytest.fixture\n        def resource(self):\n            f()\n"
            "        def test_it(self, resource):\n            pass\n"
            "        def test_outer(self, outer):\n            pass\n"
        ),
    })
    found = _impact(root, tmp_path / "cache", "lib.f")
    assert sorted(t["test"] for t in found["tests"]) == [
        "tests/test_n.py::TestOuter::TestInner::test_it",
        "tests/test_n.py::TestOuter::TestInner::test_outer",
    ]


def test_a_directly_parametrized_name_is_not_its_fixture_anywhere_in_the_chain(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def f():\n    pass\n",
        "tests/conftest.py": (
            "import pytest\nfrom lib import f\n@pytest.fixture\ndef base():\n    f()\n"
            "@pytest.fixture\ndef wrapper(base):\n    return base\n"
        ),
        "tests/test_p.py": (
            "import pytest\n"
            "@pytest.mark.parametrize('base', [42])\n"
            "def test_param(wrapper, base):\n    pass\n"
            "def test_plain(wrapper):\n    pass\n"
        ),
    })
    found = _impact(root, tmp_path / "cache", "lib.f")
    assert [t["test"] for t in found["tests"]] == ["tests/test_p.py::test_plain"]


def test_a_patch_on_a_helper_before_a_test_class_is_not_the_class_s(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def f():\n    pass\n",
        "tests/test_h.py": (
            "from unittest.mock import patch\nimport lib\n"
            "@patch('lib.f')\ndef helper(m):\n    pass\n"
            "class TestA:\n    def test_real(self):\n        lib.f()\n"
            "@patch('lib.f')\nclass TestB:\n    def test_mocked(self, m):\n        lib.f()\n"
        ),
    })
    found = _impact(root, tmp_path / "cache", "lib.f")
    assert [t["test"] for t in found["tests"]] == ["tests/test_h.py::TestA::test_real"]
    assert [p["test"] for p in found["patched"]] == ["tests/test_h.py::TestB::test_mocked"]


def test_fixtures_and_autouse_fixtures_of_a_base_class_reach_its_subclasses_tests(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def f():\n    pass\ndef g():\n    pass\n",
        "tests/base.py": (
            "import pytest\nfrom lib import g\n"
            "class Shared:\n"
            "    @pytest.fixture(autouse=True)\n    def _setup(self):\n        g()\n"
        ),
        "tests/test_inherited.py": (
            "import pytest\nfrom lib import f\nfrom tests.base import Shared\n"
            "class Base:\n    @pytest.fixture\n    def resource(self):\n        f()\n"
            "class TestChild(Base):\n    def test_it(self, resource):\n        pass\n"
            "class TestOther:\n    def test_no(self, resource):\n        pass\n"
            "class TestShared(Shared):\n    def test_auto(self):\n        pass\n"
        ),
    })
    found = _impact(root, tmp_path / "cache", "lib.f")
    assert [t["test"] for t in found["tests"]] == ["tests/test_inherited.py::TestChild::test_it"]
    found = _impact(root, tmp_path / "cache", "lib.g")
    assert [(t["test"], t["how"]) for t in found["tests"]] == [
        ("tests/test_inherited.py::TestShared::test_auto", "autouse fixture _setup"),
    ]


def test_setup_and_teardown_hooks_reach_the_tests_pytest_runs_them_for(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "".join(f"def {n}():\n    pass\n" for n in ("f", "g", "h", "k")),
        "tests/test_hooks.py": (
            "import unittest\nimport lib\n"
            "def setup_module(module):\n    lib.g()\n"
            "def setup_function(function):\n    lib.h()\n"
            "def test_plain():\n    pass\n"
            "class TestA:\n"
            "    def setup_method(self, method):\n        self.value = lib.f()\n"
            "    def test_it(self):\n        assert self.value is None\n"
            "class TestB(TestA):\n    def test_more(self):\n        pass\n"
            "class TestCase(unittest.TestCase):\n"
            "    def setUp(self):\n        lib.k()\n"
            "    def test_unit(self):\n        pass\n"
        ),
    })
    cache = tmp_path / "cache"
    reached = {
        name: sorted(t["test"].rpartition("test_hooks.py::")[2] for t in
                     _impact(root, cache, f"lib.{name}")["tests"])
        for name in ("f", "g", "h", "k")
    }
    assert reached == {
        # TestB inherits setup_method, and collects TestA's test_it as its own.
        "f": ["TestA::test_it", "TestB::test_it", "TestB::test_more"],
        "g": [
            "TestA::test_it", "TestB::test_it", "TestB::test_more", "TestCase::test_unit",
            "test_plain",
        ],
        "h": ["test_plain"],
        "k": ["TestCase::test_unit"],
    }


def test_a_test_method_of_an_uncollected_base_class_runs_in_each_test_subclass(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def f():\n    pass\n",
        "tests/test_mixin.py": (
            "import lib\n"
            "class Contract:\n    def test_contract(self):\n        lib.f()\n"
            "class TestOne(Contract):\n    pass\n"
            "class TestTwo(Contract):\n    def test_contract(self):\n        pass\n"
        ),
    })
    found = _impact(root, tmp_path / "cache", "lib.f")
    assert [t["test"] for t in found["tests"]] == [
        "tests/test_mixin.py::TestOne::test_contract",
    ]


def test_an_inherited_test_resolves_its_fixtures_in_its_own_class(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def f():\n    pass\n",
        "tests/test_inh.py": (
            "import pytest\nfrom lib import f\n"
            "class TestBase:\n"
            "    @pytest.fixture\n    def resource(self):\n        f()\n"
            "    def test_it(self, resource):\n        pass\n"
            "class TestChild(TestBase):\n"
            "    @pytest.fixture\n    def resource(self):\n        pass\n"
            "class TestKeeps(TestBase):\n    pass\n"
            # An uncollected base's test that requests the fixture runs in each test subclass.
            "class Contract:\n    def test_contract(self, resource):\n        pass\n"
            "class TestContract(Contract, TestBase):\n    pass\n"
        ),
    })
    found = _impact(root, tmp_path / "cache", "lib.f")
    assert sorted(t["test"] for t in found["tests"]) == [
        "tests/test_inh.py::TestBase::test_it",
        "tests/test_inh.py::TestContract::test_contract",
        "tests/test_inh.py::TestContract::test_it",
        "tests/test_inh.py::TestKeeps::test_it",
    ]


def test_a_subclass_overriding_a_setup_hook_does_not_run_the_base_one(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def f():\n    pass\n",
        "tests/test_ov.py": (
            "import lib\n"
            "class TestBase:\n"
            "    def setup_method(self, method):\n        lib.f()\n"
            "    def test_base(self):\n        pass\n"
            "class TestChild(TestBase):\n"
            "    def setup_method(self, method):\n        pass\n"
            "    def test_child(self):\n        pass\n"
        ),
    })
    found = _impact(root, tmp_path / "cache", "lib.f")
    assert [t["test"] for t in found["tests"]] == ["tests/test_ov.py::TestBase::test_base"]


def test_inherited_fixtures_follow_python_s_method_resolution_order(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def f():\n    pass\n",
        "tests/test_mro.py": (
            "import pytest\nfrom lib import f\n"
            "class Ancestor:\n    @pytest.fixture\n    def resource(self):\n        f()\n"
            "class Left(Ancestor):\n    pass\n"
            "class Right:\n    @pytest.fixture\n    def resource(self):\n        pass\n"
            "class TestChild(Left, Right):\n    def test_it(self, resource):\n        pass\n"
        ),
    })
    found = _impact(root, tmp_path / "cache", "lib.f")
    assert [t["test"] for t in found["tests"]] == ["tests/test_mro.py::TestChild::test_it"]


def test_unittest_hooks_run_only_for_unittest_test_cases(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def f():\n    pass\n",
        "tests/test_u.py": (
            "import unittest\nimport lib\n"
            "class TestPlain:\n"
            "    def setUp(self):\n        lib.f()\n"
            "    def test_plain(self):\n        pass\n"
            "class TestCaseLike(unittest.TestCase):\n"
            "    def setUp(self):\n        lib.f()\n"
            "    def test_unit(self):\n        pass\n"
        ),
    })
    found = _impact(root, tmp_path / "cache", "lib.f")
    assert [t["test"] for t in found["tests"]] == ["tests/test_u.py::TestCaseLike::test_unit"]


def test_a_base_imported_under_another_name_still_finds_its_subclasses(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def f():\n    pass\n",
        "tests/__init__.py": "",
        "tests/helpers.py": (
            "import lib\nclass Base:\n    def setup_method(self, method):\n        lib.f()\n"
        ),
        "tests/test_alias.py": (
            "from tests.helpers import Base as Parent\n"
            "class TestChild(Parent):\n    def test_it(self):\n        pass\n"
        ),
    })
    found = _impact(root, tmp_path / "cache", "lib.f")
    assert [t["test"] for t in found["tests"]] == ["tests/test_alias.py::TestChild::test_it"]


def test_an_autouse_name_reaches_the_fixture_that_overrides_it(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def f():\n    pass\n",
        "tests/conftest.py": (
            "import pytest\n@pytest.fixture(autouse=True)\ndef resource():\n    pass\n"
        ),
        "tests/test_over.py": (
            "import pytest\nfrom lib import f\n"
            "@pytest.fixture\ndef resource():\n    f()\n"
            "def test_it():\n    pass\n"
        ),
        "tests/test_other.py": "def test_other():\n    pass\n",
    })
    found = _impact(root, tmp_path / "cache", "lib.f")
    assert [(t["test"], t["how"]) for t in found["tests"]] == [
        ("tests/test_over.py::test_it", "autouse fixture resource"),
    ]


def test_a_unittest_case_without_the_test_prefix_is_collected(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def f():\n    pass\ndef g():\n    pass\n",
        "tests/test_calc.py": (
            "import unittest\nimport lib\n"
            "class CalculatorTests(unittest.TestCase):\n"
            "    def setUp(self):\n        lib.g()\n"
            "    def test_it(self):\n        lib.f()\n"
            "class Helper:\n    def test_not_collected(self):\n        lib.f()\n"
        ),
    })
    cache = tmp_path / "cache"
    assert [t["test"] for t in _impact(root, cache, "lib.f")["tests"]] == [
        "tests/test_calc.py::CalculatorTests::test_it",
    ]
    assert [t["test"] for t in _impact(root, cache, "lib.g")["tests"]] == [
        "tests/test_calc.py::CalculatorTests::test_it",
    ]


def test_a_fixture_offered_by_several_plugin_modules_stays_unresolved(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def f():\n    pass\n",
        "tests/a_plugin.py": (
            "import pytest\nimport lib\n@pytest.fixture\ndef resource():\n    lib.f()\n"
        ),
        "tests/b_plugin.py": "import pytest\n@pytest.fixture\ndef resource():\n    return 42\n",
        "tests/conftest.py": "pytest_plugins = ['tests.b_plugin']\n",
        "tests/test_p.py": "def test_it(resource):\n    pass\n",
    })
    assert _impact(root, tmp_path / "cache", "lib.f")["tests"] == []


def test_a_subclass_inheriting_a_constructor_is_followed(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": (
            "def f():\n    pass\n"
            "class Base:\n    def __init__(self):\n        f()\n"
            "class Child(Base):\n    pass\n"
            "class Own(Base):\n    def __init__(self):\n        pass\n"
        ),
        "tests/test_c.py": (
            "from lib import Child, Own\n"
            "def test_child():\n    Child()\n"
            "def test_own():\n    Own()\n"
        ),
    })
    found = _impact(root, tmp_path / "cache", "lib.f")
    assert [(t["test"], t["via"]) for t in found["tests"]] == [
        ("tests/test_c.py::test_child", ["lib.Child", "lib.Base.__init__", "lib.f"]),
    ]


def test_a_fixture_or_setup_hook_as_the_target_reaches_its_tests(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "tests/__init__.py": "",
        "tests/conftest.py": "import pytest\n@pytest.fixture\ndef resource():\n    return 42\n",
        "tests/test_a.py": (
            "def test_it(resource):\n    assert resource == 42\n"
            "def test_other():\n    pass\n"
            "class TestB:\n"
            "    def setup_method(self, method):\n        pass\n"
            "    def test_b(self):\n        pass\n"
        ),
    })
    cache = tmp_path / "cache"
    found = _impact(root, cache, "tests.conftest.resource")
    assert [(t["test"], t["how"]) for t in found["tests"]] == [
        ("tests/test_a.py::test_it", "fixture resource"),
    ]
    found = _impact(root, cache, "tests.test_a.TestB.setup_method")
    assert [(t["test"], t["how"]) for t in found["tests"]] == [
        ("tests/test_a.py::TestB::test_b", "setup hook setup_method"),
    ]


def test_following_a_constructor_follows_calls_of_its_class(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def f():\n    pass\nclass Foo:\n    def __init__(self):\n        f()\n",
        "tests/test_foo.py": "from lib import Foo\ndef test_it():\n    Foo()\n",
    })
    found = _impact(root, tmp_path / "cache", "lib.f")
    assert [(t["test"], t["depth"], t["via"]) for t in found["tests"]] == [
        ("tests/test_foo.py::test_it", 2, ["lib.Foo", "lib.Foo.__init__", "lib.f"]),
    ]


def test_a_constructor_named_as_the_target_reaches_calls_of_its_class(tmp_path):
    # `impact lib.Foo.__init__` found nothing; the class and the subclasses that inherit
    # the constructor are what tests call.
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": (
            "class Foo:\n    def __init__(self):\n        self.x = 42\n"
            "class Child(Foo):\n    pass\n"
            "class Own(Foo):\n    def __init__(self):\n        self.x = 1\n"
        ),
        "tests/test_a.py": (
            "from lib import Child, Foo, Own\n"
            "def test_it():\n    assert Foo().x == 42\n"
            "def test_child():\n    Child()\n"
            "def test_own():\n    Own()\n"
        ),
    })
    found = _impact(root, tmp_path / "cache", "lib.Foo.__init__")
    assert [(t["test"], t["depth"], t["via"]) for t in found["tests"]] == [
        ("tests/test_a.py::test_child", 1, ["lib.Child", "lib.Foo.__init__"]),
        ("tests/test_a.py::test_it", 1, ["lib.Foo", "lib.Foo.__init__"]),
    ]
    assert not found["truncated"]


def test_post_init_runs_only_where_the_init_is_generated(tmp_path):
    # A dataclass's generated `__init__` calls `__post_init__`; a written `__init__` does not.
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": (
            "from dataclasses import dataclass\n"
            "class Plain:\n    def __init__(self):\n        pass\n"
            # The dataclass's generated `__init__` replaces the plain base's.
            "@dataclass\nclass Foo(Plain):\n    def __post_init__(self):\n        pass\n"
            "@dataclass\nclass Gen(Foo):\n    pass\n"
            # A plain subclass runs the dataclass's generated `__init__` it inherits.
            "class Sub(Foo):\n    pass\n"
            "class Own(Foo):\n    def __init__(self):\n        pass\n"
            # Not a dataclass: nothing calls the hook.
            "class Loose:\n    def __post_init__(self):\n        pass\n"
            # The generated `__init__` of a dataclass without a hook never calls one.
            "@dataclass\nclass Bare:\n    pass\n"
            "class Late(Bare):\n    def __post_init__(self):\n        pass\n"
            "@dataclass(init=False)\nclass Manual:\n"
            "    def __init__(self):\n        pass\n"
            "    def __post_init__(self):\n        pass\n"
        ),
        "tests/test_a.py": (
            "from lib import Foo, Gen, Late, Loose, Manual, Own, Plain, Sub\n"
            "def test_late():\n    Late()\n"
            "def test_foo():\n    Foo()\n"
            "def test_gen():\n    Gen()\n"
            "def test_sub():\n    Sub()\n"
            "def test_plain():\n    Plain()\n"
            "def test_loose():\n    Loose()\n"
            "def test_own():\n    Own()\n"
            "def test_manual():\n    Manual()\n"
        ),
    })
    cache = tmp_path / "cache"
    found = _impact(root, cache, "lib.Foo.__post_init__")
    assert [t["test"] for t in found["tests"]] == [
        "tests/test_a.py::test_foo", "tests/test_a.py::test_gen", "tests/test_a.py::test_sub",
    ]
    assert _impact(root, cache, "lib.Manual.__post_init__")["tests"] == []
    assert _impact(root, cache, "lib.Loose.__post_init__")["tests"] == []
    assert _impact(root, cache, "lib.Late.__post_init__")["tests"] == []
    # The dataclasses below `Plain` generate their own `__init__`, which replaces its one.
    assert [t["test"] for t in _impact(root, cache, "lib.Plain.__init__")["tests"]] == [
        "tests/test_a.py::test_plain",
    ]


_RESOURCE = (
    "import pytest\nimport lib\n@pytest.fixture\ndef resource():\n    return lib.f()\n"
)


def test_a_copied_test_takes_the_consuming_class_s_marks(tmp_path):
    # The child class parametrizes `resource`, so its copy of test_it never runs the
    # fixture; the base's own copy still does.
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def f():\n    pass\n",
        "tests/conftest.py": _RESOURCE,
        "tests/test_a.py": (
            "import pytest\n"
            "class TestBase:\n    def test_it(self, resource):\n        pass\n"
            "@pytest.mark.parametrize('resource', [42])\n"
            "class TestChild(TestBase):\n    pass\n"
        ),
    })
    found = _impact(root, tmp_path / "cache", "lib.f")
    assert [t["test"] for t in found["tests"]] == ["tests/test_a.py::TestBase::test_it"]


def test_a_static_test_method_keeps_its_first_fixture_in_copies(tmp_path):
    # A static method has no `self`: its first argument is a fixture, in the class's own record
    # (rebuilt when a base carries marks) and in each copy.
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def f():\n    pass\n",
        "tests/conftest.py": _RESOURCE,
        "tests/test_a.py": (
            "import pytest\n"
            "@pytest.mark.usefixtures('other')\nclass Base:\n    pass\n"
            "class TestBase(Base):\n"
            "    @staticmethod\n    def test_it(resource):\n        pass\n"
            "class TestChild(TestBase):\n    pass\n"
        ),
    })
    found = _impact(root, tmp_path / "cache", "lib.f")
    assert [t["test"] for t in found["tests"]] == [
        "tests/test_a.py::TestBase::test_it", "tests/test_a.py::TestChild::test_it",
    ]


def test_marks_on_an_uncollected_base_s_method_are_kept(tmp_path):
    # `class Contract` is not collected; its method's usefixtures mark runs the fixture in each
    # collected subclass.
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def f():\n    pass\n",
        "tests/conftest.py": _RESOURCE,
        "tests/contract.py": (
            "import pytest\n"
            "class Contract:\n"
            "    @pytest.mark.usefixtures('resource')\n    def test_it(self):\n        pass\n"
        ),
        "tests/test_impl.py": "from tests.contract import Contract\nclass TestImpl(Contract):\n"
                              "    pass\n",
    })
    found = _impact(root, tmp_path / "cache", "lib.f")
    assert [(t["test"], t["how"]) for t in found["tests"]] == [
        ("tests/test_impl.py::TestImpl::test_it", "fixture resource"),
    ]


def test_a_base_module_s_marks_do_not_follow_its_tests_into_another_module(tmp_path):
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def f():\n    pass\n",
        "tests/conftest.py": _RESOURCE,
        "tests/test_base.py": (
            "import pytest\npytestmark = pytest.mark.parametrize('resource', [42])\n"
            "class TestBase:\n    def test_it(self, resource):\n        pass\n"
        ),
        "tests/test_child.py": "from tests.test_base import TestBase\n"
                               "class TestChild(TestBase):\n    pass\n",
    })
    found = _impact(root, tmp_path / "cache", "lib.f")
    assert [t["test"] for t in found["tests"]] == ["tests/test_child.py::TestChild::test_it"]


def test_a_base_class_s_marks_apply_to_a_test_class_s_own_methods(tmp_path):
    # pytest merges class marks along the MRO: a usefixtures mark on a plain base class runs the
    # fixture for the subclass's own tests too.
    root = tmp_path / "repo"
    _write(root, {
        "lib.py": "def f():\n    pass\n",
        "tests/conftest.py": _RESOURCE,
        "tests/test_a.py": (
            "import pytest\n"
            "@pytest.mark.usefixtures('resource')\nclass Base:\n    pass\n"
            "class TestX(Base):\n    def test_own(self):\n        pass\n"
        ),
    })
    found = _impact(root, tmp_path / "cache", "lib.f")
    assert [(t["test"], t["how"]) for t in found["tests"]] == [
        ("tests/test_a.py::TestX::test_own", "fixture resource"),
    ]


def test_patch_decorators_are_matched_without_rescanning_the_file(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    body = "".join(f"@patch('lib.f')\ndef test_{i}(m):\n    pass\n" for i in range(300))
    _write(root, {
        "lib.py": "def f():\n    pass\n",
        "tests/test_many.py": "from unittest.mock import patch\n" + body,
    })
    cache, config = Cache(root, tmp_path / "cache"), load_config(root)
    index = Index(update(cache, config))
    fixtures = fixtures_static.build(cache, config)[0].data
    looked = []
    real = index.definition
    monkeypatch.setattr(index, "definition", lambda full: looked.append(full) or real(full))
    found = impact.impact(index, fixtures, ["lib.f"])
    assert len(found["patched"]) == 300
    # One definition lookup per test to lay out the file's spans, not one per test per patch.
    assert sum(1 for full in looked if full.startswith("tests.test_many.test_")) <= 2 * 300


def test_fixture_visibility_work_grows_with_the_requests_not_their_square(tmp_path, monkeypatch):
    # Each request scanned every definition of its name, so N classes that override and
    # request one fixture cost N * N; looked up by scope, doubling N doubles the work.
    made = []

    class Counting(impact._Fixtures):
        def __init__(self, *args):
            super().__init__(*args)
            made.append(self)

    monkeypatch.setattr(impact, "_Fixtures", Counting)

    def work(n):
        root = tmp_path / f"repo{n}"
        classes = "".join(
            f"class TestC{i}:\n    @pytest.fixture\n    def resource(self):\n"
            "        return lib.f()\n"
            "    def test_it(self, resource):\n        pass\n"
            for i in range(n)
        )
        _write(root, {
            "lib.py": "def f():\n    pass\n",
            "tests/test_a.py": f"import pytest\nimport lib\n{classes}",
        })
        found = _impact(root, tmp_path / f"cache{n}", "lib.f")
        assert len(found["tests"]) == n
        return made[-1].inspected

    small, large = work(100), work(200)
    assert large <= 2.5 * small
