"""Which routes reach a function that raises, and with what HTTP status.

Three facts are joined. The exceptions a function raises, and the status a raise site states, are
read with ``ast`` from its own body. The routes that reach it come from walking its callers up the
code index, the way ``refs`` resolves them, and through FastAPI dependencies: a function passed to
``Depends(...)`` or ``Security(...)`` in another function's signature counts as called by it, and a
route's own dependency callables (route, router and include-time, from the routes view) count as
called by the route. The status comes from the exception handlers the app registered (routes view,
import tier): the handler of the raise site's status code, else of the exception's nearest class
in its method resolution order, the static one within the repository and then the loaded classes
the handler's class has below it; no handler means 500. A handler's status is read from its body:
the literal ``status_code`` it responds with, or the exception's own for one that passes
``exc.status_code`` on, as FastAPI's default handler does.
"""

from __future__ import annotations

import ast
import builtins
import os
import re
from pathlib import Path
from typing import Any

from codeterrain import code_static, impact
from codeterrain.cache import _contains

#: How many levels of callers and dependencies are followed by default, and at most.
DEFAULT_DEPTH = 6
MAX_DEPTH = 12
#: Functions followed up the call graph, at most.
MAX_FOLLOWED = 2_000
#: The names a FastAPI dependency is declared with.
_DEPENDS = frozenset({"Depends", "Security"})
#: FastAPI's own handlers, which live outside the repository: what status each answers with.
_FRAMEWORK_HANDLERS = {
    "fastapi.exception_handlers.http_exception_handler": "raised",
    "fastapi.exception_handlers.request_validation_exception_handler": 422,
    "fastapi.exception_handlers.websocket_request_validation_exception_handler": 1008,
}
_CATCH_ALL = frozenset({"builtins.Exception", "builtins.BaseException"})
_STATUS_NAME = re.compile(r"HTTP_(\d{3})_")


def _status(node: ast.expr | None) -> int | None:
    """A status code written as an integer or as a ``status.HTTP_404_NOT_FOUND`` constant."""
    if isinstance(node, ast.Constant) and type(node.value) is int:
        return node.value
    name = node.attr if isinstance(node, ast.Attribute) else (
        node.id if isinstance(node, ast.Name) else ""
    )
    found = _STATUS_NAME.match(name)
    return int(found.group(1)) if found else None


def _call_status(call: ast.Call) -> int | None:
    for kw in call.keywords:
        if kw.arg == "status_code":
            return _status(kw.value)
    return None


class Trees:
    """Each file's tree, parsed once, and its functions by qualified name."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._funcs: dict[str, dict[str, ast.FunctionDef | ast.AsyncFunctionDef]] = {}
        self._classes: dict[str, dict[str, ast.ClassDef]] = {}

    def _load(self, rel: str) -> None:
        if rel in self._funcs:
            return
        funcs: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        classes: dict[str, ast.ClassDef] = {}
        tree = None
        path = Path(os.path.realpath(self.root / rel))
        # A handler's file comes from the child that imported the target: only a file inside
        # the repository is read.
        if _contains(Path(os.path.realpath(self.root)), path) and path.suffix == ".py":
            try:
                data = path.read_bytes()
                tree = ast.parse(data) if len(data) <= code_static.MAX_FILE_BYTES else None
            except (OSError, SyntaxError, ValueError, RecursionError, MemoryError):
                tree = None

        def walk(nodes: list[ast.stmt], prefix: str) -> None:
            for node in nodes:
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    funcs[f"{prefix}{node.name}"] = node
                    walk(node.body, f"{prefix}{node.name}.")
                elif isinstance(node, ast.ClassDef):
                    classes[f"{prefix}{node.name}"] = node
                    walk(node.body, f"{prefix}{node.name}.")

        if tree is not None:
            walk(tree.body, "")
        self._funcs[rel], self._classes[rel] = funcs, classes

    def function(self, rel: str, qual: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        self._load(rel)
        return self._funcs[rel].get(qual)

    def cls(self, rel: str, qual: str) -> ast.ClassDef | None:
        self._load(rel)
        return self._classes[rel].get(qual)

    def signature_owner(self, rel: str, line: int) -> str:
        """The qualified name of the function whose signature (decorators to the line before its
        body) holds ``line``: where a ``Depends(...)`` there belongs."""
        self._load(rel)
        best = ""
        for qual, fn in self._funcs[rel].items():
            start = min([fn.lineno, *(d.lineno for d in fn.decorator_list)])
            if start <= line < fn.body[0].lineno and len(qual) > len(best):
                best = qual
        return best


def _own_nodes(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.AST]:
    """The nodes of ``fn``'s body, not of the functions and classes defined inside it."""
    nested = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda
    out: list[ast.AST] = []
    todo: list[ast.AST] = [n for n in fn.body if not isinstance(n, nested)]
    while todo:
        node = todo.pop()
        out.append(node)
        todo.extend(c for c in ast.iter_child_nodes(node) if not isinstance(c, nested))
    return out


