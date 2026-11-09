"""Impact: the tests that reach a Python name, joining the code index to the fixture index.

From the definition, ``impact`` walks outwards through its callers, a bounded number of levels,
until it meets tests: a call inside a test function is a test that runs the name; a call inside a
fixture reaches every test that pytest gives that fixture, directly or through other fixtures,
and every test an autouse fixture runs for; a call inside any other function (code or a test
helper) makes that function the next name to follow, and following a constructor (``__init__``)
follows the calls of its class too. A test that patches the name is listed apart: there the real
one does not run.

What a static walk cannot follow is counted, not guessed: calls that run at import time (module
level, a class body), the name used as a value (``Depends(get_user)``, a callback) and calls by
name on a receiver of unknown type. Nested functions are not definitions of their own, so a call
inside one counts as a call from the function that holds it.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from pathlib import PurePosixPath
from typing import Any

from codeterrain import code_static
from codeterrain.code_static import Index
from codeterrain.fixtures_static import is_test_module

#: How many levels of callers ``impact`` follows by default, and at most.
DEFAULT_DEPTH = 3
MAX_DEPTH = 10
#: How many functions ``impact`` follows in all, and how many fixtures it expands.
MAX_FOLLOWED = 2_000
MAX_FIXTURES = 2_000
#: How many steps of a chain a test row keeps: the first and the last ones, with "…" between.
MAX_VIA = 12
#: How many requests of a fixture (a test or fixture naming it) the fixture walk checks in all.
MAX_REQUESTS = 1_000_000
#: How many classes up an inheritance chain, and how many inheriting classes, are looked at.
MAX_BASES = 32
MAX_SUBCLASSES = 2_000
#: Methods that run when the class is called: following one follows the class's calls too.
_CONSTRUCTORS = ("__init__", "__new__", "__post_init__")
#: Functions pytest calls around tests without being asked (xunit style and unittest): a class's
#: hooks run for its tests and its subclasses' tests, a module's for the tests in that module.
_CLASS_HOOKS = frozenset({"setup_method", "teardown_method", "setup_class", "teardown_class"})
_MODULE_HOOKS = frozenset({"setup_module", "teardown_module"})
#: unittest's own, which pytest calls only for ``unittest.TestCase`` classes.
_UNITTEST_CLASS_HOOKS = frozenset({"setUp", "tearDown", "setUpClass", "tearDownClass"})
_UNITTEST_MODULE_HOOKS = frozenset({"setUpModule", "tearDownModule"})
_UNITTEST_BASES = frozenset({"unittest.TestCase", "unittest.IsolatedAsyncioTestCase"})
_FUNCTION_HOOKS = frozenset({"setup_function", "teardown_function"})


def _caller(index: Index, rel: str, scope: str) -> str:
    """The definition a call in ``scope`` of ``rel`` belongs to: the innermost enclosing
    function or method the index knows (nested functions and comprehensions are not
    definitions), or "" for code that runs at import time: module level, or a class body."""
    module = code_static.module_name(rel, index.src_package)
    while scope:
        full = f"{module}.{scope}"
        found = index.definition(full)
        if found is not None:
            return "" if found[1]["kind"] == "class" else full
        scope = scope.rpartition(".")[0]
    return ""


def _qualname(index: Index, rel: str, scope: str) -> str:
    """The pytest-style name (``Class::method``) of the definition a call in ``scope`` of
    ``rel`` belongs to, or "" when it runs at import time."""
    caller = _caller(index, rel, scope)
    module = code_static.module_name(rel, index.src_package)
    return caller.removeprefix(f"{module}.").replace(".", "::") if caller else ""


def _class_of(test: dict[str, Any]) -> str:
    """The class a test is defined in, as fixtures record it (``TestOuter::TestInner``; "" for
    a function)."""
    return test["name"].rpartition("::")[0]


def _step(name: str, path: list[str]) -> list[str]:
    """``path`` with ``name`` in front, kept to ``MAX_VIA`` steps: the newest, then the end."""
    if len(path) < MAX_VIA:
        return [name, *path]
    return [name, "…", *path[-(MAX_VIA - 2):]]


class _Decorators:
    """Which tests a decorator line of a test file applies to (``@patch(...)`` is read before
    the function's own scope): the test whose decorators span the line, else every test method
    of the class defined next, when no other definition (a decorated helper) comes between.
    Each file's spans are laid out once, then found by bisection."""

    def __init__(self, index: Index, by_file: dict[str, list[dict[str, Any]]]) -> None:
        self.index, self.by_file = index, by_file
        self._files: dict[str, tuple[list[int], list[tuple[int, int, list[str]]]]] = {}
        self._lines: dict[str, list[int]] = {}

    def _spans(self, rel: str) -> tuple[list[int], list[tuple[int, int, list[str]]]]:
        if rel not in self._files:
            module = code_static.module_name(rel, self.index.src_package)
            tests = self.by_file.get(rel, [])
            # every test and test class of the file looked up at once, not one query each
            quals = {t["name"].replace("::", ".") for t in tests} | {
                _class_of(t).replace("::", ".") for t in tests if "::" in t["name"]
            }
            known = self.index.definitions(f"{module}.{q}" for q in quals)

            def def_line(qual: str) -> int:
                found = known.get(f"{module}.{qual}")
                return found[1]["line"] if found is not None else 0

            # (start, end, tests): a test spans its first decorator to its `def`; a class is a
            # point at its `class` line, standing for all its test methods.
            spans = [
                (t["line"], def_line(t["name"].replace("::", ".")), [t["name"]]) for t in tests
            ]
            methods: dict[str, list[str]] = {}
            for t in tests:
                if "::" in t["name"]:
                    methods.setdefault(_class_of(t), []).append(t["name"])
            spans += [
                (line, line, names) for cls, names in methods.items()
                if (line := def_line(cls.replace("::", ".")))
            ]
            spans.sort()
            self._files[rel] = ([s[0] for s in spans], spans)
        return self._files[rel]

    def tests(self, rel: str, line: int) -> list[str]:
        starts, spans = self._spans(rel)
        at = bisect_right(starts, line)
        if at and spans[at - 1][0] <= line < spans[at - 1][1]:
            return spans[at - 1][2]
        # The next definition after the line is a test class: a decorator of that class.
        if at < len(spans) and spans[at][0] == spans[at][1]:
            if rel not in self._lines:
                self._lines[rel] = self.index.lines(rel)
            lines = self._lines[rel]
            following = bisect_left(lines, line)
            if following < len(lines) and lines[following] == spans[at][0]:
                return spans[at][2]
        return []


class _Classes:
    """Class inheritance from the code index, in the fixture view's naming (``Outer::Inner``):
    the classes a class takes methods and fixtures from, and the classes that inherit from it.
    Bounded: ``MAX_BASES`` classes up a chain, ``MAX_SUBCLASSES`` found below one."""

    def __init__(self, index: Index) -> None:
        self.index = index
        self._mro: dict[tuple[str, str], list[tuple[str, str]]] = {}
        self._below: dict[tuple[str, str], list[tuple[str, str]]] = {}
        self._members: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
        self.cut = False

    def _bases(self, rel: str, cls: str) -> list[tuple[str, str]]:
        out = []
        for full in self.index.bases(rel, cls.replace("::", ".")):
            found = self.index.definition(full)
            if found is not None and found[1]["kind"] == "class":
                out.append((found[0], found[1]["name"].replace(".", "::")))
        return out

    def mro(self, rel: str, cls: str, depth: int = 0) -> list[tuple[str, str]]:
        """``(rel, cls)`` and then its bases in Python's method resolution order (C3), as far
        as the index can place them; an order C3 rejects falls back to depth first."""
        key = (rel, cls)
        if key not in self._mro:
            self._mro[key] = [key]  # a cycle through bad bases stops here
            bases = self._bases(rel, cls) if depth < MAX_BASES else []
            seqs = [self.mro(*b, depth + 1) for b in bases] + [bases]
            seqs = [list(s) for s in seqs if s]
            out = [key]
            while seqs and len(out) < MAX_BASES:
                head = next(
                    (s[0] for s in seqs if not any(s[0] in t[1:] for t in seqs)), None
                )
                if head is None:  # inconsistent: keep the rest in depth-first order
                    out += [c for s in seqs for c in s if c not in out]
                    break
                if head not in out:
                    out.append(head)
                seqs = [t for t in ([c for c in s if c != head] for s in seqs) if t]
            self._mro[key] = out[:MAX_BASES]
        return self._mro[key]

    def members(self, rel: str, cls: str) -> dict[str, dict[str, Any]]:
        key = (rel, cls)
        if key not in self._members:
            self._members[key] = self.index.members(rel, cls.replace("::", "."))
        return self._members[key]

    def is_unittest(self, rel: str, cls: str) -> bool:
        """Whether ``cls`` derives from ``unittest.TestCase``."""
        return any(
            base in _UNITTEST_BASES
            for c in self.mro(rel, cls) for base in self.index.bases(c[0], c[1].replace("::", "."))
        )

    def definer(self, rel: str, cls: str, name: str) -> tuple[str, str] | None:
        """The class in the method resolution order of ``cls`` whose ``name`` it uses."""
        return next((c for c in self.mro(rel, cls) if name in self.members(*c)), None)

    def initializer(self, rel: str, cls: str) -> tuple[tuple[str, str], bool] | None:
        """The class in the MRO of ``cls`` whose ``__init__`` a call of it runs, and whether
        ``@dataclass`` generated it (a written one in the same class wins); None for object's."""
        module = code_static.module_name
        for c in self.mro(rel, cls):
            if "__init__" in self.members(*c):
                return c, False
            dotted = c[1].replace("::", ".")
            found = self.index.definition(
                ".".join(filter(None, (module(c[0], self.index.src_package), dotted)))
            )
            if found is not None and found[0] == c[0] and found[1]["gen"]:
                return c, True
        return None

    def scopes(self, rel: str, cls: str) -> list[tuple[str, str]]:
        """Where pytest looks for a class-level fixture a request in ``cls`` sees: the class and
        its bases, then each enclosing class and its bases."""
        out: list[tuple[str, str]] = []
        while cls:
            out += [s for s in self.mro(rel, cls) if s not in out]
            cls = cls.rpartition("::")[0]
        return out

    def subclasses(self, rel: str, cls: str) -> list[tuple[str, str]]:
        """Every class whose bases reach ``(rel, cls)``, nearest first."""
        key = (rel, cls)
        if key not in self._below:
            out: list[tuple[str, str]] = []
            todo = [key]
            while todo:
                here = todo.pop(0)
                # A subclass may name the base under an import alias (``Base as Parent``).
                tails = self.index.aliases({here[1].rpartition("::")[2]})
                for sub_rel, sub in dict.fromkeys(
                    c for t in sorted(tails) for c in self.index.subclasses_named(t)
                ):
                    found = (sub_rel, sub.replace(".", "::"))
                    if found in out or found == key or here not in self.mro(*found)[1:]:
                        continue
                    if len(out) >= MAX_SUBCLASSES:
                        self.cut = True
                        break
                    out.append(found)
                    todo.append(found)
            self._below[key] = out
        return self._below[key]


class _Fixtures:
    """The fixture view, arranged for the walk: who requests each fixture name, and which
    definition of a name pytest gives a request (visibility and overrides)."""

    def __init__(
        self, data: dict[str, Any], by_file: dict[str, list[dict[str, Any]]], classes: _Classes
    ) -> None:
        self.by_file, self.classes = by_file, classes
        self.at = {(f["file"], f["cls"], f["func"]): f for f in data.get("fixtures", [])}
        self.named: dict[str, list[dict[str, Any]]] = {}
        # Name -> (file, class) -> its definitions there, in view order; and name -> plugin
        # module -> its definitions: a request looks up its scopes, not every definition.
        self._placed: dict[str, dict[tuple[str, str], list[dict[str, Any]]]] = {}
        self._plugins: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for f in data.get("fixtures", []):
            self.named.setdefault(f["name"], []).append(f)
            self._placed.setdefault(f["name"], {}).setdefault((f["file"], f["cls"]), []).append(f)
            if not is_test_module(f["file"]) and PurePosixPath(f["file"]).name != "conftest.py":
                self._plugins.setdefault(f["name"], {}).setdefault(f["file"], []).append(f)
        #: Scope lookups made by ``visible``: each costs one dictionary probe.
        self.inspected = 0
        # Fixture name -> (kind, record, class) of each test and fixture that requests it, by
        # argument, usefixtures or getfixturevalue.
        self.requests: dict[str, list[tuple[str, dict[str, Any], str]]] = {}
        for t in data.get("tests", []):
            for name in {*t["argnames"], *t["usefixtures"], *t["dyn"]}:
                self.requests.setdefault(name, []).append(("test", t, _class_of(t)))
        for f in data.get("fixtures", []):
            for name in {*f["argnames"], *f["dyn"]}:
                self.requests.setdefault(name, []).append(("fixture", f, f["cls"]))
        self._seen: dict[tuple[str, str, str, int], dict[str, Any] | None] = {}

    def visible(
        self, name: str, rel: str, cls: str, skip: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """The definition of fixture ``name`` that pytest gives a request in ``rel`` (in class
        ``cls``): the class's own or a base class's, then each enclosing class's, then the
        module's, then the nearest conftest's up the folders, then one from a plugin module.
        ``skip`` is a fixture requesting its own name, which gets the one it overrides."""
        key = (name, rel, cls, id(skip))
        if key not in self._seen:
            placed = self._placed.get(name, {})

            def at(file: str, owner: str) -> dict[str, Any] | None:
                self.inspected += 1
                return next((f for f in placed.get((file, owner), ()) if f is not skip), None)

            best = None
            if cls:
                for scope in self.classes.scopes(rel, cls):
                    best = at(*scope)
                    if best is not None:
                        break
            if best is None:
                best = at(rel, "")
            if best is None:
                for folder in PurePosixPath(rel).parents:
                    best = at("conftest.py" if str(folder) == "." else f"{folder}/conftest.py", "")
                    if best is not None:
                        break
            if best is None:
                # A plugin module's fixture, when only one module offers the name: which of
                # several pytest loads depends on registration (``pytest_plugins``, entry
                # points) that a static read does not resolve, so then it stays unresolved.
                # Leaving out ``skip`` removes one module at most, so past two none is chosen.
                modules = self._plugins.get(name, {})
                offered = [] if len(modules) > 2 else [
                    f for fs in modules.values()
                    # The module's last definition, the one its import binds.
                    if (f := next((g for g in reversed(fs) if g is not skip), None)) is not None
                ]
                best = offered[0] if len(offered) == 1 else None
            if len(self._seen) >= code_static.MAX_CACHED:
                self._seen.clear()
            self._seen[key] = best
        return self._seen[key]

    def autouse_tests(self, fx: dict[str, Any]) -> list[tuple[str, dict[str, Any], str]]:
        """The tests an autouse fixture can run for: below its conftest's folder, else in its
        module, and only its class's methods (its nested and inheriting classes' too) when it
        is defined in a class."""
        if not fx.get("autouse"):
            return []
        folder, _, base = fx["file"].rpartition("/")
        if base == "conftest.py":
            tests = [
                t for rel, ts in self.by_file.items()
                if not folder or rel.startswith(f"{folder}/") for t in ts
            ]
        elif not fx["cls"]:
            tests = self.by_file.get(fx["file"], [])
        else:
            tests = self.in_classes([(fx["file"], fx["cls"])], nested=True)
        return [("autouse", t, _class_of(t)) for t in tests]

    def in_classes(self, owners: list[tuple[str, str]], nested: bool) -> list[dict[str, Any]]:
        """The tests in each ``(rel, cls)`` of ``owners`` and in every class inheriting from
        one, and, with ``nested``, in the classes nested in those."""
        every = [*owners, *(s for o in owners for s in self.classes.subclasses(*o))]
        out = []
        for rel, cls in dict.fromkeys(every):
            out += [
                t for t in self.by_file.get(rel, [])
                if _class_of(t) == cls or nested and _class_of(t).startswith(f"{cls}::")
            ]
        return out


#: What ``fixture_dependents`` reports as ``gets`` for a fixture that requests another name: pytest
#: resolves that request where each test using the fixture sits, so it differs from test to test.
PER_TEST = "per test"


def _tests_model(
    index: Index, fixtures: dict[str, Any]
) -> tuple[
    _Classes, list[dict[str, Any]], list[dict[str, Any]],
    dict[tuple[str, str], list[dict[str, Any]]], dict[str, list[dict[str, Any]]],
    dict[str, list[dict[str, Any]]], _Fixtures,
]:
    """The tests pytest collects, as ``impact`` and ``fixture_dependents`` both see them: the
    view's own (with marks a base class adds), the copies of inherited test methods, and the
    fixture view arranged over them. Returns (classes, own, copied, copies, own_by_file,
    by_file, fixtures)."""
    classes = _Classes(index)
    own = list(fixtures.get("tests", []))
    copied, copies, adjusted = _inherited_tests(index, classes, own, fixtures.get("marks"))
    own = [adjusted.get((t["file"], t["name"]), t) for t in own]
    own_by_file: dict[str, list[dict[str, Any]]] = {}
    for t in own:
        own_by_file.setdefault(t["file"], []).append(t)
    by_file = {rel: list(ts) for rel, ts in own_by_file.items()}
    for t in copied:
        by_file.setdefault(t["file"], []).append(t)
    fx = _Fixtures({**fixtures, "tests": own + copied}, by_file, classes)
    return classes, own, copied, copies, own_by_file, by_file, fx


def fixture_dependents(
    index: Index, fixtures: dict[str, Any], name: str, resolve: bool = False
) -> list[dict[str, Any]]:
    """Every fixture and test that pytest gives fixture ``name``: by argument, usefixtures or
    getfixturevalue, and every test an autouse definition of it runs for; tests include the
    copies of a test method a class inherits (``inherited`` names the method's own place).

    With ``resolve``, each row's ``gets`` is the definition pytest gives it, as ``file:line``: a
    test gets the nearest one up from its class and bases, module and conftest folders; a fixture
    requesting its own name gets the one it overrides; one requesting another name gets
    ``PER_TEST``; "" when none is visible."""
    _, _, _, copies, _, _, fx = _tests_model(index, fixtures)
    base_of = {
        id(c): f"{base_file}:{c['line']}" for (base_file, _), cs in copies.items() for c in cs
    }
    class_lines: dict[tuple[str, str], int] = {}

    def where(record: dict[str, Any], cls: str) -> int:
        """A copy's line is its class's in the consuming file, not the base method's."""
        if id(record) not in base_of:
            return record["line"]
        key = (record["file"], cls)
        if key not in class_lines:
            module = code_static.module_name(record["file"], index.src_package)
            found = index.definition(f"{module}.{cls.replace('::', '.')}")
            class_lines[key] = found[1]["line"] if found is not None else 0
        return class_lines[key]

    rows: dict[tuple[str, str, str], dict[str, Any]] = {}

    def add(kind: str, record: dict[str, Any], cls: str, via: str) -> None:
        key = (kind, record["file"], record["name"])
        if key in rows or name in record.get("parametrized", ()):
            return  # a test parametrizing the name directly never runs the fixture
        row = {
            "kind": kind, "name": record["name"], "file": record["file"],
            "line": where(record, cls), "via": via,
        }
        if id(record) in base_of:
            row["inherited"] = base_of[id(record)]
        if resolve:
            if kind == "fixture" and record["name"] != name:
                row["gets"] = PER_TEST
            else:
                skip = record if kind == "fixture" else None
                found = fx.visible(name, record["file"], cls, skip)
                row["gets"] = f"{found['file']}:{found['line']}" if found is not None else ""
        rows[key] = row

    for kind, record, cls in fx.requests.get(name, []):
        via = (
            "arg" if name in record["argnames"]
            else "usefixtures" if name in record.get("usefixtures", ())
            else "getfixturevalue"
        )
        add(kind, record, cls, via)
    for each in fx.named.get(name, []):
        for _, record, cls in fx.autouse_tests(each):
            add("test", record, cls, "autouse")
    return sorted(
        rows.values(), key=lambda r: (r["kind"] != "fixture", r["file"], r["line"], r["name"])
    )


def _inherited_tests(
    index: Index, classes: _Classes, tests: list[dict[str, Any]],
    marks: list[dict[str, Any]] | None = None,
) -> tuple[
    list[dict[str, Any]], dict[tuple[str, str], list[dict[str, Any]]],
    dict[tuple[str, str], dict[str, Any]],
]:
    """The test methods pytest collects in a test class that come from its bases, as test
    records of that class, so their fixtures resolve there; and, for each base method, its
    copies. A copy runs with the marks pytest gives it there: the method's own, the consuming
    class's and each of its bases' (pytest merges class marks along the MRO), its enclosing
    classes' and the consuming module's, not the base module's. A ``unittest.TestCase``
    subclass without the ``Test`` prefix gets records for its own test methods the same way,
    since pytest collects it whatever its name. The third result replaces the records of a test
    class's own methods when a base class carries marks the fixture view (one file at a time)
    could not apply."""
    marked = {(m["file"], m["name"]): m for m in marks or []}
    records: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for t in tests:
        cls, _, func = t["name"].rpartition("::")
        if cls:
            records.setdefault((t["file"], cls), {})[func] = t
    out: list[dict[str, Any]] = []
    copies: dict[tuple[str, str], list[dict[str, Any]]] = {}
    adjusted: dict[tuple[str, str], dict[str, Any]] = {}

    def class_marks(rel: str, cls: str, mro: list[tuple[str, str]]) -> tuple[set[str], set[str]]:
        """The module's, the enclosing classes' and every class in the MRO's own marks."""
        parts = cls.split("::")
        scopes = [(rel, ""), *((rel, "::".join(parts[:i])) for i in range(1, len(parts)))]
        use: set[str] = set()
        direct: set[str] = set()
        for key in [*scopes, *mro]:
            found = marked.get(key)
            if found is not None:
                use.update(found["use"])
                direct.update(found["direct"])
        return use, direct

    def record_for(
        row: dict[str, Any], source: dict[str, Any] | None, own: tuple[str, str],
        context: tuple[set[str], set[str]],
    ) -> dict[str, Any]:
        mine = marked.get(own)
        use = context[0] | set(mine["use"] if mine else ())
        supplied = context[1] | set(mine["direct"] if mine else ())
        # A static method has no `self` to skip.
        bound = 0 if row["kind"] == "staticmethod" else 1
        pos = row["pos"][bound:len(row["pos"]) - row["defaults"]]
        return {
            "line": source["line"] if source else row["line"],
            "argnames": [a for a in [*pos, *row["kwreq"]] if a not in supplied],
            "usefixtures": sorted(use - supplied),
            "dyn": source["dyn"] if source else [],
            "parametrized": sorted(supplied),
        }
    found = index.classes_with_bases(MAX_SUBCLASSES + 1)
    if len(found) > MAX_SUBCLASSES:
        classes.cut = True
    for rel, dotted in found[:MAX_SUBCLASSES]:
        cls = dotted.replace(".", "::")
        if not is_test_module(rel):
            continue
        named = cls.rpartition("::")[2].startswith("Test")
        # pytest collects a unittest.TestCase subclass whatever its name, and the fixture view
        # (which keeps Test* classes) has no record of its own methods.
        if not named and not classes.is_unittest(rel, cls):
            continue
        mro = classes.mro(rel, cls)
        context = class_marks(rel, cls, mro)
        taken = set(classes.members(rel, cls)) if named else set()
        if named and any(marked.get(b) for b in mro[1:]):
            # The class's own methods: the fixture view applied this file's marks, not a base
            # class's from the MRO.
            for name, row in classes.members(rel, cls).items():
                own_record = records.get((rel, cls), {}).get(name)
                if own_record is not None and row["kind"] not in ("class", "variable"):
                    adjusted[(rel, own_record["name"])] = {
                        **own_record,
                        **record_for(row, own_record, (rel, f"{cls}::{name}"), context),
                    }
        for base in mro:
            if base == (rel, cls) and named:
                continue
            members = classes.members(*base)
            for name, row in members.items():
                if name in taken or not name.startswith("test") or row["kind"] in (
                    "class", "variable",
                ):
                    continue
                source = records.get(base, {}).get(name)
                record = {
                    **record_for(row, source, (base[0], f"{base[1]}::{name}"), context),
                    "file": rel, "name": f"{cls}::{name}",
                }
                out.append(record)
                copies.setdefault((base[0], f"{base[1]}::{name}"), []).append(record)
            taken |= set(members)
    return out, copies, adjusted


def impact(
    index: Index, fixtures: dict[str, Any], targets: list[str], depth: int = DEFAULT_DEPTH,
) -> dict[str, Any]:
    """The tests that reach ``targets`` within ``depth`` levels of callers. ``fixtures`` is the
    fixture view's data (its ``fixtures`` and ``tests``). Each test row names the function it
    calls and the chain from there to a target (``via``); ``patched`` are the tests that patch a
    target; ``counts`` holds what was followed and what could not be."""
    depth = max(1, min(depth, MAX_DEPTH))
    classes, own, copied, copies, own_by_file, by_file, fx = _tests_model(index, fixtures)
    tests_in = {(t["file"], t["name"]): t for t in own}
    decorators = _Decorators(index, own_by_file)
    # Each followed name -> (the name it calls, "file:line" of that call); targets map to None.
    parent: dict[str, tuple[str, str] | None] = dict.fromkeys(targets)
    reached: dict[str, dict[str, Any]] = {}
    patched: dict[str, dict[str, Any]] = {}
    counts = dict.fromkeys(
        ("functions", "beyond_depth", "module_level", "values", "by_name"), 0
    )
    budget = [code_static.MAX_EXAMINED]
    truncated = False
    expanded: set[tuple[Any, ...]] = set()
    autoused_by_name: dict[str, list[tuple[str, dict[str, Any], str]]] = {}
    work = [MAX_REQUESTS]

    def constructed(name: str) -> list[str]:
        """The classes whose call runs ``name`` when it is a constructor: its class, and each
        subclass that inherits it unchanged; [] for anything else."""
        owner, _, method = name.rpartition(".")
        if method not in _CONSTRUCTORS:
            return []
        found_owner = index.definition(owner)
        if found_owner is None or found_owner[1]["kind"] != "class":
            return []
        here = (found_owner[0], found_owner[1]["name"].replace(".", "::"))

        def runs(rel: str, cls: str) -> bool:
            # The `__init__` a call of `cls` runs, written or generated by `@dataclass`: it must
            # be the target itself, and `__post_init__` runs only from a generated one.
            if method == "__init__":
                return classes.initializer(rel, cls) == (here, False)
            if method == "__post_init__":
                # The generated `__init__` calls the hook only if its class had one when
                # `@dataclass` ran: a hook added by a subclass of it is never called.
                found = classes.initializer(rel, cls)
                return (
                    found is not None and found[1]
                    and classes.definer(*found[0], "__post_init__") is not None
                )
            return True

        out = [owner] if runs(*here) else []
        for sub_rel, sub in classes.subclasses(*here):
            if classes.definer(sub_rel, sub, method) == here and runs(sub_rel, sub):
                sub_module = code_static.module_name(sub_rel, index.src_package)
                out.append(".".join(filter(None, (sub_module, sub.replace("::", ".")))))
        return out

    def chain(full: str) -> list[str]:
        out = [full]
        while (step := parent.get(out[-1])) is not None and len(out) <= MAX_DEPTH + 1:
            out.append(step[0])
        return out

    def reach(test: dict[str, Any], via: list[str], how: str, level: int) -> None:
        node = f"{test['file']}::{test['name']}"
        if node not in reached or reached[node]["depth"] > level:
            reached[node] = {
                "test": node, "file": test["file"], "line": test["line"],
                "depth": level, "how": how, "via": via,
            }

    def hooked(rel: str, cls: str, func: str) -> list[dict[str, Any]]:
        """The tests a setup or teardown hook ``func`` (of class ``cls``, or of the module when
        it is "") runs for; [] when it is not one. A class's hook runs for the tests of each
        class that inherits it without overriding it, inherited test methods included."""
        if cls and func in _CLASS_HOOKS | _UNITTEST_CLASS_HOOKS:
            return [
                t for t in fx.in_classes([(rel, cls)], nested=False)
                if classes.definer(t["file"], _class_of(t), func) == (rel, cls)
                and (func in _CLASS_HOOKS or classes.is_unittest(t["file"], _class_of(t)))
            ]
        if not cls and func in _MODULE_HOOKS:
            return by_file.get(rel, [])
        if not cls and func in _UNITTEST_MODULE_HOOKS:
            return [
                t for t in by_file.get(rel, [])
                if "::" in t["name"] and classes.is_unittest(rel, _class_of(t))
            ]
        if not cls and func in _FUNCTION_HOOKS:
            return [t for t in by_file.get(rel, []) if "::" not in t["name"]]
        return []

    def through_fixture(start: dict[str, Any], via: list[str], level: int) -> None:
        """Every test that gets fixture ``start``: the requests of its name that pytest
        resolves to this very definition, the requests of a fixture that does, and, for an
        autouse fixture, every test it runs for. pytest resolves a fixture's own arguments for
        the test that uses it, so each link of a chain is checked from that test (``needs``),
        except a fixture requesting its own name, which gets the one it overrides."""
        nonlocal truncated
        todo: list[tuple[dict[str, Any], list[str], tuple[dict[str, Any], ...]]] = [
            (start, via, ())
        ]
        while todo:
            fixture, path, needs = todo.pop()
            identity = (fixture["file"], fixture["cls"], fixture["func"], *map(id, needs))
            if identity in expanded:
                continue
            if len(expanded) >= MAX_FIXTURES:
                truncated = True
                return
            expanded.add(identity)
            name = fixture["name"]
            # pytest requests an autouse name for every test in the autouse fixture's reach,
            # and the request resolves like any other: to this fixture where it overrides it.
            # The reach depends on the name alone: built once, not for each fixture of a chain.
            autoused = autoused_by_name.get(name)
            if autoused is None:
                autoused = autoused_by_name[name] = list({
                    (r["file"], r["name"]): (kind, r, cls)
                    for each in fx.named.get(name, []) if each.get("autouse")
                    for kind, r, cls in fx.autouse_tests(each)
                }.values())
            chain = (fixture, *needs)
            # Whether the chain is what a test gets depends on its file and class alone: the
            # tests of one class are checked once.
            chain_visible: dict[tuple[str, str], bool] = {}
            for kind, record, cls in [*autoused, *fx.requests.get(name, [])]:
                work[0] -= 1
                if work[0] < 0:
                    truncated = True
                    return
                if kind == "fixture":
                    step = _step(f"fixture {record['name']}", path)
                    if record["name"] != name:
                        todo.append((record, step, (*needs, fixture)))
                    elif fx.visible(name, record["file"], cls, record) is fixture:
                        todo.append((record, step, needs))
                    continue
                parametrized = record["parametrized"]
                if parametrized and any(f["name"] in parametrized for f in chain):
                    continue
                where = (record["file"], cls)
                visible = chain_visible.get(where)
                if visible is None:
                    visible = chain_visible[where] = all(
                        fx.visible(f["name"], record["file"], cls) is f for f in chain
                    )
                if not visible:
                    continue
                how = "autouse fixture" if kind == "autouse" else "fixture"
                reach(record, path, f"{how} {name}", level)

    # pytest runs a fixture or a setup hook without a call site, so a target that is one
    # reaches its tests directly.
    for target in targets:
        found_def = index.definition(target)
        if found_def is None:
            continue
        rel, row = found_def
        cls, _, func = row["name"].replace(".", "::").rpartition("::")
        fixture = fx.at.get((rel, cls, func))
        if fixture is not None:
            through_fixture(fixture, [target], 1)
            continue
        for t in hooked(rel, cls, func):
            reach(t, [target], f"setup hook {func}", 1)

    frontier = list(targets)
    # A constructor named as the target runs wherever its class is called.
    for target in targets:
        for name in constructed(target):
            if name not in parent:
                parent[name] = (target, "")
                frontier.append(name)
    for level in range(1, depth + 1):
        following: list[str] = []
        for target in frontier:
            found = code_static.references(index, [target], budget=budget)
            truncated = truncated or found["truncated"]
            counts["values"] += found["counts"]["uses"]
            counts["by_name"] += found["counts"]["candidates"]
            for p in found["patches"]:
                if not p["test"]:
                    continue
                qual = _qualname(index, p["file"], p["scope"])
                names = (
                    [qual] if (p["file"], qual) in tests_in
                    else decorators.tests(p["file"], p["line"])
                )
                # The copies of a patched test method in inheriting classes are patched too.
                for rel, name in [
                    (p["file"], n) for n in names
                ] + [
                    (c["file"], c["name"]) for n in names for c in copies.get((p["file"], n), [])
                ]:
                    node = f"{rel}::{name}"
                    patched.setdefault(
                        node, {"test": node, "file": rel, "line": p["line"], "target": target},
                    )
            for call in found["calls"]:
                caller = _caller(index, call["file"], call["scope"])
                if not caller:
                    counts["module_level"] += 1
                    continue
                where = f"{call['file']}:{call['line']}"
                qual = _qualname(index, call["file"], call["scope"])
                cls, _, func = qual.rpartition("::")
                test = tests_in.get((call["file"], qual))
                # A test method also runs as each copy pytest collects in an inheriting class,
                # from a base pytest does not collect itself (``class Contract``) too.
                inherited = copies.get((call["file"], qual), [])
                for t in [test] if test is not None else []:
                    reach(t, chain(target), f"call at {where}", level)
                for t in inherited:
                    reach(t, chain(target), f"call at {where}", level)
                if test is not None or inherited:
                    continue
                fixture = fx.at.get((call["file"], cls, func))
                if fixture is not None:
                    through_fixture(
                        fixture, _step(f"fixture {fixture['name']}", chain(target)), level
                    )
                    continue
                hook_tests = hooked(call["file"], cls, func)
                for t in hook_tests:
                    reach(t, _step(caller, chain(target)), f"setup hook {func}", level)
                if hook_tests:
                    continue
                follow = [caller, *constructed(caller)]
                for name in follow:
                    if name in parent:
                        continue
                    if len(parent) - len(targets) >= MAX_FOLLOWED:
                        truncated = True
                        continue
                    parent[name] = (target, where) if name == caller else (caller, where)
                    following.append(name)
        counts["functions"] += len(following)
        frontier = following
    # Callers found at the last level whose own callers were not looked at.
    counts["beyond_depth"] = len(frontier)
    # A test that patches a target does not run the real one, whatever else it calls.
    tests = sorted(
        (r for node, r in reached.items() if node not in patched),
        key=lambda r: (r["depth"], r["test"]),
    )
    return {
        "tests": tests, "patched": sorted(patched.values(), key=lambda r: r["test"]),
        "counts": counts, "depth": depth,
        "truncated": truncated or budget[0] <= 0 or classes.cut,
    }
