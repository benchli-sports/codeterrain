"""Static code index: definitions, imports, calls and patch targets of every Python file.

Every ``.py`` file under the root, application code and tests alike, is read with ``ast`` (the
target is never imported) and cached per file by content hash. ``terrain def`` finds where a name
is defined; ``terrain refs`` finds its call sites with their shape (positional and keyword
arguments), checks that shape against the signature, and lists the places that patch it.

Names are resolved the way a reader would: through the file's imports, its own module-level
definitions, ``self``/``cls`` inside a class, and re-exports (``from .orders import create`` in a
package ``__init__``). There is no type inference: a call on an object of unknown type
(``repo.save()``) is a by-name candidate, never an exact reference.
"""

from __future__ import annotations

import ast
import functools
import json
import os
import re
import sqlite3
import stat
from collections.abc import Callable, Iterable, Iterator
from itertools import islice
from pathlib import Path, PurePosixPath
from typing import Any

from codeterrain.cache import (
    ON_WINDOWS,
    Cache,
    _contains,
    _create_private,
    _private_dir,
    user_cache_dir,
    utc_now,
)
from codeterrain.config import Config
from codeterrain.fixtures_static import is_test_module

VIEW = "code"
#: The facts format: bumped whenever the facts read from a file change shape or meaning.
OBJECT_KIND = "code-23"
#: A file larger than this is reported, not parsed: ``ast`` on a huge generated module costs
#: memory out of proportion to what it tells.
MAX_FILE_BYTES = 2_000_000
_TOO_LARGE = f"file larger than {MAX_FILE_BYTES} bytes; not indexed"
_UNDECODABLE = "path is not valid UTF-8; not indexed"
#: Longest error text kept for a file: a parse error can quote the source (a long ``# coding:``
#: name), and every symlink alias of the file stores its own row.
MAX_ERROR_TEXT = 500
#: The parse is bounded by work, not only by bytes: a file of more tokens than this (ordinary code
#: runs to about one every four bytes, so a 2 MB module fits), or one that yields more facts of
#: any kind (definitions, imports, calls, values, patches) than ``MAX_FILE_FACTS``, is reported,
#: not indexed.
MAX_FILE_TOKENS = 600_000
MAX_FILE_FACTS = 50_000
#: How many bytes of strings a file's facts repeat at most: a fact carries its scope, class and
#: qualified name, which the source writes once, so a long function name over many calls would
#: otherwise keep its length once a call.
MAX_FILE_FACT_BYTES = 10_000_000
_TOKEN = re.compile(rb"\w+|[^\w\s]")


class _TooDense(Exception):
    pass
#: How many re-export hops ``resolve`` follows before giving up.
MAX_HOPS = 10
#: How many names ``aliases`` collects for one query: renamed imports of a name number a few in
#: real code, so past this the search stops (and ``refs`` says its lists may be cut).
MAX_ALIASES = 1_000
#: How many values go into one ``IN (...)`` of a query: SQLite allows a few thousand parameters.
_BATCH = 500
#: How many definitions a bare or qualified name lists; a name shared by more is cut, with a
#: count, so a checkout with thousands of same-named methods cannot flood the output or memory.
MAX_MATCHES = 100
#: How many bytes of stored definitions those matches add up to at most: a few broad signatures
#: cost as much as thousands of plain ones, so the list is cut by size as well as by count.
MAX_MATCH_BYTES = 5_000_000
#: How many import rows ``importers`` and ``imports`` read at most.
MAX_IMPORTS = 10_000
#: The comprehensions that run where they are written (a generator expression runs later).
_EAGER = ("<listcomp>", "<setcomp>", "<dictcomp>")

#: ``made``: for a variable, the class it is an instance of as written (``settings: Settings =
#: ...`` or ``settings = Settings()``), else "": a constructor call or annotation, not resolved yet.
#: ``gen``: for a class, whether a decorator generates its ``__init__`` (``@dataclass``).
DEF_TYPES = {
    "name": str, "kind": str, "line": "int", "col": "int", "end": "int", "endcol": "int",
    "sig": str, "doc": str,
    "pos": "strs", "posonly": "int", "kwonly": "strs", "kwreq": "strs", "defaults": "int",
    "star": bool, "dstar": bool, "made": str, "gen": bool,
}
#: ``scope`` is "" for a module-level import; ``deep`` says whether nested functions see it (an
#: import in a function body does, one in a class body does not).
IMPORT_TYPES = {
    "line": "int", "level": "int", "module": str, "name": str, "alias": str, "scope": str,
    "deep": bool,
}
CALL_TYPES = {
    "line": "int", "col": "int", "callee": str, "attr": str, "recv": str, "pos": "int",
    "kw": "strs",
    "star": bool, "dstar": bool, "scope": str, "cls": str, "local": bool, "glob": bool,
}
#: ``local``: an object target's receiver is a parameter or local, not the file's binding.
#: ``glob``: the receiver is declared ``global``, so it means the module's binding.
PATCH_TYPES = {
    "line": "int", "target": str, "how": str, "scope": str, "local": bool, "glob": bool,
}
#: A base of a class (``class TestChild(Base)``), as written: ``cls`` is the class's qualified name.
BASE_TYPES = {"line": "int", "cls": str, "base": str}
#: A read or write of an environment variable named by a string literal: ``os.getenv("X")``,
#: ``os.environ["X"]``, ``os.environ.get("X")``, ``"X" in os.environ``. ``head`` is the name as
#: written (``os.environ``, ``getenv``), resolved to ``os`` through the file's imports when asked;
#: ``how`` is "get", "read", "set", "delete", "setdefault", "pop" or "in".
ENV_TYPES = {"line": "int", "name": str, "head": str, "how": str, "scope": str}
#: The ``os`` names an environment read goes through, once resolved.
ENV_HEADS = frozenset({"os.environ", "os.getenv", "os.environb", "os.getenvb"})
#: A name used as a value, not called there. ``how`` says where: "arg" (passed to the call
#: ``within``: ``Depends(get_settings)``, a callback), "assign" (assigned as it is to ``within``),
#: "augment" (combined into ``within``: ``TOTAL += VALUE``), "member" (an element of a ``within``
#: literal: set, list, tuple or dict), "default" (a default of the function ``within``),
#: "return", "compare" or "read" (anywhere else: an ``if`` test, an operand, an f-string).
USE_TYPES = {
    "line": "int", "col": "int", "name": str, "attr": str, "how": str, "within": str,
    "scope": str, "cls": str, "glob": bool,
}


# --- reading one file --------------------------------------------------------------------------