def _resolve(index: code_static.Index, rel: str, scope: str, dotted: str) -> str:
    head, _, rest = dotted.partition(".")
    bound = index.binding(rel, scope, head)
    if bound is not None:
        return index.resolve(".".join(filter(None, (bound, rest))))
    module = code_static.module_name(rel, index.src_package)
    local = index.resolve(f"{module}.{dotted}")
    if index.definition(local) is not None:
        return local
    if not rest and isinstance(getattr(builtins, head, None), type):
        return f"builtins.{head}"
    return dotted


def raised(
    index: code_static.Index, trees: Trees, full: str
) -> list[dict[str, Any]]:
    """The exceptions ``full`` raises in its own body: ``exception`` (full name), ``line`` and
    ``status`` (the status code the raise site or the exception's class states, else None)."""
    found = index.definition(full)
    if found is None:
        return []
    rel, row = found
    fn = trees.function(rel, row["name"])
    if fn is None:
        return []
    out = []
    for node in _own_nodes(fn):
        if not isinstance(node, ast.Raise) or node.exc is None:
            continue
        exc = node.exc
        call = exc if isinstance(exc, ast.Call) else None
        dotted = code_static._dotted(call.func if call else exc)
        if not dotted:
            continue
        name = _resolve(index, rel, row["name"], dotted)
        status = None
        if call is not None:
            status = _call_status(call)
            if status is None and call.args and name.rpartition(".")[2] == "HTTPException":
                status = _status(call.args[0])
        if status is None:
            status = class_status(index, trees, name)
        out.append({"exception": name, "file": rel, "line": node.lineno, "status": status})
    out.sort(key=lambda r: r["line"])
    return out


def class_status(index: code_static.Index, trees: Trees, full: str) -> int | None:
    """A status the class or a base in the repository states: ``status_code = 404`` in its body,
    or ``status_code=404`` in a call in its own methods (``super().__init__(status_code=404)``)."""
    for rel, cls in _mro(index, full):
        node = trees.cls(rel, cls.replace("::", "."))
        if node is None:
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "status_code" for t in stmt.targets
            ):
                found = _status(stmt.value)
                if found is not None:
                    return found
            if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
                for inner in _own_nodes(stmt):
                    if isinstance(inner, ast.Call) and (found := _call_status(inner)) is not None:
                        return found
    return None


def _mro(index: code_static.Index, full: str) -> list[tuple[str, str]]:
    found = index.definition(full)
    if found is None or found[1]["kind"] != "class":
        return []
    return impact._Classes(index).mro(found[0], found[1]["name"].replace(".", "::"))


