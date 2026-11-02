"""Static pytest fixture facts from ``ast``: no imports, no pytest run, instant when warm.

Per file the pass records every fixture (scope, autouse, params, dependencies, location) and every
test function (the fixtures it requests). The facts are keyed by content hash in the cache, so an
edit re-parses one file. Overrides and dependents are resolved by name from those facts; they are a
static approximation of pytest's visibility rules (plugin and ``pytest_plugins`` fixtures are not
seen, and a fixture defined outside ``test_paths`` is not either).
"""

from __future__ import annotations

import ast
import os
from pathlib import PurePosixPath
from typing import Any

from codeterrain.cache import Cache, ViewRecord
from codeterrain.config import Config

VIEW = "fixtures"
OBJECT_KIND = "static"
#: The shape of the view's rows: bump it when they change, so a signed view of the old shape is
#: rebuilt rather than read unchecked.
VIEW_FORMAT = "fixtures-1"
SCOPE_LETTER = {"session": "S", "package": "P", "module": "M", "class": "C", "function": "F"}
_SELF_NAMES = {"self", "cls"}
#: field -> expected type; "strs" is a list of strings, "int" excludes bool.
FIXTURE_FACT_TYPES = {
    "name": str,
    "func": str,
    "scope": str,
    "autouse": bool,
    "params": "int",
    "argnames": "strs",
    "line": "int",
    "cls": str,
    "dyn": "strs",
}
TEST_FACT_TYPES = {
    "name": str,
    "line": "int",
    "argnames": "strs",
    "usefixtures": "strs",
    "dyn": "strs",
    "parametrized": "strs",
}
#: The marks written on one thing, not inherited: ``name`` is "" for the module (``pytestmark``),
#: a class (``Outer::Inner``: its decorators and body ``pytestmark``), or a test method of any
#: class (``Base::test_it``: its decorators). ``use`` are usefixtures names, ``direct`` the
#: directly parametrized ones. A test copied into a subclass is given its marks from these.
MARK_FACT_TYPES = {"name": str, "use": "strs", "direct": "strs"}
FIXTURE_FACT_KEYS = frozenset(FIXTURE_FACT_TYPES)
TEST_FACT_KEYS = frozenset(TEST_FACT_TYPES)