def _dotted(node: ast.AST) -> str:
    """``a.b.c`` for a chain of names and attributes, else ""."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return ""
    parts.append(node.id)
    return ".".join(reversed(parts))


def _signature(args: ast.arguments) -> dict[str, Any]:
    pos = [a.arg for a in (*args.posonlyargs, *args.args)]
    kwonly = [a.arg for a in args.kwonlyargs]
    kwreq = [a.arg for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True) if d is None]
    defaults = len(args.defaults)
    shown = [
        name if i < len(pos) - defaults else f"{name}=…" for i, name in enumerate(pos)
    ]
    if args.posonlyargs:
        shown.insert(len(args.posonlyargs), "/")
    if args.vararg is not None:
        shown.append(f"*{args.vararg.arg}")
    elif kwonly:
        shown.append("*")
    shown.extend(
        a.arg if d is None else f"{a.arg}=…"
        for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True)
    )
    if args.kwarg is not None:
        shown.append(f"**{args.kwarg.arg}")
    return {
        "sig": f"({', '.join(shown)})", "pos": pos, "posonly": len(args.posonlyargs),
        "kwonly": kwonly, "kwreq": kwreq, "defaults": defaults, "star": args.vararg is not None,
        "dstar": args.kwarg is not None,
    }


_NO_SIGNATURE = {
    "sig": "", "pos": [], "posonly": 0, "kwonly": [], "kwreq": [], "defaults": 0, "star": False,
    "dstar": False,
}


def _doc(node: ast.AST) -> str:
    try:
        text = ast.get_docstring(node) or ""  # type: ignore[arg-type]
    except TypeError:
        return ""
    first = text.strip().splitlines()[0] if text.strip() else ""
    return first[:120]


def _generates_init(decorators: list[ast.expr], aliases: set[str]) -> bool:
    """Whether a class decorated so gets a generated ``__init__``: ``@dataclass`` (any module's,
    or a name in ``aliases`` it was imported as), unless it says ``init=False``."""
    for d in decorators:
        func = d.func if isinstance(d, ast.Call) else d
        dotted = _dotted(func)
        if dotted.rsplit(".", 1)[-1] != "dataclass" and dotted not in aliases:
            continue
        init = next((k.value for k in d.keywords if k.arg == "init"), None) if isinstance(
            d, ast.Call
        ) else None
        return not (isinstance(init, ast.Constant) and init.value is False)
    return False


def _method_kind(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    names = {_dotted(d).rsplit(".", 1)[-1] for d in fn.decorator_list}
    if "staticmethod" in names:
        return "staticmethod"
    if "classmethod" in names:
        return "classmethod"
    if "property" in names or "cached_property" in names:
        return "property"
    return "method"


def _argument(call: ast.Call, index: int, keyword: str) -> ast.expr | None:
    """The argument passed at position ``index`` or by the name ``keyword``."""
    positional = [a for a in call.args[: index + 1] if not isinstance(a, ast.Starred)]
    if len(positional) == len(call.args[: index + 1]) and len(positional) > index:
        return positional[index]
    return next((k.value for k in call.keywords if k.arg == keyword), None)


def _string(node: ast.expr | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _patch_target(call: ast.Call, callee: str) -> tuple[str, str] | None:
    """(target, how) for ``patch("a.b")``, ``patch.object(X, "attr")`` and
    ``monkeypatch.setattr(...)``, their arguments passed by position or by keyword, else None.
    The reader passes a renamed import of ``patch`` here under its own name."""
    tail = callee.rsplit(".", 1)[-1]
    head = callee.rsplit(".", 1)[0] if "." in callee else ""
    target = _argument(call, 0, "target")
    if tail == "patch" and (named := _string(target)):
        return named, "patch"
    if tail == "object" and head.rsplit(".", 1)[-1] == "patch":
        owner = _dotted(target) if target is not None else ""
        attr = _string(_argument(call, 1, "attribute"))
        if owner and attr:
            return f"{owner}.{attr}", "patch.object"
    if callee.endswith("monkeypatch.setattr"):
        named = _string(target)
        if named and "." in named:
            return named, "monkeypatch.setattr"
        attr = _string(_argument(call, 1, "name"))
        owner = _dotted(target) if target is not None and named is None else ""
        if owner and attr:
            return f"{owner}.{attr}", "monkeypatch.setattr(obj)"
    return None


#: Where ``patch`` is imported from: a renamed import of it is still a patch.
_PATCH_MODULES = ("unittest.mock", "mock")


_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
_COMPREHENSION_NAMES = {
    ast.ListComp: "<listcomp>", ast.SetComp: "<setcomp>", ast.DictComp: "<dictcomp>",
    ast.GeneratorExp: "<genexpr>",
}


def _walrus_targets(node: ast.AST) -> set[str]:
    """``(x := ...)`` inside a comprehension binds ``x`` in the enclosing function."""
    found: set[str] = set()
    stack = [node]
    while stack:
        sub = stack.pop()
        if isinstance(sub, ast.NamedExpr) and isinstance(sub.target, ast.Name):
            found.add(sub.target.id)
        if not isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            stack.extend(ast.iter_child_nodes(sub))
    return found


def _bound_names(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
) -> tuple[set[str], set[str], set[str]]:
    """The names bound in the function's own scope: (parameters and assignments, imports, names
    declared ``global``, which mean the module's binding whatever encloses the function). A
    nested function, lambda, class or comprehension keeps its bindings to itself, and each node
    is visited once here, so nesting costs no repeated walks; a name declared ``global`` or
    ``nonlocal`` is not local."""
    args = node.args
    names = {
        a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
        if a is not None
    }
    declared: set[str] = set()
    imported: set[str] = set()
    globals_: set[str] = set()
    # What each imported name was imported from: a second, different source makes it ambiguous.
    sources: dict[str, tuple[int, str, str]] = {}
    stack: list[ast.AST] = list(node.body) if isinstance(node.body, list) else [node.body]
    while stack:
        sub = stack.pop()
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            # Only the name, the decorators and the defaults belong to this scope.
            if not isinstance(sub, ast.Lambda):
                names.add(sub.name)
                stack.extend(sub.decorator_list)
            stack.extend([*sub.args.defaults, *(d for d in sub.args.kw_defaults if d)])
            continue
        if isinstance(sub, ast.ClassDef):
            names.add(sub.name)
            stack.extend([*sub.decorator_list, *sub.bases, *(k.value for k in sub.keywords)])
            continue
        if isinstance(sub, _COMPREHENSIONS):
            stack.append(sub.generators[0].iter)  # the one part evaluated in this scope
            names |= _walrus_targets(sub)
            continue
        if isinstance(sub, (ast.Import, ast.ImportFrom)):
            for a in sub.names:
                if a.name == "*":
                    continue
                if isinstance(sub, ast.Import):
                    local = a.asname or a.name.split(".", 1)[0]
                    source = (0, a.name if a.asname else local, "")
                else:
                    local = a.asname or a.name
                    source = (sub.level, sub.module or "", a.name)
                imported.add(local)
                if sources.setdefault(local, source) != source:
                    names.add(local)
            continue
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
            names.add(sub.id)
        elif isinstance(sub, ast.ExceptHandler) and sub.name:
            names.add(sub.name)
        elif isinstance(sub, (ast.MatchAs, ast.MatchStar)) and sub.name:
            names.add(sub.name)  # `case {"callback": create}:` captures `create`
        elif isinstance(sub, ast.MatchMapping) and sub.rest:
            names.add(sub.rest)
        elif isinstance(sub, (ast.Global, ast.Nonlocal)):
            declared.update(sub.names)
            if isinstance(sub, ast.Global):
                globals_.update(sub.names)
        stack.extend(ast.iter_child_nodes(sub))
    # A name the function both imports and assigns (`from lib import create`, then
    # `create = callback`), or imports from two places, means whichever ran last, which a static
    # reader cannot tell: it is left a local, unresolved, rather than claimed for an import.
    return names - declared, imported - declared - names, globals_


class _Reader(ast.NodeVisitor):
    def __init__(self) -> None:
        self.defs: list[dict[str, Any]] = []
        self.imports: list[dict[str, Any]] = []
        # The names `dataclass` was imported as (`from dataclasses import dataclass as dc`).
        self._dataclass_names: set[str] = set()
        self.calls: list[dict[str, Any]] = []
        self.patches: list[dict[str, Any]] = []
        self.uses: list[dict[str, Any]] = []
        self.bases: list[dict[str, Any]] = []
        self.envs: list[dict[str, Any]] = []
        #: The names the file's imports bind to ``os`` or to its environment names, anywhere.
        self._env_names: set[str] = set()
        self._spent = 0
        self._bytes = 0
        self._scope: list[str] = []
        self._classes: list[str] = []
        self._in_function = 0
        # Names bound inside each enclosing function: passing one is not a reference to a def.
        # Each entry is (names bound there, names imported there, names declared global there);
        # the innermost scope that has the name decides.
        self._locals: list[tuple[set[str], set[str], set[str]]] = []
        # Names `patch` is imported under (`from unittest.mock import patch as mock_patch`),
        # and what each (scope, name) is imported as there: True for `patch`, False for anything
        # else or for both, with whether the scope is a class body.
        self._patchers: set[str] = set()
        self._imported: dict[tuple[str, str], tuple[bool, bool]] = {}
        # The name and attribute nodes already kept (as a call, an argument or a use of a known
        # kind): the walk keeps every other read as a plain "read" use.
        self._seen: set[int] = set()

    def _qualname(self, name: str) -> str:
        return ".".join([*self._scope, name])

    def _spend(self, facts: int, size: int = 0) -> None:
        """Count ``facts`` about to be kept, carrying ``size`` bytes of repeated strings (scope,
        class, qualified name), against the file's budget, before they are kept: every kind
        counts, so no statement shape (a chain of assignments, a call of many arguments, a long
        import list, a long name over many calls) can keep more than ``MAX_FILE_FACTS`` facts or
        ``MAX_FILE_FACT_BYTES`` bytes."""
        self._spent += facts
        self._bytes += size
        if self._spent > MAX_FILE_FACTS:
            raise _TooDense(f"more than {MAX_FILE_FACTS} facts; not indexed")
        if self._bytes > MAX_FILE_FACT_BYTES:
            raise _TooDense(f"more than {MAX_FILE_FACT_BYTES} bytes of facts; not indexed")

    def _add_def(
        self, node: ast.AST, name: str, kind: str, signature: dict[str, Any], made: str = "",
        gen: bool = False, until: ast.AST | None = None,
    ) -> None:
        # ``until``: the node whose end is the moment the name is bound, when that is not the
        # end of ``node`` (`for T in ITER` binds T once ITER is evaluated). A read before that
        # position, on the same line too, sees the earlier binding; one after it, this one.
        last = until or node
        self._spend(1, len(self._qualname(name)) + len(made))
        self.defs.append(
            {
                "name": self._qualname(name), "kind": kind, "line": node.lineno,  # type: ignore[attr-defined]
                "col": getattr(node, "col_offset", None) or 0,
                "end": getattr(last, "end_lineno", None) or node.lineno,  # type: ignore[attr-defined]
                "endcol": getattr(last, "end_col_offset", None) or 0,
                "doc": _doc(node) if kind != "variable" else "", **signature, "made": made,
                "gen": gen,
            }
        )

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for d in node.decorator_list:
            self.visit(d)
        for base in [*node.bases, *(k.value for k in node.keywords)]:
            self.visit(base)
        if not self._in_function:
            self._add_def(
                node, node.name, "class", _NO_SIGNATURE,
                gen=_generates_init(node.decorator_list, self._dataclass_names),
            )
            qualname = self._qualname(node.name)
            for base in node.bases:
                if dotted := _dotted(base):
                    self._spend(1, len(dotted))
                    self.bases.append({"line": node.lineno, "cls": qualname, "base": dotted})
        self._scope.append(node.name)
        self._classes.append(".".join(self._scope))
        for stmt in node.body:
            self.visit(stmt)
        self._classes.pop()
        self._scope.pop()

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for d in node.decorator_list:
            self.visit(d)
        for default in [*node.args.defaults, *(d for d in node.args.kw_defaults if d)]:
            self._value(default, "default", node.name)
            self.visit(default)
        # `user: Annotated[str, Depends(get_user)]` names a dependency in an annotation.
        args = node.args
        for a in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
            if a is not None and a.annotation is not None:
                self.visit(a.annotation)
        if node.returns is not None:
            self.visit(node.returns)
        if not self._in_function:
            in_class = bool(self._classes) and self._scope and ".".join(self._scope) == (
                self._classes[-1]
            )
            kind = _method_kind(node) if in_class else "function"
            # Every parameter is kept (in the signature and its lists): counted before it is.
            args = node.args
            self._spend(
                len(args.posonlyargs) + len(args.args) + len(args.kwonlyargs)
                + (args.vararg is not None) + (args.kwarg is not None)
            )
            self._add_def(node, node.name, kind, _signature(args))
        self._scope.append(node.name)
        self._in_function += 1
        self._locals.append(_bound_names(node))
        for stmt in node.body:
            self.visit(stmt)
        self._locals.pop()
        self._in_function -= 1
        self._scope.pop()

    visit_FunctionDef = _function
    visit_AsyncFunctionDef = _function

    def _assigned(
        self, node: ast.AST, targets: list[ast.expr], made: str = "",
        until: ast.AST | None = None,
    ) -> None:
        if self._in_function:
            return
        # `a, (b, *c) = ...` binds every name in the target, as `a = ...` binds one. A stack,
        # pushed in reverse, keeps source order and costs one step a name. Only a name assigned
        # directly (`a = b = Settings()`, not `a, b = ...`) is an instance of ``made``.
        pending = targets[::-1]
        while pending:
            target = pending.pop()
            if isinstance(target, ast.Name):
                self._add_def(
                    node, target.id, "variable", _NO_SIGNATURE,
                    made if any(t is target for t in targets) else "", until=until,
                )
            elif isinstance(target, (ast.Tuple, ast.List)):
                pending.extend(reversed(target.elts))
            elif isinstance(target, ast.Starred):
                pending.append(target.value)

    def visit_Assign(self, node: ast.Assign) -> None:
        made = _dotted(node.value.func) if isinstance(node.value, ast.Call) else ""
        self._assigned(node, node.targets, made)
        # `A = B = VALUE` assigns the value to each target: one use for each.
        for target in node.targets:
            self._value(node.value, "assign", _dotted(target))
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        # `TOTAL += VALUE` reads the value into a computed one: not another name for it. It
        # also rebinds TOTAL, so an earlier `TOTAL = VALUE` or `TOTAL = Settings()` is no longer
        # what TOTAL holds.
        self._assigned(node, [node.target])
        self._value(node.value, "augment", _dotted(node.target))
        self.generic_visit(node)

    def visit_For(self, node: ast.For | ast.AsyncFor) -> None:
        # The target binds before the body runs, once the iterable is evaluated: a read in the
        # iterable (`for A in [A]`) sees the binding from before.
        self._assigned(node.target, [node.target], until=node.iter)
        self.generic_visit(node)

    visit_AsyncFor = visit_For

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        # `(create := ...)` outside a function binds in the module or class body, from the
        # target's line: the value runs first. In a comprehension it binds in the scope around
        # it (Python's rule), so the comprehension segments are left out of its name.
        self.visit(node.value)
        if not self._in_function:
            scope = self._scope
            while self._scope and self._scope[-1] in _COMPREHENSION_NAMES.values():
                self._scope = self._scope[:-1]
            self._assigned(node.target, [node.target], until=node.value)
            self._scope = scope

    def visit_With(self, node: ast.With | ast.AsyncWith) -> None:
        for item in node.items:
            if item.optional_vars is not None:
                self._assigned(item.optional_vars, [item.optional_vars])
        self.generic_visit(node)

    visit_AsyncWith = visit_With

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            # `settings: Settings = get_settings()`: the annotation names the class; without
            # one that is a plain name, the constructor call does.
            made = _dotted(node.annotation) or (
                _dotted(node.value.func) if isinstance(node.value, ast.Call) else ""
            )
            self._assigned(node, [node.target], made)
            self._value(node.value, "assign", _dotted(node.target))
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> None:
        self._value(node.value, "return", "")
        self.generic_visit(node)

    def visit_Yield(self, node: ast.Yield | ast.YieldFrom) -> None:
        self._value(node.value, "return", "")
        self.generic_visit(node)

    visit_YieldFrom = visit_Yield

    def visit_Compare(self, node: ast.Compare) -> None:
        # `"X" in os.environ`
        if len(node.ops) == 1 and isinstance(node.ops[0], ast.In | ast.NotIn):
            self._env(node.lineno, node.left, _dotted(node.comparators[0]), "in")
        for operand in [node.left, *node.comparators]:
            self._value(operand, "compare", "")
        self.generic_visit(node)

    def _members(self, node: ast.Set | ast.List | ast.Tuple | ast.Dict, kind: str) -> None:
        if not isinstance(node, ast.Set | ast.Dict) and not isinstance(node.ctx, ast.Load):
            return self.generic_visit(node)  # an assignment target, not a value
        elements = (
            [*(k for k in node.keys if k is not None), *node.values]
            if isinstance(node, ast.Dict) else node.elts
        )
        for element in elements:
            self._value(
                element.value if isinstance(element, ast.Starred) else element, "member", kind
            )
        self.generic_visit(node)

    def visit_Set(self, node: ast.Set) -> None:
        self._members(node, "set")

    def visit_List(self, node: ast.List) -> None:
        self._members(node, "list")

    def visit_Tuple(self, node: ast.Tuple) -> None:
        self._members(node, "tuple")

    def visit_Dict(self, node: ast.Dict) -> None:
        self._members(node, "dict")

    def visit_Subscript(self, node: ast.Subscript) -> None:
        # `os.environ["X"]`, read, set or deleted
        how = {ast.Store: "set", ast.Del: "delete"}.get(type(node.ctx), "read")
        self._env(node.lineno, node.slice, _dotted(node.value), how)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load) and id(node) not in self._seen:
            self._value(node, "read", "")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        # `settings.TABLE` read anywhere not already kept with a kind (an `if` test, an operand,
        # a loop's iterable): one use of the whole chain, not of each part.
        if not _dotted(node):
            self.generic_visit(node)  # `make().TABLE`: the call inside is still read
        elif isinstance(node.ctx, ast.Load) and id(node) not in self._seen:
            self._value(node, "read", "")

    def _value(self, node: ast.expr | None, how: str, within: str) -> None:
        """Keep ``node`` as a use of the name it reads, when it is a name or attribute chain
        (``settings.TABLE``) that no enclosing function binds: a parameter or local is not a
        reference to a definition. The node is marked, so the walk does not keep it again as a
        plain read."""
        name = _dotted(node) if isinstance(node, (ast.Name, ast.Attribute)) else ""
        if not name:
            return
        self._seen.add(id(node))
        if self._binder(name) == "local":
            return
        scope = ".".join(self._scope)
        cls = self._classes[-1] if self._classes else ""
        self._spend(1, len(scope) + len(cls) + len(name) + len(within))
        self.uses.append(
            {
                "line": node.lineno, "col": node.col_offset,  # type: ignore[attr-defined]
                "name": name, "attr": name.rsplit(".", 1)[-1],  # type: ignore[union-attr]
                "how": how, "within": within, "scope": scope, "cls": cls,
                "glob": self._binder(name) == "global",
            }
        )

    def _import_scope(self) -> dict[str, Any]:
        scope = ".".join(self._scope)
        in_class_body = bool(self._classes) and scope == self._classes[-1]
        return {"scope": scope, "deep": not in_class_body}

    def _record_import(self, local: str, is_patch: bool) -> None:
        scope = ".".join(self._scope)
        key = (scope, local)
        earlier = self._imported.get(key)
        in_class_body = bool(self._classes) and scope == self._classes[-1]
        self._imported[key] = (is_patch and (earlier is None or earlier[0]), in_class_body)
        if is_patch:
            self._patchers.add(local)

    def _patch_import(self, name: str) -> bool | None:
        """Whether ``name`` here means an import of ``patch``, under its own name or another:
        the innermost scope that binds it decides, a parameter or local is not, and a class
        body's import counts only in that body; a scope that imports the name both as ``patch``
        and as something else leaves it unknown, so not a patch. None when no import or local
        binds the name (a star import, say): then only its spelling is known."""
        binder = self._binder(name)
        if binder == "local":
            return False
        if binder == "global":
            found = self._imported.get(("", name))
            return None if found is None else found[0]
        for i in range(len(self._scope), -1, -1):
            found = self._imported.get((".".join(self._scope[:i]), name))
            if found is not None and (i == len(self._scope) or not found[1]):
                return found[0]
        return None

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            # `import a.b` binds `a`; `import a.b as x` binds `x` to `a.b`.
            local = alias.asname or alias.name.split(".", 1)[0]
            module = alias.name if alias.asname else alias.name.split(".", 1)[0]
            self._record_import(local, False)
            self._spend(1, len(".".join(self._scope)) + len(module))
            self.imports.append(
                {
                    "line": node.lineno, "level": 0, "module": module, "name": "",
                    "alias": local, **self._import_scope(),
                }
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            # `from m import *` binds names known only at run time, but the dependency on `m` is
            # plain: it is kept as an import of `m.*` bound to `*`, which no name ever looks up.
            if alias.name != "*":
                self._record_import(
                    alias.asname or alias.name,
                    not node.level and node.module in _PATCH_MODULES and alias.name == "patch",
                )
            self._spend(1, len(".".join(self._scope)) + len(node.module or ""))
            if alias.name == "dataclass":
                self._dataclass_names.add(alias.asname or alias.name)
            self.imports.append(
                {
                    "line": node.lineno, "level": node.level, "module": node.module or "",
                    "name": alias.name, "alias": alias.asname or alias.name,
                    **self._import_scope(),
                }
            )

    def visit_Call(self, node: ast.Call) -> None:
        callee = _dotted(node.func)
        attr = node.func.attr if isinstance(node.func, ast.Attribute) else (
            node.func.id if isinstance(node.func, ast.Name) else ""
        )
        if node.args:
            # `os.getenv("X")`, `os.environ.get("X")` and the like
            if attr in ("getenv", "getenvb"):
                self._env(node.lineno, node.args[0], callee, "get")
            elif attr in ("get", "setdefault", "pop") and isinstance(node.func, ast.Attribute):
                self._env(node.lineno, node.args[0], callee.rpartition(".")[0], attr)
        scope = ".".join(self._scope)
        # The call and each of its arguments (a value use, a keyword name) count before any is
        # kept, so one call of a hundred thousand arguments is refused as a dense file is; each
        # repeats the scope and class, and a value use the callee too.
        args = len(node.args) + len(node.keywords)
        context = len(scope) + len(self._classes[-1] if self._classes else "")
        self._spend(1 + args, (1 + args) * context + args * len(callee))
        # `Service(repo).place(...)`: the receiver is a call, often a constructor.
        recv = (
            _dotted(node.func.value.func)
            if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Call)
            else ""
        )
        # `def invoke(create): create(1)`: a name bound in the function hides the import.
        binder = self._binder(callee or recv) if callee or recv else ""
        self.calls.append(
            {
                "line": node.lineno, "col": node.col_offset, "callee": callee, "attr": attr,
                "recv": recv,
                "pos": sum(1 for a in node.args if not isinstance(a, ast.Starred)),
                "kw": [k.arg for k in node.keywords if k.arg is not None],
                "star": any(isinstance(a, ast.Starred) for a in node.args),
                "dstar": any(k.arg is None for k in node.keywords),
                "scope": scope, "cls": self._classes[-1] if self._classes else "",
                "local": binder == "local", "glob": binder == "global",
            }
        )
        # The callee is kept as the call, not also as a read; a receiver that is not a plain chain
        # (`make().save()`) is still walked.
        if callee:
            self._seen.add(id(node.func))
        for value in [*node.args, *(k.value for k in node.keywords)]:
            name = _dotted(value) if isinstance(value, (ast.Name, ast.Attribute)) else ""
            if name:
                self._seen.add(id(value))
            binder = self._binder(name) if name else "local"
            if binder != "local":
                self.uses.append(
                    {
                        "line": value.lineno, "col": value.col_offset, "name": name,
                        "attr": name.rsplit(".", 1)[-1],
                        "how": "arg", "within": callee, "scope": scope,
                        "cls": self._classes[-1] if self._classes else "",
                        "glob": binder == "global",
                    }
                )
        first, _, rest = callee.partition(".")
        # `patch` itself and its renamed imports count where that import binds the name, so a
        # parameter, a local or another import of the name is not a patch.
        known = self._patch_import(first) if first == "patch" or first in self._patchers else None
        if known:
            patched = _patch_target(node, f"patch.{rest}" if rest else "patch")
        else:
            patched = None if known is False else _patch_target(node, callee)
        if patched is not None:
            receiver = self._binder(patched[0]) if patched[1] not in (
                "patch", "monkeypatch.setattr"
            ) else ""
            self._spend(1, len(scope))
            self.patches.append(
                {
                    "line": node.lineno, "target": patched[0], "how": patched[1], "scope": scope,
                    "local": receiver == "local", "glob": receiver == "global",
                }
            )
        self.generic_visit(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        # The defaults run where the lambda is made, outside its parameters' scope.
        for default in [*node.args.defaults, *(d for d in node.args.kw_defaults if d)]:
            self._value(default, "default", "<lambda>")
            self.visit(default)
        # The body runs in the lambda's own scope, which skips a class body's names.
        self._in_function += 1
        self._scope.append("<lambda>")
        self._locals.append(_bound_names(node))
        self._value(node.body, "return", "")
        self.visit(node.body)
        self._locals.pop()
        self._scope.pop()
        self._in_function -= 1

    def _comprehension(
        self, node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp
    ) -> None:
        # The first iterable runs in the enclosing scope; the rest runs in the comprehension's
        # own scope (named as Python names it), which skips a class body's names, and its
        # targets bind there.
        first, *rest = node.generators
        self.visit(first.iter)
        self._scope.append(_COMPREHENSION_NAMES[type(node)])
        self._locals.append(
            (
                {
                    n.id
                    for g in node.generators
                    for n in ast.walk(g.target)
                    if isinstance(n, ast.Name)
                },
                set(),
                set(),
            )
        )
        for part in [first.target, *first.ifs]:
            self.visit(part)
        for g in rest:
            self.visit(g)
        for part in (
            [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
        ):
            self.visit(part)
        self._locals.pop()
        self._scope.pop()

    visit_ListComp = visit_SetComp = visit_DictComp = visit_GeneratorExp = _comprehension

    def _binder(self, name: str) -> str:
        """Where ``name`` is bound: "local" in an enclosing function, "global" when declared
        so, "" for the file's or a function's import. ``self.method`` and ``cls.method`` are "",
        a bare ``self`` is local."""
        root, dot, _ = name.partition(".")
        if dot and root in ("self", "cls"):
            return ""
        for names, imported, globals_ in reversed(self._locals):
            if root in globals_:
                return "global"
            if root in imported:
                return ""  # the scope's own import: resolved through the scoped bindings
            if root in names:
                return "local"
        return ""

    def _local(self, name: str) -> bool:
        return self._binder(name) == "local"

    def _env(self, line: int, name: ast.expr | None, head: str, how: str) -> None:
        """Keep an environment variable named by a string literal, through a name as written
        (``os.environ``, ``environ``, ``getenv``); whether it is ``os``'s is resolved later."""
        first, _, rest = head.partition(".")
        if (
            isinstance(name, ast.Constant) and isinstance(name.value, str)
            and first in self._env_names and (not rest or rest in _ENV_ATTRS)
            and not self._local(first)
        ):
            scope = ".".join(self._scope)
            self._spend(1, len(scope) + len(head) + len(name.value))
            self.envs.append(
                {"line": line, "name": name.value, "head": head, "how": how, "scope": scope}
            )