def _lineage(index: code_static.Index, full: str) -> list[str]:
    """``full`` and its bases, nearest first: those the repository defines in method resolution
    order, then the bases they name outside it."""
    places = _mro(index, full)
    if not places:
        return [full]
    module = code_static.module_name
    names = [
        ".".join(filter(None, (module(r, index.src_package), c.replace("::", "."))))
        for r, c in places
    ]
    for rel, cls in places:
        for base in index.bases(rel, cls.replace("::", ".")):
            if base not in names and index.definition(base) is None:
                names.append(base)
    return names


def _same(written: str, loaded: str) -> bool:
    """Whether ``written`` (a name as imported, of a class outside the repository) can be the
    loaded class ``loaded``: equal, the same class name in a module of the same top-level
    package, or, for a bare name (``--exception HTTPException``), the same class name."""
    if written == loaded:
        return True
    w_module, _, w_name = written.rpartition(".")
    l_module, _, l_name = loaded.rpartition(".")
    return w_name == l_name and (not w_module or l_module.startswith(f"{w_module}."))


def _is(index: code_static.Index, name: str, loaded: str) -> bool:
    """Whether ``name`` (an exception as the index or the code names it) is the loaded class
    ``loaded``. A class of the repository is loaded under the index's name, or under a tail of it
    in a nested project (``errors.Conflict`` for ``apps/web/errors.py``)."""
    if not loaded:
        return False
    if index.definition(name) is not None:
        return name == loaded or "." in loaded and name.endswith(f".{loaded}")
    return _same(name, loaded)


def named(index: code_static.Index, text: str) -> str:
    """The exception a user names: a class of the repository, else a built-in one under the
    name a raise of it is read as (``builtins.ValueError``), else as given."""
    found = index.find(text, kind="class")
    if found:
        return found[0]
    if isinstance(getattr(builtins, text, None), type):
        return f"builtins.{text}"
    return text


def indexed(index: code_static.Index, runtime: str, rel: str = "") -> str:
    """The index's name for a function as the running app names it. Its module is named as it
    was imported, which in a nested project is a tail of the name from the repository root
    (``routes`` for ``apps/web/routes.py``): with the file it is in, the longest such tail that
    prefixes the name is replaced by the module's full name."""
    # `create_app.<locals>.version` at run time is `create_app.version` in the index
    name = runtime.replace(".<locals>.", ".")
    if rel:
        module = code_static.module_name(rel, index.src_package)
        parts = module.split(".")
        for start in range(len(parts)):
            tail = ".".join(parts[start:])
            if name.startswith(f"{tail}."):
                return f"{module}{name[len(tail):]}"
    return name


def outcome(
    index: code_static.Index, trees: Trees, handlers: list[dict[str, Any]],
    exception: str, status: int | None,
) -> dict[str, Any]:
    """The handler an app runs for ``exception`` raised with ``status``, and the status it answers
    with: ``status`` (int, or None when the handler's is not a literal) and ``handler``."""
    chosen = None
    if status is not None:
        chosen = next((h for h in handlers if h["status"] == status), None)
    lineage = _lineage(index, exception)
    if chosen is None:
        for name in lineage:
            # The handler of the class itself. A class outside the repository may be imported
            # under a re-export (`fastapi.HTTPException` of `fastapi.exceptions.HTTPException`).
            exact = [h for h in handlers if _is(index, name, h["exception"])]
            if exact:
                chosen = exact[0]
                break
            # Then the handled class nearest above it, by the loaded classes below each.
            below = [h for h in handlers if any(_is(index, name, s) for s in h["subclasses"])]
            if below:
                chosen = min(below, key=lambda h: len(h["subclasses"]))
                break
    if chosen is None:
        chosen = next((h for h in handlers if h["exception"] in _CATCH_ALL), None)
    if chosen is None:
        return {"status": 500, "handler": "", "how": "unhandled"}
    return {
        "status": _handler_status(trees, chosen, status), "handler": chosen["handler"],
        "how": "handler",
    }