def _dotted(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _call_parts(node: ast.expr) -> tuple[str, list[ast.expr], dict[str, ast.expr]]:
    if isinstance(node, ast.Call):
        kwargs = {k.arg: k.value for k in node.keywords if k.arg}
        return _dotted(node.func), list(node.args), kwargs
    return _dotted(node), [], {}


def _const(node: ast.expr | None) -> Any:
    return node.value if isinstance(node, ast.Constant) else None


def _fixture_marker(decorators: list[ast.expr]) -> tuple[ast.expr, dict[str, ast.expr]] | None:
    for dec in decorators:
        name, _, kwargs = _call_parts(dec)
        if name.rsplit(".", 1)[-1] in {"fixture", "yield_fixture"}:
            return dec, kwargs
    return None


def _argnames(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Parameters pytest treats as fixture requests: no default, not self/cls."""
    args = fn.args
    positional = args.posonlyargs + args.args
    without_default = positional[: len(positional) - len(args.defaults)]
    keyword_only = [a for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True) if d is None]
    return [a.arg for a in without_default + keyword_only if a.arg not in _SELF_NAMES]


def _str_list(node: ast.expr | None) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [part.strip() for part in node.value.split(",") if part.strip()]
    if isinstance(node, ast.List | ast.Tuple):
        return [
            e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)
        ]
    return []


def _marks(decorators: list[ast.expr]) -> tuple[list[str], list[str]]:
    """(usefixtures names, direct-parametrize names) from a decorator list."""
    use: list[str] = []
    direct: list[str] = []
    for dec in decorators:
        name, args, kwargs = _call_parts(dec)
        if name.endswith("mark.usefixtures"):
            for arg in args:
                use.extend(_str_list(arg))
        elif name.endswith("mark.parametrize"):
            names = args[0] if args else kwargs.get("argnames")
            if names is not None:
                through = _indirect_names(args, kwargs, names)
                direct.extend(n for n in _str_list(names) if n not in through)
    return use, direct


def _indirect_names(
    args: list[ast.expr], kwargs: dict[str, ast.expr], names: ast.expr
) -> set[str]:
    """Parametrized names that go through a fixture (``indirect=True`` or a list of names), given
    as the keyword or as the third positional argument, as in
    ``parametrize(argnames, argvalues, indirect)``."""
    indirect = args[2] if len(args) >= 3 else kwargs.get("indirect")
    if indirect is None or _const(indirect) is False:
        return set()
    if isinstance(indirect, ast.List | ast.Tuple):
        return set(_str_list(indirect))
    return set(_str_list(names))


def _pytestmark(body: list[ast.stmt]) -> tuple[list[str], list[str]]:
    """(usefixtures names, direct-parametrize names) from ``pytestmark = <mark | [marks]>``."""
    use: list[str] = []
    direct: list[str] = []
    for stmt in body:
        if isinstance(stmt, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "pytestmark" for t in stmt.targets
        ):
            value = stmt.value
            marks = value.elts if isinstance(value, ast.List | ast.Tuple) else [value]
            found_use, found_direct = _marks(list(marks))
            use.extend(found_use)
            direct.extend(found_direct)
    return use, direct


def _dynamic_requests(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    found: list[str] = []
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"getfixturevalue", "getfuncargvalue"}
            and node.args
            and isinstance(_const(node.args[0]), str)
        ):
            found.append(node.args[0].value)
    return sorted(set(found))


def _first_line(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    return min([fn.lineno, *(d.lineno for d in fn.decorator_list)])


def _fixture_fact(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    marker: tuple[ast.expr, dict[str, ast.expr]],
    cls: str,
) -> dict[str, Any]:
    kwargs = marker[1]
    scope = _const(kwargs.get("scope")) if "scope" in kwargs else "function"
    params = kwargs.get("params")
    n_params = 0
    if params is not None:
        n_params = len(params.elts) if isinstance(params, ast.List | ast.Tuple) else -1
    return {
        "name": _const(kwargs.get("name")) or fn.name,
        "func": fn.name,
        "scope": scope if isinstance(scope, str) else "dynamic",
        "autouse": _const(kwargs.get("autouse")) is True,
        "params": n_params,
        "argnames": _argnames(fn),
        "line": _first_line(fn),
        "cls": cls,
        "dyn": _dynamic_requests(fn),
    }


def _test_fact(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    cls: str,
    inherited_use: list[str],
    inherited_direct: list[str],
) -> dict[str, Any]:
    use, direct = _marks(fn.decorator_list)
    supplied = {*direct, *inherited_direct}
    return {
        "name": f"{cls}::{fn.name}" if cls else fn.name,
        "line": _first_line(fn),
        "argnames": [a for a in _argnames(fn) if a not in supplied],
        # A directly parametrized name is supplied by the parameter, not the fixture, even when a
        # usefixtures mark also names it.
        "usefixtures": sorted(set(use + inherited_use) - supplied),
        "dyn": _dynamic_requests(fn),
        # pytest gives these names the parameter everywhere in the test's fixture closure, so a
        # fixture of the same name never runs for it, not even through another fixture.
        "parametrized": sorted(supplied),
    }


def parse_source(source: str) -> dict[str, Any]:
    """Facts for one file's text: ``{"fixtures": [...], "tests": [...], "error": str|None}``."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        # A deep expression (a generated file, or a hostile one) exhausts the parser: that file
        # becomes a recorded parse error and the rest of the repository still answers.
        return {
            "fixtures": [], "tests": [], "marks": [], "error": f"{type(exc).__name__}: {exc}"
        }
    fixtures: list[dict[str, Any]] = []
    tests: list[dict[str, Any]] = []
    marks: list[dict[str, Any]] = []
    module_use, module_direct = _pytestmark(tree.body)

    def mark(name: str, use: list[str], direct: list[str]) -> None:
        if use or direct:
            marks.append({"name": name, "use": sorted(set(use)), "direct": sorted(set(direct))})

    mark("", module_use, module_direct)

    def visit(
        fn: ast.FunctionDef | ast.AsyncFunctionDef,
        cls: str,
        class_use: list[str],
        class_direct: list[str],
    ) -> None:
        marker = _fixture_marker(fn.decorator_list)
        if marker:
            fixtures.append(_fixture_fact(fn, marker, cls))
        elif fn.name.startswith("test"):
            tests.append(
                _test_fact(fn, cls, module_use + class_use, module_direct + class_direct)
            )

    def visit_class(
        node: ast.ClassDef, outer: str, outer_use: list[str], outer_direct: list[str]
    ) -> None:
        # Nested test classes are collected too (TestOuter::TestInner) and inherit the outer
        # class's marks.
        name = f"{outer}::{node.name}" if outer else node.name
        decorated_use, decorated_direct = _marks(node.decorator_list)
        body_use, body_direct = _pytestmark(node.body)
        mark(name, decorated_use + body_use, decorated_direct + body_direct)
        class_use = outer_use + decorated_use + body_use
        class_direct = outer_direct + decorated_direct + body_direct
        for member in node.body:
            if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
                if member.name.startswith("test"):
                    # Kept for every class, collected or not: a subclass that inherits the
                    # method runs it with these marks.
                    mark(f"{name}::{member.name}", *_marks(member.decorator_list))
                if node.name.startswith("Test") or _fixture_marker(member.decorator_list):
                    visit(member, name, class_use, class_direct)
            elif (
                isinstance(member, ast.ClassDef)
                and node.name.startswith("Test")
                and member.name.startswith("Test")
            ):
                visit_class(member, name, class_use, class_direct)

    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            visit(node, "", [], [])
        elif isinstance(node, ast.ClassDef):
            visit_class(node, "", [], [])
    return {"fixtures": fixtures, "tests": tests, "marks": marks, "error": None}


#: How many top-level entries ``unlisted_test_trees`` looks at, and how many entries of each
#: directory: a listing, not a walk.
MAX_TREE_ENTRIES = 500
_NOT_TEST_TREES = frozenset({"node_modules", "venv", "site-packages", "build", "dist"})


def unlisted_test_trees(config: Config) -> list[str]:
    """Top-level directories outside ``test_paths`` that hold a ``conftest.py`` or test modules
    directly: a second test tree (a live suite with its own conftest chain) that the fixture
    view does not read. Symbolic links and hidden directories are skipped."""
    listed = [PurePosixPath(p) for p in config.test_paths]
    if any(p in (PurePosixPath("."), PurePosixPath("")) for p in listed):
        return []
    out = []
    try:
        with os.scandir(config.root) as top:
            entries = [e for _, e in zip(range(MAX_TREE_ENTRIES), top, strict=False)]
    except OSError:
        return []
    for entry in sorted(entries, key=lambda e: e.name):
        name = entry.name
        if name.startswith((".", "_")) or name in _NOT_TEST_TREES:
            continue
        try:
            if entry.is_symlink() or not entry.is_dir():
                continue
        except OSError:
            continue
        here = PurePosixPath(name)
        # A test path inside it (`backend/tests`), or it inside a test path, is read already.
        if any(p == here or here in p.parents or p in here.parents for p in listed):
            continue
        try:
            with os.scandir(entry.path) as inner:
                names = [e.name for _, e in zip(range(MAX_TREE_ENTRIES), inner, strict=False)]
        except OSError:
            continue
        if "conftest.py" in names or any(is_test_module(n) for n in names):
            out.append(name)
    return out


def is_test_module(rel: str) -> bool:
    name = PurePosixPath(rel).name
    return (name.startswith("test_") or name.endswith("_test.py")) and name.endswith(".py")


def _directory(rel: str) -> PurePosixPath:
    return PurePosixPath(rel).parent


def _resolve_overrides(fixtures: list[dict[str, Any]]) -> None:
    """Point each fixture at the nearest same-named conftest fixture it shadows, if any."""
    conftests: dict[str, list[dict[str, Any]]] = {}
    for fx in fixtures:
        if PurePosixPath(fx["file"]).name == "conftest.py":
            conftests.setdefault(fx["name"], []).append(fx)
    for fx in fixtures:
        here = _directory(fx["file"])
        best: dict[str, Any] | None = None
        for cand in conftests.get(fx["name"], []):
            if cand["file"] == fx["file"]:
                continue
            there = _directory(cand["file"])
            if there == here or there in here.parents:
                if best is None or len(_directory(best["file"]).parts) < len(there.parts):
                    best = cand
        fx["overrides"] = f"{best['file']}:{best['line']}" if best else ""


def _typed(entry: Any, types: dict[str, Any]) -> bool:
    if not isinstance(entry, dict):
        return False
    for field, kind in types.items():
        value = entry.get(field)
        if kind == "int":
            ok = isinstance(value, int) and not isinstance(value, bool)
        elif kind == "strs":
            ok = isinstance(value, list) and all(isinstance(v, str) for v in value)
        else:
            ok = isinstance(value, kind)
        if not ok:
            return False
    return True


def _valid_facts(facts: Any) -> bool:
    """A cached per-file entry has every field this version reads, of the right type (anything
    else is re-parsed)."""
    return (
        isinstance(facts, dict)
        and (facts.get("error") is None or isinstance(facts.get("error"), str))
        and "error" in facts
        and isinstance(facts.get("fixtures"), list)
        and isinstance(facts.get("tests"), list)
        and isinstance(facts.get("marks"), list)
        and all(_typed(f, FIXTURE_FACT_TYPES) for f in facts["fixtures"])
        and all(_typed(t, TEST_FACT_TYPES) for t in facts["tests"])
        and all(_typed(m, MARK_FACT_TYPES) for m in facts["marks"])
    )


def _valid_view(data: Any) -> bool:
    return (
        isinstance(data, dict)
        and data.get("format") == VIEW_FORMAT
        and all(isinstance(data.get(k), list) for k in ("fixtures", "tests", "marks", "errors"))
        and all(_typed(f, {**FIXTURE_FACT_TYPES, "file": str, "overrides": str})
                for f in data["fixtures"])
        and all(_typed(t, {**TEST_FACT_TYPES, "file": str}) for t in data["tests"])
        and all(_typed(m, {**MARK_FACT_TYPES, "file": str}) for m in data["marks"])
        and all(_typed(e, {"file": str, "error": str}) for e in data["errors"])
    )


def build(cache: Cache, config: Config, force: bool = False) -> tuple[ViewRecord, bool]:
    """The ``fixtures`` view, rebuilt incrementally when any test file changed.

    Returns (record, rebuilt).
    """
    fingerprint = cache.scan(config.test_paths)
    record = cache.read_view(VIEW)
    if (
        record is not None
        and not force
        and record.fingerprint == fingerprint
        and (record := cache.served(record, VIEW_FORMAT, _valid_view)) is not None
    ):
        cache.flush()
        return record, False
    objects = cache.load_objects(OBJECT_KIND)
    added = False
    for rel, sha in fingerprint.items():
        if not _valid_facts(objects.get(sha)):
            text = (cache.root / rel).read_bytes().decode("utf-8", errors="replace")
            objects[sha] = parse_source(text)
            added = True
    if added:
        cache.save_objects(OBJECT_KIND, objects)
    fixtures: list[dict[str, Any]] = []
    tests: list[dict[str, Any]] = []
    marks: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for rel in sorted(fingerprint):
        facts = objects[fingerprint[rel]]
        if facts["error"]:
            errors.append({"file": rel, "error": facts["error"]})
        fixtures.extend({**fx, "file": rel} for fx in facts["fixtures"])
        # Marks of every file: a test class inherits from bases in other modules.
        marks.extend({**m, "file": rel} for m in facts["marks"])
        if is_test_module(rel):
            tests.extend({**t, "file": rel} for t in facts["tests"])
    _resolve_overrides(fixtures)
    fixtures.sort(key=lambda f: (f["name"], f["file"], f["line"]))
    record = cache.write_view(
        VIEW,
        {
            "format": VIEW_FORMAT, "fixtures": fixtures, "tests": tests, "marks": marks,
            "errors": errors,
        },
        fingerprint,
        sign=True,  # built from checked facts: a warm read need not check every row again
    )
    cache.flush()
    return record, True


def scope_letter(scope: str) -> str:
    return SCOPE_LETTER.get(scope, "?")


def normalise_scope(text: str) -> str:
    """``--scope`` accepts a scope word or its S/P/M/C/F letter."""
    lowered = text.lower()
    for word, letter in SCOPE_LETTER.items():
        if lowered in (word, letter.lower()):
            return word
    raise ValueError(f"unknown scope {text!r}; use session, package, module, class or function")


def select(
    data: dict[str, Any], substring: str = "", autouse: bool = False, scope: str | None = None
) -> list[dict[str, Any]]:
    needle = substring.lower()
    return [
        fx
        for fx in data["fixtures"]
        if needle in fx["name"].lower()
        and (not autouse or fx["autouse"])
        and (scope is None or fx["scope"] == scope)
    ]


def dependents(data: dict[str, Any], name: str) -> list[dict[str, Any]]:
    """Fixtures and tests that request ``name`` (by argument, usefixtures or getfixturevalue)."""
    out: list[dict[str, Any]] = []
    for fx in data["fixtures"]:
        how = _how(name, fx["argnames"], [], fx["dyn"])
        if how:
            out.append({"kind": "fixture", "name": fx["name"], "file": fx["file"],
                        "line": fx["line"], "via": how})
    for test in data["tests"]:
        how = _how(name, test["argnames"], test["usefixtures"], test["dyn"])
        if how:
            out.append({"kind": "test", "name": test["name"], "file": test["file"],
                        "line": test["line"], "via": how})
    return out


def _how(name: str, args: list[str], use: list[str], dyn: list[str]) -> str:
    if name in args:
        return "arg"
    if name in use:
        return "usefixtures"
    if name in dyn:
        return "getfixturevalue"
    return ""