#: The ``os`` attributes an environment read goes through.
_ENV_ATTRS = frozenset({"environ", "environb", "getenv", "getenvb"})


def _env_names(tree: ast.Module) -> set[str]:
    """The names any import of the file binds to ``os`` (``import os as o``) or to one of
    ``_ENV_ATTRS`` (``from os import environ as env``): only those can read the environment."""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            # `import os.path` binds `os` too; `import os.path as p` binds the submodule.
            out.update(
                a.asname or "os" for a in node.names
                if a.name == "os" or a.name.startswith("os.") and not a.asname
            )
        elif isinstance(node, ast.ImportFrom) and node.module == "os" and not node.level:
            out.update(a.asname or a.name for a in node.names if a.name in _ENV_ATTRS)
    return out


def parse_source(source: str | bytes) -> dict[str, Any]:
    """Facts for one file's text, or its bytes: ``ast`` then honours a BOM and a coding line."""
    empty: dict[str, Any] = {
        "defs": [], "imports": [], "calls": [], "patches": [], "uses": [], "bases": [],
        "envs": [],
    }
    if len(source) > MAX_FILE_BYTES:
        return {**empty, "error": _TOO_LARGE}
    # The tree costs memory by tokens, not bytes: a dense file is refused before it is parsed.
    data = source.encode("utf-8", "surrogatepass") if isinstance(source, str) else source
    if sum(1 for _ in _TOKEN.finditer(data)) > MAX_FILE_TOKENS:
        return {**empty, "error": f"more than {MAX_FILE_TOKENS} tokens; not indexed"}
    try:
        tree = ast.parse(source)
        reader = _Reader()
        reader._env_names = _env_names(tree)
        reader.visit(tree)
    except _TooDense as exc:
        return {**empty, "error": str(exc)[:MAX_ERROR_TEXT]}
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        return {**empty, "error": f"{type(exc).__name__}: {exc}"[:MAX_ERROR_TEXT]}
    return {
        "defs": reader.defs, "imports": reader.imports, "calls": reader.calls,
        "patches": reader.patches, "uses": reader.uses, "bases": reader.bases, "envs": reader.envs,
        "error": None,
    }


# --- modules and names -------------------------------------------------------------------------


@functools.lru_cache(maxsize=65_536)
def module_name(rel: str, src_package: bool = False) -> str:
    """The import name of a repository file: ``src/app/x.py`` and ``app/x.py`` are ``app.x``,
    unless ``src`` is itself a package (``src_package``: it has an ``__init__.py``); a package
    ``__init__.py`` is the package."""
    parts = list(PurePosixPath(rel).with_suffix("").parts)
    if parts and parts[0] == "src" and len(parts) > 1 and not src_package:
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


#: The files that make a folder a project of its own, whose code imports itself from there.
_PROJECT_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg")


def project_roots(files: Iterable[str]) -> frozenset[str]:
    """The nested project folders among ``files``: each holds a ``pyproject.toml``,
    ``setup.py`` or ``setup.cfg``, as an app in a monorepo does, and its code imports its
    modules from there (``apps/api`` on the path, ``from src.repositories import factory``)."""
    return frozenset(
        rel.rpartition("/")[0] for rel in files
        if "/" in rel and rel.rpartition("/")[2] in _PROJECT_MARKERS
    )


def root_of(rel: str, roots: frozenset[str]) -> str:
    """The innermost project root holding ``rel``, or "": one lookup per enclosing folder, so
    the cost follows the path's depth, not the number of roots."""
    parts = rel.split("/")[:-1]
    for depth in range(len(parts), 0, -1):
        folder = "/".join(parts[:depth])
        if folder in roots:
            return folder
    return ""


def _absolute(module: str, is_package: bool, level: int, target: str) -> str:
    """The absolute module of ``from <level dots><target> import ...`` written in ``module``."""
    if level == 0:
        return target
    base = module.split(".") if module else []
    if not is_package:
        base = base[:-1]
    if level > 1:
        base = base[: len(base) - (level - 1)] if level - 1 <= len(base) else []
    return ".".join([*base, target] if target else base)


def bindings(
    rel: str, facts: dict[str, Any], src_package: bool = False
) -> dict[tuple[str, bool], list[tuple[str, str, int]]]:
    """(scope, deep) -> every (local name, the full dotted name it stands for, the line from
    which it holds), in source order. Scope "" holds the module-level imports and definitions;
    an import inside a function or class body binds only there. Every binding is kept, not only
    the last: an expression at module or class level means the binding of its own moment
    (``create = create(1)`` calls the import). A definition holds from the line after it ends."""
    module = module_name(rel, src_package)
    is_package = PurePosixPath(rel).name == "__init__.py"
    scopes: dict[tuple[str, bool], list[tuple[str, str, int]]] = {("", True): []}
    bound: list[tuple[int, tuple[str, bool], str, str]] = []
    for imp in facts["imports"]:
        source = _absolute(module, is_package, imp["level"], imp["module"])
        key = (imp["scope"], imp["deep"]) if imp["scope"] else ("", True)
        target = (f"{source}.{imp['name']}" if source else imp["name"]) if imp["name"] else source
        bound.append((imp["line"], key, imp["alias"], target))
    for d in facts["defs"]:
        if "." not in d["name"]:
            target = f"{module}.{d['name']}" if module else d["name"]
            bound.append((d["end"], ("", True), d["name"], target))
    for line, key, alias, target in sorted(bound, key=lambda b: b[0]):
        scopes.setdefault(key, []).append((alias, target, line))
    return scopes