def _handler_status(
    trees: Trees, handler: dict[str, Any], raised_status: int | None
) -> int | None:
    known = _FRAMEWORK_HANDLERS.get(handler["handler"])
    if known is not None:
        return raised_status if known == "raised" else known
    if not handler["file"] or handler["file"].startswith("/") or re.match(
        r"[A-Za-z]:/", handler["file"]
    ):
        return None  # outside the repository: its body is not read
    qual = handler["handler"]
    fn = None
    parts = qual.split(".")
    for cut in range(1, len(parts)):
        fn = trees.function(handler["file"], ".".join(parts[cut:]))
        if fn is not None:
            break
    if fn is None:
        return None
    found = {
        s for node in _own_nodes(fn) if isinstance(node, ast.Call)
        if (s := _call_status(node)) is not None
    }
    if len(found) == 1:
        return found.pop()
    passes_on = any(
        isinstance(node, ast.Attribute) and node.attr == "status_code"
        for node in _own_nodes(fn)
    )
    return raised_status if not found and passes_on else None


def reaching(
    index: code_static.Index, trees: Trees, routes: list[dict[str, Any]], full: str,
    depth: int = DEFAULT_DEPTH,
) -> tuple[list[tuple[dict[str, Any], list[str]]], bool]:
    """The routes whose handler reaches ``full`` through calls and dependencies within ``depth``
    levels, each with the chain from the handler down to ``full``; and whether the walk stopped
    at ``MAX_FOLLOWED``."""
    depth = max(1, min(depth, MAX_DEPTH))
    by_handler: dict[str, list[dict[str, Any]]] = {}
    by_dependency: dict[str, list[dict[str, Any]]] = {}
    for route in routes:
        handler = indexed(index, route["handler"], route.get("file") or "")
        by_handler.setdefault(handler, []).append(route)
        # a callable instance is named by its class; its body is `__call__`
        by_handler.setdefault(f"{handler}.__call__", []).append(route)
        for dep in route["dependencies"]:
            by_dependency.setdefault(indexed(index, dep), []).append(route)

    def served(name: str) -> list[dict[str, Any]]:
        """The routes ``name`` is the handler or a dependency of. A dependency comes without its
        file, so one named by a tail of ``name`` that is no definition of its own is ``name``."""
        out = [*by_handler.get(name, []), *by_dependency.get(name, [])]
        parts = name.split(".")
        for start in range(1, len(parts) - 1):
            tail = ".".join(parts[start:])
            if (tail in by_handler or tail in by_dependency) and index.definition(tail) is None:
                out.extend([*by_handler.get(tail, []), *by_dependency.get(tail, [])])
        return out

    # name -> the name it calls on the way down to `full`
    down: dict[str, str | None] = {full: None}
    level, cut = [full], False
    found: dict[tuple[str, str, str], tuple[dict[str, Any], list[str]]] = {}

    def chain(name: str) -> list[str]:
        out = [name]
        while (nxt := down[out[-1]]) is not None:
            out.append(nxt)
        return out

    for step in range(depth + 1):
        for name in level:
            for route in served(name):
                key = (route["app"], route["method"], route["path"])
                found.setdefault(key, (route, chain(name)))
        if step == depth:
            break
        upper: list[str] = []
        for name in level:
            refs = code_static.references(index, [name])
            callers = [impact._caller(index, c["file"], c["scope"]) for c in refs["calls"]]
            for use in refs["uses"]:
                if use["within"].rpartition(".")[2] in _DEPENDS:
                    owner = trees.signature_owner(use["file"], use["line"])
                    module = code_static.module_name(use["file"], index.src_package)
                    callers.append(".".join(filter(None, (module, owner))) if owner else "")
            for caller in callers:
                if caller and caller not in down:
                    if len(down) >= MAX_FOLLOWED:
                        cut = True
                        break
                    down[caller] = name
                    upper.append(caller)
        level = upper
        if not level:
            break
    ordered = sorted(found.values(), key=lambda f: (f[0]["path"], f[0]["method"], f[0]["app"]))
    return ordered, cut
