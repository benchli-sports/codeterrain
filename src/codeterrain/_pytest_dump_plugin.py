"""pytest plugin, loaded into the TARGET's pytest run: dumps each test's fixture setup order.

Run as ``python -m pytest --collect-only -q -p codeterrain._pytest_dump_plugin``; it writes a
``codeterrain.order/1`` document (docs/extractor-protocol.md) to ``$CODETERRAIN_RESULT``. Items that
share a fixture closure share one entry; ``nodes`` maps every node id to its closure.

Only stdlib and pytest are imported, so any pytest 7+ environment can host it.
"""

from __future__ import annotations

import inspect
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

SCHEMA = "codeterrain.order/1"


def closure_of(item: Any) -> tuple[list[str], dict[str, Any]]:
    """(names_closure, name -> tuple of FixtureDef) for ``item``.

    Private API, isolated here: ``item._fixtureinfo``. Public fallback when it is absent:
    ``item.fixturenames`` plus the fixture manager's ``getfixturedefs``.
    """
    info = getattr(item, "_fixtureinfo", None)
    if info is not None and hasattr(info, "names_closure") and hasattr(info, "name2fixturedefs"):
        return list(info.names_closure), dict(info.name2fixturedefs)
    names = list(item.fixturenames)
    manager = item.session._fixturemanager
    defs: dict[str, Any] = {}
    for name in names:
        found = manager.getfixturedefs(name, item)
        if found:
            defs[name] = tuple(found)
    return names, defs


def lookup_fixturedefs(item: Any, name: str) -> tuple[Any, ...]:
    """The fixture definitions visible to ``item`` under ``name`` (public manager API)."""
    manager = getattr(getattr(item, "session", None), "_fixturemanager", None)
    if manager is None:
        return ()
    try:
        found = manager.getfixturedefs(name, item)
    except (TypeError, AttributeError):
        found = manager.getfixturedefs(name, item.nodeid)
    return tuple(found or ())


def setup_order(
    names: list[str],
    defs: dict[str, Any],
    resolve: Callable[[str], tuple[Any, ...]] | None = None,
) -> list[Any]:
    """FixtureDefs in the order pytest sets them up: each requested name in closure order,
    dependencies first.

    Overrides resolve as pytest does: a request for ``name`` takes the definition one level up for
    every fixture of that name still being set up in the chain of requests, however many other
    fixtures sit between (an override requesting a helper that requests the same name gets the
    parent). A name whose fixture already finished is not resolved again.

    ``resolve`` finds a name the closure does not list: pytest 8 leaves out what only an
    overridden parent definition requests, and resolves it lazily when the parent is set up."""
    order: list[Any] = []
    finished: set[str] = set()
    chain: list[str] = []

    def visit(name: str) -> None:
        if name in finished:
            return
        candidates = defs.get(name)
        if not candidates and resolve is not None:
            candidates = defs[name] = resolve(name)
        index = -1 - chain.count(name)
        if not candidates or -index > len(candidates):
            return
        fixturedef = candidates[index]
        chain.append(name)
        for dep in fixturedef.argnames:
            if dep != "request":
                visit(dep)
        chain.pop()
        order.append(fixturedef)
        finished.add(name)

    for name in names:
        visit(name)
    return order


def _location(func: Any, root: Path) -> tuple[str, int]:
    try:
        code = inspect.unwrap(func).__code__
    except (AttributeError, ValueError):
        return "", 0
    real = Path(os.path.realpath(code.co_filename))
    try:
        return real.relative_to(root).as_posix(), code.co_firstlineno
    except ValueError:
        return real.as_posix(), code.co_firstlineno


def param_scopes(item: Any) -> dict[str, str]:
    """name -> the scope pytest runs a parametrized argument at, from the item's callspec.

    ``parametrize(..., indirect=True, scope="module")`` runs a function-scoped fixture at module
    scope. ``CallSpec2._arg2scope`` is private; without it the declared scope is reported.
    """
    raw = getattr(getattr(item, "callspec", None), "_arg2scope", None)
    if not isinstance(raw, dict):
        return {}
    scopes = {name: getattr(scope, "value", scope) for name, scope in raw.items()}
    return {name: scope for name, scope in scopes.items() if isinstance(scope, str)}


def _entry(fixturedef: Any, root: Path, scope: str | None = None) -> dict[str, Any]:
    file, line = _location(fixturedef.func, root)
    return {
        "name": fixturedef.argname,
        "scope": scope or fixturedef.scope,
        "file": file,
        "line": line,
        "params": bool(getattr(fixturedef, "params", None)),
    }