# --- the store ---------------------------------------------------------------------------------
#
# The index is a SQLite database in the cache directory, not a JSON view: a large backend has
# hundreds of thousands of call sites, and a query reads only the rows for the name it asks about.
# A changed file replaces its own rows, and a removed file's rows go with it. Every row is
# checked when it is read: a database that does not hold what this core writes is not trusted.


def _running_scope(scope: str) -> str:
    """The scope whose execution point code in ``scope`` runs at: a list, set or dict
    comprehension runs where it is written, so at module level it means the bindings of that
    moment, though its names live in a scope of their own."""
    while scope.rpartition(".")[2] in _EAGER:
        scope = scope.rpartition(".")[0]
    return scope


def _batches(values: set[str]) -> Iterator[list[str]]:
    ordered = sorted(values)
    for start in range(0, len(ordered), _BATCH):
        yield ordered[start : start + _BATCH]


class CorruptIndex(RuntimeError):
    """The code index holds rows this core did not write; ``terrain refresh code`` rebuilds it."""


class UnsafeIndex(CorruptIndex):
    """The code index's path is a link, or another user can change it or a folder above it."""


_TABLES = (
    "meta", "facts", "files", "defs", "binds", "calls", "patches", "uses", "bases", "envs",
)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY, rel TEXT NOT NULL UNIQUE, sha TEXT NOT NULL, test INTEGER NOT NULL,
    error TEXT);