def build_document(
    items: list[Any], root: Path, errors: int, pytest_version: str
) -> dict[str, Any]:
    closures: list[list[dict[str, Any]]] = []
    index: dict[str, int] = {}
    nodes: dict[str, int] = {}
    for item in items:
        names, defs = closure_of(item)
        found = setup_order(names, defs, lambda name, item=item: lookup_fixturedefs(item, name))
        # The parameter reaches the nearest definition of a name, the last one set up.
        scopes = param_scopes(item)
        last = {fd.argname: i for i, fd in enumerate(found)}
        entries = [
            _entry(fd, root, scopes.get(fd.argname) if last[fd.argname] == i else None)
            for i, fd in enumerate(found)
        ]
        key = json.dumps(entries, sort_keys=True)
        if key not in index:
            index[key] = len(closures)
            closures.append(entries)
        nodes[item.nodeid] = index[key]
    return {
        "schema": SCHEMA,
        "generator": {"name": "codeterrain-pytest-dump", "pytest": pytest_version},
        "closures": closures,
        "nodes": nodes,
        "errors": errors,
    }


#: (pytest option name, flag) pairs: options whose value makes pytest write a file when set.
REPORT_OPTIONS = (
    ("xmlpath", "--junitxml"),
    ("htmlpath", "--html"),
    ("log_file", "--log-file"),
    ("debug", "--debug"),
    ("report_log", "--report-log"),
    ("json_report", "--json-report"),
    ("allure_report_dir", "--alluredir"),
)


def _writes_reports(config: Any) -> list[str]:
    """Options in force that make pytest write report files (into the target, by default)."""
    found: list[str] = [
        flag for option, flag in REPORT_OPTIONS if config.getoption(option, None)
    ]
    cov = config.getoption("cov_report", None)
    # A tuple, not ``list | tuple``: this module runs in the target's interpreter (3.9 and up).
    names = cov.keys() if isinstance(cov, dict) else cov if isinstance(cov, (list, tuple)) else []  # noqa: UP038
    if {str(n).split(":", 1)[0] for n in names} - {"term", "term-missing"}:
        found.append("--cov-report")
    return found


class _Namespace:
    """``getoption`` over the early argparse namespace, before the config is fully parsed."""

    def __init__(self, namespace: Any, early_config: Any = None) -> None:
        self.namespace = namespace
        self.early_config = early_config

    def getoption(self, name: str, default: Any = None) -> Any:
        return getattr(self.namespace, name, default)

    def getini(self, name: str) -> Any:
        return self.early_config.getini(name)


def _ini_log_file(config: Any) -> str:
    """The ``log_file`` ini value after every ``-o`` override; empty when it cannot be read."""
    try:
        return str(config.getini("log_file") or "")
    except (AttributeError, ValueError):
        return ""


def _refuse_report_options(config: Any) -> None:
    """Refuse a run whose options make pytest write files, whichever source supplied them.

    codeterrain blanks ``log_file`` and switches ``addopts`` off; what is left is ``pytest_args``
    (user config only) and, under ``respect_addopts``, the project's own options.
    """
    import pytest

    active = _writes_reports(config)
    if _ini_log_file(config):
        active.append("-o log_file=...")
    if not active:
        return
    if os.environ.get("CODETERRAIN_RESPECT_ADDOPTS") == "1":
        raise pytest.UsageError(
            f"codeterrain: respect_addopts is on but the project's options ({', '.join(active)}) "
            "make pytest write report files into the repository; remove them for the "
            "extraction run or set respect_addopts = false"
        )
    raise pytest.UsageError(
        f"codeterrain: the extraction options ({', '.join(active)}) make pytest write files "
        "into the repository; remove them from pytest_args"
    )


def pytest_load_initial_conftests(early_config: Any, parser: Any = None, args: Any = None) -> None:
    """Refuse before pytest opens ``--debug``'s log file, which happens right after parsing."""
    namespace = getattr(early_config, "known_args_namespace", None)
    if namespace is not None:
        _refuse_report_options(_Namespace(namespace, early_config))


def pytest_configure(config: Any) -> None:
    _refuse_report_options(config)


def pytest_collection_finish(session: Any) -> None:
    import pytest

    target = os.environ.get("CODETERRAIN_RESULT")
    if not target:
        return
    root = Path(os.path.realpath(os.environ.get("CODETERRAIN_ROOT") or session.config.rootpath))
    document = build_document(list(session.items), root, session.testsfailed, pytest.__version__)
    # bytes, not write_text(newline=...), which needs 3.10 in the target's interpreter
    Path(target).write_bytes(json.dumps(document).encode("utf-8"))