CREATE TABLE IF NOT EXISTS defs (
    full TEXT NOT NULL, tail TEXT NOT NULL, file INTEGER NOT NULL, json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS defs_full ON defs (full);
CREATE INDEX IF NOT EXISTS defs_tail ON defs (tail);
CREATE INDEX IF NOT EXISTS defs_file ON defs (file);
CREATE TABLE IF NOT EXISTS binds (
    file INTEGER NOT NULL, alias TEXT NOT NULL, name TEXT NOT NULL, target TEXT NOT NULL,
    tail TEXT NOT NULL, scope TEXT NOT NULL, deep INTEGER NOT NULL, line INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS binds_file ON binds (file);
CREATE INDEX IF NOT EXISTS binds_name ON binds (name);
CREATE INDEX IF NOT EXISTS binds_tail ON binds (tail);
CREATE INDEX IF NOT EXISTS binds_alias ON binds (file, alias, scope, line);
CREATE TABLE IF NOT EXISTS calls (file INTEGER NOT NULL, attr TEXT NOT NULL, json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS calls_attr ON calls (attr);
CREATE INDEX IF NOT EXISTS calls_file ON calls (file);
CREATE TABLE IF NOT EXISTS patches (
    file INTEGER NOT NULL, tail TEXT NOT NULL, line INTEGER NOT NULL, json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS patches_tail ON patches (tail);
CREATE INDEX IF NOT EXISTS patches_file ON patches (file, line);
CREATE TABLE IF NOT EXISTS uses (file INTEGER NOT NULL, attr TEXT NOT NULL, json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS uses_attr ON uses (attr);
CREATE INDEX IF NOT EXISTS uses_file ON uses (file);
CREATE TABLE IF NOT EXISTS bases (file INTEGER NOT NULL, cls TEXT NOT NULL, tail TEXT NOT NULL,
    json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS bases_class ON bases (file, cls);
CREATE INDEX IF NOT EXISTS bases_tail ON bases (tail);
CREATE TABLE IF NOT EXISTS envs (file INTEGER NOT NULL, name TEXT NOT NULL, json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS envs_name ON envs (name);
CREATE INDEX IF NOT EXISTS envs_file ON envs (file);
"""


def _span(row: dict[str, Any]) -> tuple[tuple[int, int], tuple[int, int]]:
    """A definition's (line, column) start and the (line, column) at which it binds."""
    return (row["line"], row["col"]), (row["end"], row["endcol"])


def _typed(row: Any, types: dict[str, Any]) -> bool:
    if not isinstance(row, dict) or set(row) != set(types):
        return False
    for key, kind in types.items():
        value = row[key]
        if kind == "int":
            if not isinstance(value, int) or isinstance(value, bool):
                return False
        elif kind == "strs":
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                return False
        elif not isinstance(value, kind):
            return False
    return True


def _valid_facts(facts: Any) -> bool:
    return (
        isinstance(facts, dict)
        and set(facts) == {
            "defs", "imports", "calls", "patches", "uses", "bases", "envs", "error"
        }
        and (facts["error"] is None or isinstance(facts["error"], str))
        and all(_typed(d, DEF_TYPES) for d in facts["defs"])
        and all(_typed(i, IMPORT_TYPES) for i in facts["imports"])
        and all(_typed(c, CALL_TYPES) for c in facts["calls"])
        and all(_typed(p, PATCH_TYPES) for p in facts["patches"])
        and all(_typed(u, USE_TYPES) for u in facts["uses"])
        and all(_typed(b, BASE_TYPES) for b in facts["bases"])
        and all(_typed(e, ENV_TYPES) for e in facts["envs"])
    )


def _row(text: Any, types: dict[str, Any]) -> dict[str, Any]:
    """A stored row back as a dict, checked against ``types``."""
    try:
        values = json.loads(text)
    except (TypeError, ValueError, RecursionError):
        values = None
    value = (
        dict(zip(types, values, strict=True))
        if isinstance(values, list) and len(values) == len(types)
        else None
    )
    if not _typed(value, types):
        raise CorruptIndex("the code index holds a row this version did not write")
    return value  # type: ignore[return-value]


def _pack(row: dict[str, Any], types: dict[str, Any]) -> str:
    """A row as a JSON array in the order of ``types``: the keys are not stored."""
    return json.dumps([row[key] for key in types], separators=(",", ":"))


def db_path(cache: Cache) -> Path:
    # A folder of its own: SQLite creates its journal beside the database, and no one else may.
    return _index_home(cache) / f"code-{cache.root_id}" / "index.sqlite3"


def _index_home(cache: Cache) -> Path:
    """Where the index folder goes. On Windows a cache outside the user's profile (in the
    checkout, on a drive whose ACL lets every user change it) cannot be made private without
    ACL calls, so the index goes in the per-user cache under the profile instead."""
    if ON_WINDOWS and not _contains(
        Path(os.path.realpath(Path.home())), Path(os.path.realpath(cache.dir))
    ):
        return user_cache_dir(cache.root)
    return cache.dir


def _is_link(info: os.stat_result) -> bool:
    reparse = getattr(info, "st_file_attributes", 0) & getattr(
        stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
    )
    return stat.S_ISLNK(info.st_mode) or bool(reparse)


def _refuse_unsafe(path: Path) -> None:
    """Refuse a database SQLite would open through a link, or one another user could swap or
    plant files beside: SQLite follows a link, creates its journal next to the database, and a
    rebuild drops the tables of whatever database it opened. Everywhere, the database's own
    folder and the database are no links and the database has no other name. On POSIX that
    folder belongs to the user and only the user can write it, and every folder above belongs to
    the user (or root) and is writable by no one else unless sticky (``/tmp``), as ssh requires."""
    hint = "; use --cache-dir or CODETERRAIN_CACHE_DIR under your home directory"
    own = os.lstat(path.parent)
    if _is_link(own) or not stat.S_ISDIR(own.st_mode):
        raise UnsafeIndex(f"the code index folder is a link: {path.parent}{hint}")
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        info = None
    if info is not None and (_is_link(info) or not stat.S_ISREG(info.st_mode)):
        raise UnsafeIndex(f"the code index is not a regular file: {path}{hint}")
    if info is not None and info.st_nlink != 1:
        raise UnsafeIndex(f"the code index has another name (a hard link): {path}{hint}")
    if ON_WINDOWS:
        return
    uid = os.getuid()
    if info is not None and info.st_uid != uid:
        raise UnsafeIndex(f"the code index belongs to another user: {path}{hint}")
    if own.st_uid != uid or own.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise UnsafeIndex(f"another user can change the code index folder {path.parent}{hint}")
    folder = Path(os.path.abspath(path.parent.parent))
    for d in (folder, *folder.parents):
        st = os.stat(d)
        others_write = st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        if st.st_uid not in (uid, 0) or (others_write and not st.st_mode & stat.S_ISVTX):
            raise UnsafeIndex(f"another user can change the code index folder {d}{hint}")


def _connect(cache: Cache) -> sqlite3.Connection:
    path = db_path(cache)
    _private_dir(path.parent)
    _refuse_unsafe(path)
    if not path.exists():
        try:
            _create_private(path, b"")  # 0600 on POSIX before SQLite writes anything
        except FileExistsError:
            pass
    # Checked again after the create: a link that won the race to the name is refused. Past this
    # point the file is the user's own, and the folders above it let no one else replace it.
    _refuse_unsafe(path)
    before = os.lstat(path)
    db = sqlite3.connect(path, timeout=30, isolation_level=None)
    after = os.lstat(path)
    if (after.st_ino, after.st_dev, after.st_nlink) != (before.st_ino, before.st_dev, 1):
        db.close()
        raise UnsafeIndex(f"the code index changed while it was opened: {path}")
    db.execute("PRAGMA journal_mode=DELETE")
    return db


def _reset(db: sqlite3.Connection) -> None:
    for table in _TABLES:
        db.execute(f"DROP TABLE IF EXISTS {table}")
    for statement in _SCHEMA.split(";"):
        if statement.strip():
            db.execute(statement)
    db.execute("INSERT INTO meta VALUES ('format', ?)", (OBJECT_KIND,))


def _current(db: sqlite3.Connection) -> bool:
    try:
        row = db.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()
    except sqlite3.DatabaseError:
        return False
    return row is not None and row[0] == OBJECT_KIND


def _meta(db: sqlite3.Connection, key: str) -> str | None:
    try:
        row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    except sqlite3.DatabaseError:
        return None
    return row[0] if row is not None and isinstance(row[0], str) else None


def _roots(value: str | None) -> frozenset[str]:
    try:
        roots = json.loads(value or "[]")
    except ValueError:
        return frozenset()
    if not isinstance(roots, list):
        return frozenset()
    return frozenset(r for r in roots if isinstance(r, str))


def _src_package(db: sqlite3.Connection) -> bool | None:
    value = _meta(db, "src_package")
    return None if value is None else value == "1"


def _escaped(rel: str) -> str:
    """``rel`` itself, or for a path holding bytes that are not UTF-8 (surrogate escapes) the
    same path with those characters written as ``\\udcNN``, which SQLite can store."""
    try:
        rel.encode("utf-8")
    except UnicodeEncodeError:
        return rel.encode("utf-8", "backslashreplace").decode("utf-8")
    return rel


def _facts(cache: Cache, rel: str) -> dict[str, Any]:
    with open(cache.root / rel, "rb") as f:
        data = f.read(MAX_FILE_BYTES + 1)  # never more than the cap, whatever the file's size
    if len(data) > MAX_FILE_BYTES:
        return {**parse_source(""), "error": _TOO_LARGE}
    return parse_source(data)


def _forget(db: sqlite3.Connection, rel: str) -> None:
    row = db.execute("SELECT id FROM files WHERE rel = ?", (rel,)).fetchone()
    if row is None:
        return
    for table in ("defs", "binds", "calls", "patches", "uses", "bases", "envs"):
        db.execute(f"DELETE FROM {table} WHERE file = ?", (row[0],))
    db.execute("DELETE FROM files WHERE id = ?", (row[0],))


def _record(
    db: sqlite3.Connection, rel: str, sha: str, test: bool, facts: dict[str, Any],
    src_package: bool,
) -> None:
    module = module_name(rel, src_package)
    bound = bindings(rel, facts, src_package)
    # The facts are kept per content, so the parse could not charge the module name each
    # definition and binding is stored under: a long path multiplies the stored size.
    prefixed = len(facts["defs"]) + sum(len(b) for b in bound.values())
    if module and (len(module) + 1) * prefixed > MAX_FILE_FACT_BYTES:
        db.execute(
            "INSERT INTO files (rel, sha, test, error) VALUES (?, ?, ?, ?)",
            (rel, sha, int(test),
             f"more than {MAX_FILE_FACT_BYTES} bytes of names under its module path; not indexed"),
        )
        return
    fid = db.execute(
        "INSERT INTO files (rel, sha, test, error) VALUES (?, ?, ?, ?)",
        (rel, sha, int(test), facts["error"]),
    ).lastrowid
    db.executemany(
        "INSERT INTO defs VALUES (?, ?, ?, ?)",
        [
            (f"{module}.{d['name']}" if module else d["name"], d["name"].rsplit(".", 1)[-1], fid,
             _pack(d, DEF_TYPES))
            for d in facts["defs"]
        ],
    )
    db.executemany(
        "INSERT INTO binds VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (fid, alias, f"{module}.{alias}" if module else alias, target,
             target.rsplit(".", 1)[-1], scope, int(deep), line)
            for (scope, deep), rows in bound.items()
            for alias, target, line in rows
        ],
    )
    db.executemany(
        "INSERT INTO calls VALUES (?, ?, ?)",
        [(fid, c["attr"], _pack(c, CALL_TYPES)) for c in facts["calls"]],
    )
    db.executemany(
        "INSERT INTO patches VALUES (?, ?, ?, ?)",
        [
            (fid, p["target"].rsplit(".", 1)[-1], p["line"], _pack(p, PATCH_TYPES))
            for p in facts["patches"]
        ],
    )
    db.executemany(
        "INSERT INTO uses VALUES (?, ?, ?)",
        [(fid, u["attr"], _pack(u, USE_TYPES)) for u in facts["uses"]],
    )
    db.executemany(
        "INSERT INTO bases VALUES (?, ?, ?, ?)",
        [
            (fid, b["cls"], b["base"].rsplit(".", 1)[-1], _pack(b, BASE_TYPES))
            for b in facts["bases"]
        ],
    )
    db.executemany(
        "INSERT INTO envs VALUES (?, ?, ?)",
        [(fid, e["name"], _pack(e, ENV_TYPES)) for e in facts["envs"]],
    )


def _installed(directory: Path) -> bool:
    """A directory packages were installed into (it holds ``*.dist-info``, or an ``*.egg-info``
    that records installed files or has no source package beside it), such as a vendored
    dependency folder for a deployment bundle: not the repository's own code. An egg-info next
    to its package is an editable install of the repository itself, found by the distribution's
    name (``src/app.egg-info`` beside ``src/app``) or by the import packages its
    ``top_level.txt`` lists, since the two names need not match."""
    try:
        with os.scandir(directory) as entries:
            names = {e.name for e in entries}
    except OSError:
        return False
    if any(n.endswith(".dist-info") for n in names):
        return True
    for n in names:
        if not n.endswith(".egg-info"):
            continue
        if _regular(directory / n, "installed-files.txt"):
            return True
        name = n.removesuffix(".egg-info")
        packages = {name, name.replace("-", "_"), *_top_level(directory / n)}
        if not names & (packages | {f"{p}.py" for p in packages}):
            return True
    return False


def _regular(folder: Path, name: str) -> bool:
    """Whether ``folder/name`` is a regular file reached through no link: package metadata
    in the checkout is read only there, never through a link out of it or from a device."""
    try:
        for path in (folder, folder / name):
            info = os.lstat(path)
            if _is_link(info):
                return False
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode)


def _top_level(egg_info: Path) -> set[str]:
    """The import packages an egg-info's ``top_level.txt`` lists (read up to 64 KB), from a
    regular file reached through no link."""
    if not _regular(egg_info, "top_level.txt"):
        return set()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(egg_info / "top_level.txt", flags)
    except OSError:
        return set()
    with os.fdopen(fd, "rb") as f:
        # Swapped for something else since the check: refused, not read.
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            return set()
        try:
            text = f.read(65_536).decode("utf-8", "replace")
        except OSError:
            return set()
    return {line.strip() for line in text.splitlines() if line.strip()}


def _own_code(cache: Cache, fingerprint: dict[str, str]) -> dict[str, str]:
    """``fingerprint`` without the files under an installed-packages directory."""
    verdict: dict[str, bool] = {}

    def vendored(parts: tuple[str, ...]) -> bool:
        for depth in range(1, len(parts)):
            key = "/".join(parts[:depth])
            if key not in verdict:
                verdict[key] = _installed(cache.root / key)
            if verdict[key]:
                return True
        return False

    return {rel: sha for rel, sha in fingerprint.items() if not vendored(tuple(rel.split("/")))}


def update(cache: Cache, config: Config, force: bool = False) -> sqlite3.Connection:
    """The code index, brought up to date with the files on disk: only files whose content
    changed are read again. Returns an open connection for queries."""
    found = cache.scan(None, (".py", *(m for m in _PROJECT_MARKERS if not m.endswith(".py"))))
    cache.flush()
    fingerprint = _own_code(cache, {rel: sha for rel, sha in found.items() if rel.endswith(".py")})
    # A path that is not valid UTF-8 (a POSIX file name byte) cannot be stored: it is kept under
    # an escaped name, reported and not read.
    undecodable = {
        _escaped(rel): sha for rel, sha in fingerprint.items() if _escaped(rel) != rel
    }
    fingerprint = {
        rel: sha for rel, sha in fingerprint.items() if _escaped(rel) == rel
    } | undecodable
    roots = json.dumps(sorted(project_roots(found)))
    tests = tuple(p.rstrip("/") + "/" for p in config.test_paths)
    db = _connect(cache)
    try:
        db.execute("BEGIN IMMEDIATE")
        # Whether `src/` is a package decides every module name under it: a change rebuilds.
        src_package = "src/__init__.py" in fingerprint
        rebuilt = force or not _current(db) or _src_package(db) != src_package
        if rebuilt:
            _reset(db)
            db.execute("INSERT INTO meta VALUES ('src_package', ?)", (str(int(src_package)),))
        try:
            known = {
                rel: sha for rel, sha in db.execute("SELECT rel, sha FROM files")
                if isinstance(rel, str) and isinstance(sha, str)
            }
        except sqlite3.DatabaseError:
            _reset(db)
            db.execute("INSERT INTO meta VALUES ('src_package', ?)", (str(int(src_package)),))
            rebuilt, known = True, {}
        changed = [rel for rel, sha in fingerprint.items() if known.get(rel) != sha]
        for rel in [*changed, *(rel for rel in known if rel not in fingerprint)]:
            _forget(db, rel)
        for rel in changed:
            if rel in undecodable:
                facts = {**parse_source(""), "error": _UNDECODABLE}
            else:
                facts = _facts(cache, rel)
            test = is_test_module(rel) or rel.startswith(tests)
            _record(db, rel, fingerprint[rel], test, facts, src_package)
        # A change of `test_paths` reclassifies the files already read; their facts still hold.
        test_paths = json.dumps(sorted(tests))
        if _meta(db, "test_paths") != test_paths:
            db.executemany(
                "UPDATE files SET test = ? WHERE id = ?",
                [
                    (int(is_test_module(rel) or rel.startswith(tests)), fid)
                    for fid, rel in db.execute("SELECT id, rel FROM files").fetchall()
                    if isinstance(rel, str)
                ],
            )
            db.execute("INSERT OR REPLACE INTO meta VALUES ('test_paths', ?)", (test_paths,))
        # The project roots only steer how imports are looked up: nothing is read again.
        if _meta(db, "roots") != roots:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('roots', ?)", (roots,))
        if changed or len(known) != len(fingerprint):
            db.execute("INSERT OR REPLACE INTO meta VALUES ('built_at', ?)", (utc_now(),))
        db.execute("COMMIT")
        if rebuilt:
            db.execute("VACUUM")  # the dropped tables' pages, returned to the disk
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        db.close()
        raise
    return db


def summary(db: sqlite3.Connection) -> dict[str, Any]:
    """Counts, when the index last changed, and the files that could not be read."""
    files = db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    defs = db.execute("SELECT COUNT(*) FROM defs").fetchone()[0]
    built = db.execute("SELECT value FROM meta WHERE key = 'built_at'").fetchone()
    errors = [
        {"file": rel, "error": error}
        for rel, error in db.execute(
            "SELECT rel, error FROM files WHERE error IS NOT NULL ORDER BY rel"
        )
        if isinstance(rel, str) and isinstance(error, str)
    ]
    return {
        "files": files, "defs": defs, "built_at": built[0] if built else "", "errors": errors,
    }


# --- questions ---------------------------------------------------------------------------------


class Index:
    """Queries over the code index; rows are read on demand and checked as they are read."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        # How many ``instance_of`` lookups are open: a variable made from another variable's
        # member (`a = b.Make()`, `b = a.Make()`) recurses, so the chain is bounded.
        self._making = 0
        self.src_package = bool(_src_package(db))
        self.roots = _roots(_meta(db, "roots"))
        self._known: dict[str, bool] = {}
        self._defs: dict[str, tuple[str, dict[str, Any]] | None] = {}
        self._defs_bytes = 0
        self._binds: dict[tuple[str, str, str, int], dict[str, tuple[bool, str, int]]] = {}
        self._tests: dict[str, bool] = {}
        # File -> its innermost project root: a deep file pays for its depth once, not per row.
        self._root: dict[str, str] = {}
        self.find_cut = False

    def definition(self, full: str) -> tuple[str, dict[str, Any]] | None:
        """The definition of ``full``: within a file the last one, as Python binds it, so the
        implementation after its ``@overload`` stubs (rows are stored in source order)."""
        if full not in self._defs:
            row = self.db.execute(
                "SELECT f.rel, d.json FROM defs d JOIN files f ON f.id = d.file"
                " WHERE d.full = ? ORDER BY f.rel, d.rowid DESC LIMIT 1",
                (full,),
            ).fetchone()
            # Bounded by count and by the stored size of what it holds: a few receivers with
            # huge signatures would otherwise keep every decoded one for the whole query.
            size = len(row[1]) if row is not None and isinstance(row[1], str) else 0
            if (
                len(self._defs) >= MAX_CACHED
                or self._defs_bytes + size > MAX_CACHED_DEF_BYTES
            ):
                self._defs.clear()
                self._defs_bytes = 0
            self._defs[full] = None if row is None else (row[0], _row(row[1], DEF_TYPES))
            self._defs_bytes += size
        return self._defs[full]

    def spans(self, rel: str, full: str) -> list[tuple[tuple[int, int], tuple[int, int]]]:
        """Where each definition of ``full`` in ``rel`` (every assignment of a variable) starts
        and where it binds, as (line, column) pairs, in source order."""
        out = []
        for (raw,) in self.db.execute(
            "SELECT d.json FROM defs d JOIN files f ON f.id = d.file"
            " WHERE f.rel = ? AND d.full = ? ORDER BY d.rowid", (rel, full),
        ):
            row = _row(raw, DEF_TYPES)
            out.append(_span(row))
        return out

    def bases(self, rel: str, cls: str) -> list[str]:
        """The bases of class ``cls`` (its qualified name) in ``rel``, as the index names them;
        a base it cannot place is left out."""
        module = module_name(rel, self.src_package)
        scope = cls.rpartition(".")[0]
        out = []
        for (raw,) in self.db.execute(
            "SELECT b.json FROM bases b JOIN files f ON f.id = b.file"
            " WHERE f.rel = ? AND b.cls = ? ORDER BY b.rowid", (rel, cls),
        ):
            row = _row(raw, BASE_TYPES)
            head, _, rest = row["base"].partition(".")
            bound = self.binding(rel, scope, head, row["line"])
            if bound is not None:
                full = f"{bound}.{rest}" if rest else bound
            else:
                local = f"{scope}.{head}" if scope else head
                full = f"{module}.{local}" if module else local
                full = f"{full}.{rest}" if rest else full
            out.append(self.resolve(full))
        return out

    def members(self, rel: str, cls: str) -> dict[str, dict[str, Any]]:
        """The names defined directly in class ``cls`` of ``rel`` (methods, nested classes and
        class attributes), each with its definition row."""
        module = module_name(rel, self.src_package)
        prefix = f"{module}.{cls}" if module else cls
        out: dict[str, dict[str, Any]] = {}
        for (raw,) in self.db.execute(
            "SELECT d.json FROM defs d JOIN files f ON f.id = d.file"
            " WHERE f.rel = ? AND d.full > ? AND d.full < ? ORDER BY d.rowid",
            (rel, f"{prefix}.", f"{prefix}/"),
        ):
            row = _row(raw, DEF_TYPES)
            owner, _, name = row["name"].rpartition(".")
            if owner == cls:
                out[name] = row
        return out

    def modules_named(self, name: str, limit: int = MAX_MATCHES) -> list[tuple[str, str]]:
        """(module, file) of each indexed module called ``name``: its full dotted name, or one
        ending in ``.name``; at most ``limit``, exact matches first. A namespace package (no
        ``__init__.py``) is named by the modules below it, with its folder as the file."""
        exact, tail = [], []
        known: dict[str, str] = {}
        packages: dict[str, str] = {}
        for (rel,) in self.db.execute("SELECT rel FROM files WHERE error IS NULL ORDER BY rel"):
            if not isinstance(rel, str):
                continue
            module = module_name(rel, self.src_package)
            known.setdefault(module, rel)
            parts, folder = module.split("."), PurePosixPath(rel).parent
            if PurePosixPath(rel).name == "__init__.py":
                folder = folder.parent
            for depth in range(len(parts) - 1, 0, -1):
                packages.setdefault(".".join(parts[:depth]), f"{folder.as_posix()}/")
                folder = folder.parent
        for module, rel in [*known.items(), *(
            (m, f) for m, f in packages.items() if m not in known
        )]:
            if module == name:
                exact.append((module, rel))
            elif module.endswith(f".{name}"):
                tail.append((module, rel))
        return (sorted(exact) + sorted(tail))[:limit]

    def _imported(
        self, where: str, args: tuple[Any, ...], limit: int
    ) -> Iterator[dict[str, Any]]:
        """Import bindings matching ``where``, each target as the index names it (a nested
        project's own imports resolved from its folder). A definition is kept as a binding too:
        it is the one whose full name the same file defines."""
        for rel, test, alias, target, scope, line in self.db.execute(
            "SELECT f.rel, f.test, b.alias, b.target, b.scope, b.line FROM binds b"
            f" JOIN files f ON f.id = b.file WHERE ({where}) AND NOT EXISTS ("
            "SELECT 1 FROM defs d WHERE d.full = b.target AND d.file = b.file)"
            " ORDER BY f.rel, b.line LIMIT ?",
            (*args, limit),
        ):
            if not all(isinstance(v, str) for v in (rel, alias, target, scope)):
                continue
            yield {
                "file": rel, "line": line, "scope": scope, "imports": self.located(rel, target),
                "as": alias, "test": bool(test),
            }

    def importers(self, module: str, limit: int = MAX_IMPORTS) -> list[dict[str, Any]]:
        """Each import, in another file, of ``module`` or of a name from it (``import m``,
        ``from m import f``, ``from m.sub import g``), with the file, line and local name. In a
        nested project the module is also imported by its name from the project's folder
        (``from lib import f`` for ``apps.web.lib``), so that spelling is looked for too."""
        spellings = [module]
        for root in sorted(self.roots, key=len, reverse=True):
            base = root.replace("/", ".")
            if module.startswith(f"{base}."):
                local = module[len(base) + 1:]
                spellings += [local, local[4:]] if local.startswith("src.") else [local]
                break
        where = " OR ".join(["b.target = ? OR substr(b.target, 1, ?) = ?"] * len(spellings))
        args = tuple(v for m in spellings for v in (m, len(m) + 1, f"{m}."))
        return [
            row for row in self._imported(where, args, limit)
            if (row["imports"] == module or row["imports"].startswith(f"{module}."))
            and module_name(row["file"], self.src_package) != module
        ]

    def imports_of(self, rel: str, limit: int = MAX_IMPORTS) -> list[dict[str, Any]]:
        """What the file ``rel`` imports, each resolved to the full name it binds."""
        return list(self._imported("f.rel = ?", (rel,), limit))

    def classes_with_bases(self, limit: int) -> list[tuple[str, str]]:
        """(file, class) of up to ``limit`` classes in test files that have a base."""
        return [
            (rel, cls) for rel, cls in self.db.execute(
                "SELECT DISTINCT f.rel, b.cls FROM bases b JOIN files f ON f.id = b.file"
                " WHERE f.test = 1 ORDER BY f.rel, b.cls LIMIT ?", (limit,),
            )
        ]

    def subclasses_named(self, tail: str) -> list[tuple[str, str]]:
        """(file, class) of every class with a base whose last part is ``tail``: candidates,
        to be checked with ``bases``."""
        return [
            (rel, cls) for rel, cls in self.db.execute(
                "SELECT DISTINCT f.rel, b.cls FROM bases b JOIN files f ON f.id = b.file"
                " WHERE b.tail = ?", (tail,),
            )
        ]

    def definitions(self, fulls: Iterable[str]) -> dict[str, tuple[str, dict[str, Any]]]:
        """``definition`` of many names in a few queries: the found ones, by name."""
        found: dict[str, tuple[str, dict[str, Any]]] = {}
        wanted = sorted(set(fulls))
        for start in range(0, len(wanted), 500):
            chunk = wanted[start:start + 500]
            marks = ", ".join("?" * len(chunk))
            # per name, the first file and within it the last row, as ``definition`` picks
            for full, rel, raw in self.db.execute(
                "SELECT d.full, f.rel, d.json FROM defs d JOIN files f ON f.id = d.file"
                f" WHERE d.full IN ({marks}) ORDER BY d.full, f.rel, d.rowid DESC",
                chunk,
            ):
                if full not in found:
                    found[full] = (rel, _row(raw, DEF_TYPES))
        return found

    def lines(self, rel: str) -> list[int]:
        """The ``def`` or ``class`` line of every definition in ``rel``, sorted."""
        return sorted(
            _row(raw, DEF_TYPES)["line"]
            for (raw,) in self.db.execute(
                "SELECT d.json FROM defs d JOIN files f ON f.id = d.file WHERE f.rel = ?", (rel,)
            )
        )

    def _export(self, name: str) -> str | None:
        """What the module-level name ``name`` finally stands for when another module binds
        it there (an import), or None when its last binding is its own definition."""
        row = self.db.execute(
            "SELECT b.target, f.rel FROM binds b JOIN files f ON f.id = b.file"
            " WHERE b.name = ? AND b.scope = '' ORDER BY b.line DESC, b.rowid DESC LIMIT 1",
            (name,),
        ).fetchone()
        if row is None or not isinstance(row[0], str) or not isinstance(row[1], str):
            return None
        target = self.located(row[1], row[0])
        return target if target != name else None

    def _binds_at(
        self, rel: str, scope: str, name: str, before: int = 0
    ) -> dict[str, tuple[bool, str, int]]:
        """Scope -> (seen from nested scopes, target, line) of the latest binding of ``name`` in
        ``scope``, each scope enclosing it and the module (""). With ``before``, in ``scope``
        itself (and the module, for a module-level or top-level class body) only a binding from
        before that line counts: that code runs top to bottom. SQLite picks the one row per
        scope through the (file, alias, scope, line) index, so a lookup keeps the scope's depth
        in rows, not the file's bindings; the cache is emptied past ``MAX_CACHED`` lookups."""
        key = (rel, scope, name, before)
        if key not in self._binds:
            chain, s = {""}, scope
            while s:
                chain.add(s)
                s = s.rpartition(".")[0]
            cut = ({scope, ""} if "." not in scope else {scope}) if before else set()
            found: dict[str, tuple[bool, str, int]] = {}
            for batch in _batches(chain):
                marks = ", ".join("?" * len(batch))
                cuts = ", ".join("?" * len(cut & set(batch)))
                for s, deep, target, line in self.db.execute(
                    "SELECT b.scope, b.deep, b.target, MAX(b.line) FROM binds b"
                    " JOIN files f ON f.id = b.file WHERE f.rel = ? AND b.alias = ?"
                    f" AND b.scope IN ({marks})"
                    + (f" AND (b.scope NOT IN ({cuts}) OR b.line < ?)" if cuts else "")
                    + " GROUP BY b.scope",
                    (rel, name, *batch, *(sorted(cut & set(batch)) + [before] if cuts else [])),
                ):
                    if isinstance(s, str) and isinstance(target, str) and type(line) is int:
                        found[s] = (bool(deep), target, line)
            if len(self._binds) >= MAX_CACHED:
                self._binds.clear()
            self._binds[key] = found
        return self._binds[key]

    def bound_line(self, rel: str, scope: str, name: str, before: int) -> int:
        """The line of the latest import before ``before`` that binds ``name`` in ``scope``
        itself, or 0."""
        hit = self._binds_at(rel, scope, name, before).get(scope) if scope else None
        return hit[2] if hit is not None else 0

    def binding(self, rel: str, scope: str, name: str, before: int = 0) -> str | None:
        """What ``name`` stands for in ``scope`` of ``rel``: the import in the innermost
        enclosing function that binds it, else the module-level binding. With ``before`` (code
        at module level or in a class body, which runs top to bottom), the binding of that
        moment: only one from before that line counts in its own scope."""
        found, here = self._binds_at(rel, scope, name, before), True
        while scope:
            hit = found.get(scope)
            if hit is not None and (hit[0] or here):
                return self.located(rel, hit[1])
            scope, here = scope.rpartition(".")[0], False
        module = found.get("")
        return self.located(rel, module[1]) if module is not None else None

    def located(self, rel: str, target: str) -> str:
        """``target``, an import path written in ``rel``, as the index names it. Module names
        start at the repository root; code in a nested project (``apps/api`` with its own
        ``pyproject.toml``) imports from its own folder, or from its ``src/`` on the path, so
        an import path the index does not know is looked up there."""
        if not self.roots:
            return target
        if rel not in self._root:
            if len(self._root) >= MAX_CACHED:
                self._root.clear()
            self._root[rel] = root_of(rel, self.roots)
        root = self._root[rel]
        if not root or target.count(".") >= MAX_IMPORT_PARTS or self._known_depth(target):
            return target
        base = root.replace("/", ".")
        for prefix in (base, f"{base}.src"):
            # The candidate counts only when it names something inside the project: a module
            # or definition below the prefix, not merely the prefix's own folders.
            if self._known_depth(f"{prefix}.{target}") > prefix.count(".") + 1:
                return f"{prefix}.{target}"
        return target

    def _known_depth(self, name: str) -> int:
        """How many leading parts of ``name`` the index knows: the longest prefix that is a
        definition or a module-level binding (an import a package re-exports), or a module
        holding one (0 when not even the first part is). At most ``MAX_IMPORT_PARTS`` parts are
        looked at, so a crafted dotted name costs a bounded number of lookups."""
        parts = name.split(".")[:MAX_IMPORT_PARTS]
        for depth in range(len(parts), 0, -1):
            prefix = ".".join(parts[:depth])
            if prefix not in self._known:
                if len(self._known) >= MAX_CACHED:
                    self._known.clear()
                span = (prefix, f"{prefix}.", f"{prefix}/")
                self._known[prefix] = self.db.execute(
                    "SELECT 1 FROM defs WHERE full = ? OR (full > ? AND full < ?)"
                    " UNION ALL SELECT 1 FROM binds WHERE scope = '' AND"
                    " (name = ? OR (name > ? AND name < ?)) LIMIT 1",
                    (*span, *span),
                ).fetchone() is not None
            if self._known[prefix]:
                return depth
        return 0

    def env_reads(self, name: str, limit: int = 10_000) -> list[dict[str, Any]]:
        """Every place that reads, sets or tests environment variable ``name`` through ``os``
        (``os.environ``, ``os.getenv`` and their imported names), ``file`` and ``test`` added;
        a name as written that does not resolve to ``os``'s is left out. At most ``limit``."""
        out: list[dict[str, Any]] = []
        for rel, raw in self.db.execute(
            "SELECT f.rel, e.json FROM envs e JOIN files f ON f.id = e.file"
            " WHERE e.name = ? ORDER BY f.rel, e.rowid", (name,),
        ):
            row = _row(raw, ENV_TYPES)
            head, _, rest = row["head"].partition(".")
            before = 0 if _running_scope(row["scope"]) else row["line"]
            bound = self.binding(rel, row["scope"], head, before)
            if bound is None or ".".join(filter(None, (bound, rest))) not in ENV_HEADS:
                continue
            out.append({**row, "file": rel, "test": self.is_test(rel)})
            if len(out) >= limit:
                break
        return out

    def is_test(self, rel: str) -> bool:
        if rel not in self._tests:
            row = self.db.execute("SELECT test FROM files WHERE rel = ?", (rel,)).fetchone()
            self._tests[rel] = bool(row and row[0])
        return self._tests[rel]

    def resolve(self, full: str) -> str:
        """``full`` with re-exports followed: ``app.services.create`` becomes
        ``app.services.orders.create`` when the package re-exports it. A name inside a
        re-exported class (``pkg.Model.save``) is followed through its prefix."""
        for _ in range(MAX_HOPS):
            # The module's binding first: an import after a definition of the same name wins.
            moved = self._export(full)
            if moved is not None:
                full = moved
                continue
            if self.definition(full) is not None:
                return full
            head, _, tail = full.rpartition(".")
            if not head:
                return full
            prefix = self._resolve_prefix(head)
            if prefix == head:
                return full
            full = f"{prefix}.{tail}"
        return full

    def _resolve_prefix(self, name: str, depth: int = 0) -> str:
        """``name`` with re-exports followed, on itself or on a shorter prefix: ``pkg.api.Service``
        is ``pkg.implementation.Service`` when ``pkg`` re-exports ``implementation as api``."""
        for _ in range(MAX_HOPS):
            moved = self._export(name)
            if moved is None:
                if self.definition(name) is not None:
                    return name
                break
            name = moved
        head, _, tail = name.rpartition(".")
        if not head or depth >= MAX_HOPS:
            return name
        prefix = self._resolve_prefix(head, depth + 1)
        return name if prefix == head else f"{prefix}.{tail}"

    #: The definitions a name can mean: its last part is the tail, and it is the full name or a
    #: dotted suffix of it.
    _MATCHING = " FROM defs WHERE tail = ? AND (full = ? OR substr(full, -?) = ?)"

    def _matching(self, name: str, kind: str | None = None) -> tuple[str, list[Any]]:
        """The ``WHERE`` clause of the definitions ``name`` can mean, of ``kind`` if given, and
        its parameters."""
        suffix = f".{name}"
        params: list[Any] = [name.rsplit(".", 1)[-1], name, len(suffix), suffix]
        if kind is None:
            return self._MATCHING, params
        return f"{self._MATCHING} AND json_extract(json, '$[1]') = ?", [*params, kind]

    def find(
        self, name: str, limit: int = MAX_MATCHES, kind: str | None = None
    ) -> list[str]:
        """Full names of the definitions ``name`` can mean: a full dotted name, a qualified
        name (``Class.method``) or a bare name; at most ``limit`` of them, in order, whose
        stored definitions add up to at most ``MAX_MATCH_BYTES`` (the first always counts).
        ``find_cut`` says whether the size stopped the list; ``count_matches`` says how many
        there are. With ``kind``, only definitions of that kind count, before the limit."""
        self.find_cut = False
        resolved = self.resolve(name)
        definition = self.definition(resolved)
        if definition is not None and (kind is None or definition[1]["kind"] == kind):
            return [resolved]
        where, params = self._matching(name, kind)
        found: list[str] = []
        size = 0
        for full, length in self.db.execute(
            f"SELECT full, MAX(length(json)){where} GROUP BY full ORDER BY full LIMIT ?",
            (*params, limit),
        ):
            if not isinstance(full, str) or type(length) is not int:
                continue
            if found and size + length > MAX_MATCH_BYTES:
                self.find_cut = True
                break
            found.append(full)
            size += length
        return found

    def count_matches(self, name: str, kind: str | None = None) -> int:
        where, params = self._matching(name, kind)
        row = self.db.execute(f"SELECT COUNT(DISTINCT full){where}", params).fetchone()
        return row[0] if row is not None and isinstance(row[0], int) else 0

    def of_kind(self, kind: str, limit: int) -> list[str]:
        """Full names of the definitions of ``kind`` (``class``, ``function``), in order; at most
        ``limit``."""
        return [
            full for (full,) in self.db.execute(
                "SELECT DISTINCT full FROM defs WHERE json_extract(json, '$[1]') = ? "
                "ORDER BY full LIMIT ?",
                (kind, limit),
            )
            if isinstance(full, str)
        ]

    def kinds(self) -> list[str]:
        """Every kind of definition the index holds."""
        return [
            kind for (kind,) in self.db.execute(
                "SELECT DISTINCT json_extract(json, '$[1]') FROM defs ORDER BY 1"
            )
            if isinstance(kind, str)
        ]

    def similar(self, name: str, limit: int = 10) -> list[str]:
        """Defined names whose last part contains the last part of ``name``."""
        needle = name.rsplit(".", 1)[-1].lower()
        found = set()
        for (full,) in self.db.execute("SELECT DISTINCT full FROM defs WHERE instr(lower(tail), ?)",
                                       (needle,)):
            if isinstance(full, str):
                found.add(full)
            if len(found) >= max(200, limit):
                break
        return sorted(found)[:limit]

    def aliases(self, tails: set[str], limit: int = MAX_ALIASES) -> set[str]:
        """``tails`` and every local name bound to one of them under another name
        (``from lib import create as renamed``), followed through re-exports that rename; at
        most ``limit`` names, read in batches."""
        found = set(tails)
        frontier = set(tails)
        for _ in range(MAX_HOPS):
            new: set[str] = set()
            for batch in _batches(frontier):
                for (alias,) in self.db.execute(
                    "SELECT DISTINCT alias FROM binds WHERE alias != tail"
                    f" AND tail IN ({', '.join('?' * len(batch))})",
                    batch,
                ):
                    if isinstance(alias, str) and alias not in found and len(found) < limit:
                        found.add(alias)
                        new.add(alias)
            if not new:
                break
            frontier = new
        return found

    def _named(
        self, table: str, column: str, types: dict[str, Any], tails: set[str]
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        """The rows of ``table`` whose ``column`` is one of ``tails``, decoded one at a time and
        queried in batches."""
        for batch in _batches(tails):
            for rel, text in self.db.execute(
                f"SELECT f.rel, x.json FROM {table} x JOIN files f ON f.id = x.file"
                f" WHERE x.{column} IN ({', '.join('?' * len(batch))}) ORDER BY f.rel, x.rowid",
                batch,
            ):
                yield rel, _row(text, types)

    def calls_named(self, tails: set[str]) -> Iterator[tuple[str, dict[str, Any]]]:
        return self._named("calls", "attr", CALL_TYPES, tails)

    def patches_named(self, tails: set[str]) -> Iterator[tuple[str, dict[str, Any]]]:
        return self._named("patches", "tail", PATCH_TYPES, tails)

    def uses_named(self, tails: set[str]) -> Iterator[tuple[str, dict[str, Any]]]:
        return self._named("uses", "attr", USE_TYPES, tails)

    def instance_of(self, rel: str, definition: dict[str, Any]) -> str:
        """The class a variable defined in ``rel`` is an instance of, from what made it (its
        annotation or constructor call, resolved where it is written), or "" when that is not a
        class the index knows. A variable assigned again later is read at its last assignment,
        as ``definition`` gives it."""
        made = definition["made"]
        if not made or self._making >= MAX_HOPS:
            return ""
        scope = definition["name"].rpartition(".")[0]
        self._making += 1
        try:
            target = self.call_target(
                rel,
                {
                    "callee": made, "recv": "", "attr": made.rsplit(".", 1)[-1], "scope": scope,
                    "cls": scope, "line": definition["line"], "local": False, "glob": False,
                },
            )
        finally:
            self._making -= 1
        found = self.definition(target) if target else None
        return target if found is not None and found[1]["kind"] == "class" else ""

    def call_target(self, rel: str, call: dict[str, Any]) -> str:
        """The full name a call resolves to, or "" when the receiver's type is unknown."""
        callee = call["callee"]
        if not callee and call["recv"]:
            made = self.call_target(rel, {**call, "callee": call["recv"], "recv": ""})
            found = self.definition(made) if made else None
            if found is not None and found[1]["kind"] == "class":
                member = self.resolve(f"{made}.{call['attr']}")
                # Not defined on the class (inherited, or set at run time): by name only.
                return member if self.definition(member) is not None else ""
            return ""
        if not callee:
            return ""
        head, _, rest = callee.partition(".")
        module = module_name(rel, self.src_package)
        if head in ("self", "cls") and call["cls"] and rest and "." not in rest:
            member = self.resolve(f"{module}.{call['cls']}.{rest}")
            # Not defined on the class (inherited, or set at run time): unknown, so by name only.
            return member if self.definition(member) is not None else ""
        if call["local"]:
            return ""  # a parameter or local of the same name, not the file's binding
        # Code at module level runs top to bottom: it means the binding of its own moment.
        before = 0 if _running_scope(call["scope"]) else call["line"]
        local: str | None = None
        if call["cls"] and call["scope"] == call["cls"]:
            # In the class body itself (not its methods) the class's latest binding of the name
            # before the call comes first, a definition or an import: the body runs top to
            # bottom, so a later one does not count yet.
            before = call["line"]
            member = f"{module}.{call['cls']}.{head}"
            found = self.definition(member)
            defined = (
                found[1]["line"] if found is not None and found[0] == rel
                and found[1]["line"] < before else 0
            )
            imported = self.bound_line(rel, call["scope"], head, before)
            if defined and not defined < imported < before:
                if not rest:
                    return self.resolve(member)
                # `settings = Settings()` in the body, then `settings.TABLE`: what follows the
                # class's own variable goes through the same walk as a module's.
                local = member
        if local is None:
            local = (
                self.binding(rel, "", head) if call["glob"]
                else self.binding(rel, call["scope"], head, before)
            )
        if local is None:
            return ""
        on_class = False
        if rest:
            # `svc = make(); svc.save()`, or `state.svc.save()` with `svc` a variable of the
            # module `state`: past a variable the type is unknown, unless the variable was made
            # by its class (`settings = Settings()`, `settings: Settings = ...`): then what
            # follows is a member of that class.
            parts = rest.split(".")
            i, hops = 0, 0
            while i < len(parts):
                found = self.definition(self.resolve(".".join([local, *parts[:i]])))
                if found is not None and found[1]["kind"] == "variable":
                    made = self.instance_of(*found) if hops < MAX_HOPS else ""
                    if not made:
                        return ""
                    local, parts, i, hops, on_class = made, parts[i:], 0, hops + 1, True
                    continue
                on_class = on_class or (found is not None and found[1]["kind"] == "class")
                i += 1
            rest = ".".join(parts)
        full = f"{local}.{rest}" if rest else local
        if on_class and self.definition(self.resolve(full)) is None:
            # `Child.build()` of a member the class does not define (inherited, or set at run
            # time): unknown, so by name only, as for `self` and a constructed instance.
            return ""
        # The file's own definition, chosen as the binding of the call's moment: a later import
        # of the name (the module's final binding, which `resolve` follows) does not apply yet.
        if local == (f"{module}.{head}" if module else head) and self.definition(full):
            return full
        return self.resolve(full)

    def patch_target(self, rel: str, patch: dict[str, Any]) -> str:
        """The full name a patch replaces: a string target is already an import path; an
        object target (``patch.object(orders, "create")``) is resolved through the file."""
        target = patch["target"]
        if patch["how"] in ("patch", "monkeypatch.setattr"):
            return self.resolve(self.located(rel, target))
        if patch["local"]:
            return ""  # `def test(lib): patch.object(lib, ...)`: the parameter, not the import
        head, _, rest = target.partition(".")
        if patch["glob"]:
            local = self.binding(rel, "", head)
        else:
            # At module level the receiver is the binding of the patch's own moment.
            before = 0 if _running_scope(patch["scope"]) else patch["line"]
            local = self.binding(rel, patch["scope"], head, before)
        return self.resolve(f"{local}.{rest}") if local else ""

    def patched_at(self, rel: str, line: int, tails: set[str], wanted: set[str]) -> bool:
        """Whether line ``line`` of ``rel`` patches one of ``wanted``: only that line's rows are
        read, through the (file, line) index, so a query keeps no set of every patch site."""
        for batch in _batches(tails):
            rows = self.db.execute(
                "SELECT x.json FROM patches x JOIN files f ON f.id = x.file"
                f" WHERE f.rel = ? AND x.line = ? AND x.tail IN ({', '.join('?' * len(batch))})",
                (rel, line, *batch),
            )
            if any(self.patch_target(rel, _row(text, PATCH_TYPES)) in wanted for (text,) in rows):
                return True
        return False


#: How many names one diagnostic lists before it says how many more there are.
MAX_NAMED = 10


def signature_shape(definition: dict[str, Any], bound: bool) -> dict[str, Any]:
    """What ``shape_problems`` needs of a signature, built once: with ``bound``, the first
    parameter is passed implicitly. ``index`` maps each positional name to its position."""
    skip = 1 if bound and definition["pos"] else 0
    pos = definition["pos"][skip:]
    return {
        "count": len(pos), "posonly": max(definition["posonly"] - skip, 0),
        "index": {name: i for i, name in enumerate(pos)},
        "required": max(len(pos) - definition["defaults"], 0),
        "kwonly": set(definition["kwonly"]), "kwreq": set(definition["kwreq"]),
    }


class _Shapes:
    """The signature shapes of one query, (target, through ``__init__``, bound) -> shape: each
    built once, not once a call. The kept ones hold at most ``budget`` parameters together, and
    the query builds at most ``work`` parameters in all: a shape that does not fit is built
    again when needed, until the work is spent, after which ``get`` returns None (the call is
    listed, its shape not checked). Nothing kept is dropped, so alternating calls cannot make
    every call rebuild a shape."""

    def __init__(self, budget: int, work: int) -> None:
        self.budget, self.work = budget, work
        self.kept: dict[tuple[str, bool, bool], dict[str, Any]] = {}
        self.size = 0

    def get(
        self, key: tuple[str, bool, bool], definition: dict[str, Any], bound: bool
    ) -> dict[str, Any] | None:
        if key in self.kept:
            return self.kept[key]
        size = len(definition["pos"]) + len(definition["kwonly"])
        if size > self.work:
            return None
        self.work -= size
        shape = signature_shape(definition, bound)
        if self.size + size <= self.budget:
            self.kept[key] = shape
            self.size += size
        return shape


def _listing(names: list[str], total: int) -> str:
    text = ", ".join(names[:MAX_NAMED])
    return f"{text} and {total - MAX_NAMED} more" if total > MAX_NAMED else text


def shape_problems(
    definition: dict[str, Any], call: dict[str, Any], bound: bool,
    shape: dict[str, Any] | None = None,
) -> list[str]:
    """What is wrong with how ``call`` passes arguments to ``definition`` (empty when it fits or
    when ``*args``/``**kwargs`` at the call site make it unknowable). ``shape`` is
    ``signature_shape(definition, bound)``, passed when one query checks many calls: the check
    then costs the call's size, not the signature's, and each message names at most
    ``MAX_NAMED`` names."""
    if call["star"] or call["dstar"] or definition["kind"] in ("class", "variable", "property"):
        return []
    if shape is None:
        shape = signature_shape(definition, bound)
    count, posonly, index = shape["count"], shape["posonly"], shape["index"]
    passed = call["pos"]
    problems = []
    if passed > count and not definition["star"]:
        kwonly = definition["kwonly"]
        hint = f"; keyword-only: {_listing(kwonly, len(kwonly))}" if kwonly else ""
        problems.append(f"{passed - count} positional argument(s) too many{hint}")
    # A positional-only name given as a keyword lands in `**kwargs`, or is an error without it.
    if not definition["dstar"]:
        misplaced = [k for k in call["kw"] if index.get(k, posonly) < posonly]
        if misplaced:
            listed = _listing(misplaced, len(misplaced))
            problems.append(f"positional-only passed by keyword: {listed}")
        unknown = [k for k in call["kw"] if k not in index and k not in shape["kwonly"]]
        if unknown:
            problems.append(f"unknown keyword(s): {_listing(unknown, len(unknown))}")
    twice = [k for k in call["kw"] if posonly <= index.get(k, passed) < passed]
    if twice:
        problems.append(f"multiple values for: {_listing(twice, len(twice))}")
    given = set(call["kw"])
    absent = len(shape["kwreq"]) - sum(1 for k in given if k in shape["kwreq"])
    if absent:
        # Scanning in signature order stops at the names shown: past the call's own keywords,
        # the first few absent ones are found within that many more steps.
        first = list(islice((k for k in definition["kwreq"] if k not in given), MAX_NAMED))
        problems.append(f"missing keyword-only: {_listing(first, absent)}")
    # Required positions the call fills neither by position nor by keyword.
    required, start = shape["required"], max(passed, posonly)
    named = sum(1 for k in given if start <= index.get(k, -1) < required)
    unfilled = max(required - passed, 0) - named
    if unfilled > 0:
        problems.append(f"{unfilled} required positional argument(s) missing")
    return problems


def _bound(index: Index, rel: str, call: dict[str, Any], target: str) -> bool:
    """Whether the call passes the first parameter implicitly: a method called on an instance
    (``self.save()``, ``order.save()``) or any classmethod, not ``Order.save(order)``."""
    found = index.definition(target)
    kind = found[1]["kind"] if found else ""
    if kind == "classmethod":
        return True
    if kind != "method" or (call["callee"] and "." not in call["callee"]):
        return False  # `create(1, 2)` in the class body: a plain function call, no `self`
    receiver = call["callee"].rpartition(".")[0]
    if not receiver:
        return True  # an instance made in place: `Service(repo).place(...)`
    if receiver == "cls":
        return False  # `cls.save(instance, ...)` in a classmethod: the class, not an instance
    owner = target.rpartition(".")[0]
    return index.call_target(rel, {**call, "callee": receiver, "recv": ""}) != owner


#: How many sites of each kind ``references`` keeps; past it they are counted, not kept, so a name
#: called hundreds of thousands of times costs no more memory than this many rows.
MAX_LISTED = 10_000
#: How many bytes of text the sites one ``references`` query keeps add up to at most, across its
#: lists; past it they are counted, not kept, so long scopes or names cannot multiply the rows.
MAX_LISTED_BYTES = 10_000_000
#: How many candidate rows (calls, value uses and patches of the name and its aliases) one query
#: resolves at most: each can cost a few lookups, so this bounds the work and the lookup cache.
MAX_EXAMINED = 100_000
#: How many variables assigned from a target ``references`` follows, one level each: they share
#: the rows-read budget, so this bounds the extra queries, not the work.
MAX_FOLLOWED = 20
#: How many binding lookups ``Index`` keeps cached before it empties the cache; ``references``
#: keeps as many per-line patch answers.
MAX_CACHED = 10_000
#: How many bytes of stored definitions ``Index.definition`` keeps decoded before it empties its
#: cache.
MAX_CACHED_DEF_BYTES = 8_000_000
#: How many dotted parts of an import path the project-root lookup examines: real module paths
#: have a handful, and a longer one is left as written.
MAX_IMPORT_PARTS = 32
#: How many parameters the signature shapes one ``references`` query keeps add up to at most; past
#: it they are dropped and rebuilt as needed, so many broad signatures of one name cost no more.
MAX_SHAPE_PARAMS = 100_000
#: How many parameters one ``references`` query reads into signature shapes in all, rebuilt ones
#: included; past it, calls are listed without a shape check (``counts["unchecked"]``).
MAX_SHAPE_WORK = 2_000_000


def references(
    index: Index, targets: list[str], keep: Callable[[dict[str, Any]], bool] | None = None,
    limit: int = MAX_LISTED, budget: list[int] | None = None, follow: bool = False,
    shape_of: str = "", unbound_functions: bool = False,
) -> dict[str, Any]:
    """Exact call sites of ``targets`` with their shape and problems, the patch sites, the places
    the name is used as a value (a line already listed as a patch is not), and the by-name
    candidates: calls of the same attribute on a receiver whose type is unknown. ``keep`` filters
    the sites (tests or code); ``counts`` holds the totals of the kept sites, of which each list
    holds at most ``limit``. ``budget`` (one number in a list) is the rows left to read, shared
    by callers that look up several names in turn. With ``follow``, a module or class variable
    assigned straight from a target (``TABLE = settings.TABLE``) is followed one level: its
    calls, patches and uses are listed too, each with ``via`` naming it. ``shape_of`` (set when
    following) is the definition whose signature checks the calls found: a call of a method
    through an alias binds ``self`` or not depending on what was assigned, so it is not checked,
    nor (with ``unbound_functions``, an alias in a class body) a call of a function."""
    wanted = set(targets)
    tails = {t.rsplit(".", 1)[-1] for t in targets}
    names = index.aliases(tails)
    lists: dict[str, list[dict[str, Any]]] = {
        "calls": [], "candidates": [], "patches": [], "uses": [],
    }
    shapes = _Shapes(MAX_SHAPE_PARAMS, MAX_SHAPE_WORK)
    # (file, line) -> whether that line patches the name: one read a line, not one a use.
    patched: dict[tuple[str, int], bool] = {}
    counts = dict.fromkeys(
        ("calls", "calls_in_tests", "problems", "unchecked", "candidates", "patches", "uses"), 0
    )
    # The variables to follow: full name -> (the name it is written under, the target it holds,
    # whether it is written in a class body, its file, the line of its last assignment).
    aliases: dict[str, tuple[str, str, bool, str, int]] = {}
    # Each such variable -> the lines of its assignments from a wanted name, the last or not.
    holding: dict[str, set[tuple[int, int]]] = {}
    skipped = False  # an alias past ``MAX_FOLLOWED``: the lists are then not complete

    kept = [0]

    def keep_row(kind: str, row: dict[str, Any]) -> None:
        size = _text_bytes(row)
        if len(lists[kind]) < limit and kept[0] + size <= MAX_LISTED_BYTES:
            lists[kind].append(row)
            kept[0] += size

    def add(kind: str, row: dict[str, Any]) -> bool:
        if keep is not None and not keep(row):
            return False
        counts[kind] += 1
        keep_row(kind, row)
        return True

    if budget is None:
        budget = [MAX_EXAMINED]

    def examine(rows: Iterator[tuple[str, dict[str, Any]]]) -> Iterator[tuple[str, dict[str, Any]]]:
        for row in rows:
            if budget[0] <= 0:
                return
            budget[0] -= 1
            yield row

    for rel, patch in examine(index.patches_named(names)):
        if index.patch_target(rel, patch) in wanted:
            add(
                "patches",
                {
                    "file": rel, "line": patch["line"], "how": patch["how"],
                    "target": patch["target"], "scope": patch["scope"],
                    "test": index.is_test(rel),
                },
            )
    for rel, call in examine(index.calls_named(names)):
        target = index.call_target(rel, call)
        base = {
            "file": rel, "line": call["line"], "col": call["col"], "scope": call["scope"],
            "test": index.is_test(rel), "shape": _shape_text(call),
        }
        found = index.definition(target) if target in wanted else None
        if found is not None:
            owner = shape_of or target
            checked = index.definition(owner) if shape_of else found
            unknown = ("method", "function") if unbound_functions else ("method",)
            if checked is None or (shape_of and checked[1]["kind"] in unknown):
                problems, shape = [], None
            else:
                definition, bound = checked[1], _bound(index, rel, call, owner)
                if definition["kind"] == "class":
                    init = index.definition(f"{owner}.__init__")
                    definition, bound = (init[1], True) if init else (definition, False)
                shape = shapes.get(
                    (owner, definition is not checked[1], bound), definition, bound
                )
                problems = [] if shape is None else shape_problems(definition, call, bound, shape)
            if add("calls", {**base, "target": target, "problems": problems}):
                counts["unchecked"] += shape is None
                counts["calls_in_tests"] += base["test"]
                counts["problems"] += bool(problems)
        elif (
            not target and call["attr"] in tails
            and (call["callee"] == "" or "." in call["callee"])
        ):
            add("candidates", {**base, "callee": call["callee"] or f"(…).{call['attr']}"})
    for rel, use in examine(index.uses_named(names)):
        # A patch is also an argument (`monkeypatch.setattr(x, "y", fake)` passes `fake`): only
        # patch sites are listed as patches.
        as_call = {
            "callee": use["name"], "recv": "", "cls": use["cls"], "attr": use["attr"],
            "local": False, "glob": use["glob"], "scope": use["scope"], "line": use["line"],
        }
        hit = index.call_target(rel, as_call)
        if hit not in wanted:
            continue
        spot = (rel, use["line"])
        if spot not in patched:
            if len(patched) >= MAX_CACHED:
                patched.clear()
            patched[spot] = index.patched_at(rel, use["line"], names, wanted)
        if not patched[spot]:
            row = {
                "file": rel, "line": use["line"], "col": use["col"], "how": use["how"],
                "within": use["within"], "scope": use["scope"], "test": index.is_test(rel),
            }
            add("uses", row)
            # Found whether or not ``keep`` lists the assignment itself: `--tests` still wants
            # the tests that use a production module's alias.
            if (
                follow and use["how"] == "assign"
                and use["within"].isidentifier() and use["scope"] in ("", use["cls"])
            ):
                module = module_name(rel, index.src_package)
                alias = ".".join(p for p in (module, use["scope"], use["within"]) if p)
                defined = index.definition(alias)
                if alias not in wanted and defined is not None and defined[0] == rel:
                    # Bounded by the uses examined (``budget``).
                    holding.setdefault(alias, set()).add((use["line"], use["col"]))
                # Only the variable's last assignment, as Python binds it: `A = VALUE` then
                # `A = 3` leaves `A` meaning 3.
                if (
                    alias not in wanted and alias not in aliases and defined is not None
                    and defined[0] == rel
                    and _span(defined[1])[0] <= (use["line"], use["col"]) <= _span(defined[1])[1]
                ):
                    if len(aliases) < MAX_FOLLOWED:
                        aliases[alias] = (
                            use["within"], hit, bool(use["scope"]), rel, defined[1]["line"]
                        )
                    else:
                        skipped = True
    cut = skipped
    for alias, (short, original, in_class, at, assigned) in aliases.items():
        spans = index.spans(at, alias)
        held_at = holding.get(alias, set())

        def held(
            row: dict[str, Any], at: str = at, assigned: int = assigned,
            spans: list[tuple[tuple[int, int], tuple[int, int]]] = spans,
            held_at: set[tuple[int, int]] = held_at,
        ) -> bool:
            # Module code and class bodies before the last assignment run with whatever the
            # variable held then: the latest assignment that ended before the site, by line and
            # column (a right-hand side reads the binding from before its own; a read after a
            # `;` on the same line reads the new one), must be one from the target. A function
            # body runs later, whatever its line. A patch (a row with neither `problems` nor
            # `within`) looks its target up when it is applied, so it is kept.
            site = "problems" in row or "within" in row
            if (
                site and row["file"] == at and row["line"] < assigned
                and _import_time(index, at, row)
            ):
                site_at = (row["line"], row["col"])
                last = next((s for s in reversed(spans) if s[1] <= site_at), None)
                if last is None or not any(last[0] <= at_ <= last[1] for at_ in held_at):
                    return False
            return keep is None or keep(row)

        # A call of the alias (`ALIAS = send; ALIAS()`) is checked against the target's own
        # signature, not the variable's empty one.
        # In a class body a function becomes a method of the class's instances (`emit = send`,
        # then `Client().emit()` passes `self`), so whether `self` is bound is not known.
        found = references(
            index, [alias], held, limit, budget, shape_of=original, unbound_functions=in_class
        )
        cut = cut or found["truncated"]
        # The alias's totals, not only the rows it kept: its lists are cut at ``limit`` too.
        for key in ("calls", "calls_in_tests", "problems", "unchecked", "patches", "uses"):
            counts[key] += found["counts"][key]
        for kind in ("calls", "patches", "uses"):
            for row in found[kind]:
                keep_row(kind, {**row, "via": short})
    truncated = cut or len(names) >= MAX_ALIASES or budget[0] <= 0 or any(
        counts[kind] > len(rows) for kind, rows in lists.items()
    )
    return {**lists, "counts": counts, "truncated": truncated}


def _import_time(index: Index, rel: str, row: dict[str, Any]) -> bool:
    """Whether a site of ``rel`` runs when the module is imported: in module code, a class body
    or a comprehension there, not inside a function."""
    parts = row["scope"].split(".") if row["scope"] else []
    while parts and parts[-1] in _EAGER:
        parts.pop()
    if not parts:
        return True
    module = module_name(rel, index.src_package)
    found = index.definition(".".join(p for p in (module, *parts) if p))
    return found is not None and found[0] == rel and found[1]["kind"] == "class"


def _text_bytes(row: dict[str, Any]) -> int:
    """The characters of a site row's text: its strings and the strings in its lists."""
    return sum(
        len(v) if isinstance(v, str)
        else sum(len(x) for x in v if isinstance(x, str)) if isinstance(v, list) else 0
        for v in row.values()
    )


def _shape_text(call: dict[str, Any]) -> str:
    parts = []
    if call["pos"]:
        parts.append(f"{call['pos']} positional")
    if call["kw"]:
        parts.append("kw " + ",".join(call["kw"]))
    if call["star"]:
        parts.append("*args")
    if call["dstar"]:
        parts.append("**kwargs")
    return "; ".join(parts) or "no arguments"
