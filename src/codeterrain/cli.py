"""The ``terrain`` command line."""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codeterrain import (
    __version__,
    calls_join,
    calls_static,
    code_static,
    fixtures_order,
    fixtures_static,
    impact,
    languages,
    protocol,
    routes_view,
    views,
)
from codeterrain.budget import (
    DEFAULT_BUDGET,
    cut_lines,
    field_safe,
    render_table,
    terminal_safe,
)
from codeterrain.cache import Cache, CacheBusy, age_text, checkouts
from codeterrain.config import (
    Config,
    ConfigError,
    load_config,
    resolve_root,
    user_config_path,
)
from codeterrain.executables import distrust
from codeterrain.plugins.manifest_join import ManifestError, route_key
from codeterrain.sandbox import SandboxError, probe_versions

PYTHON_ENV = "CODETERRAIN_PYTHON"
#: ``terrain refresh languages``: every installed language pack.
LANGUAGES = "languages"


class UsageError(Exception):
    """A bad argument value; reported as ``terrain: <message>`` with exit status 2."""


@dataclass
class Context:
    config: Config
    cache: Cache
    args: argparse.Namespace

    @property
    def python(self) -> str:
        return self.args.python or os.environ.get(PYTHON_ENV) or sys.executable


def _common() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", help="repository root (default: current directory)")
    common.add_argument("--config", help="TOML file with a [tool.codeterrain] table")
    common.add_argument("--cache-dir", help="cache location (or $CODETERRAIN_CACHE_DIR)")
    common.add_argument("--python", help="interpreter for the import and pytest tier")
    common.add_argument(
        "--budget",
        type=int,
        default=DEFAULT_BUDGET,
        help="cut text output at about N tokens (default %(default)s; 0 = no cut)",
    )
    common.add_argument("--json", action="store_true", help="print the full data as JSON")
    common.add_argument("--fresh", action="store_true", help="rebuild before answering")
    return common


def build_parser() -> argparse.ArgumentParser:
    common = _common()
    parser = argparse.ArgumentParser(
        prog="terrain",
        description="Answers about your code in under a second, from a cache (no LLM, no network).",
    )
    parser.add_argument("--version", action="version", version=f"terrain {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p = sub.add_parser("fixtures", parents=[common], help="list pytest fixtures")
    p.add_argument("substring", nargs="?", default="", help="name contains this")
    p.add_argument("--autouse", action="store_true", help="only autouse fixtures")
    p.add_argument("--scope", help="only this scope (session, package, module, class, function)")
    p.set_defaults(run=cmd_fixtures)

    p = sub.add_parser("fixture", parents=[common], help="one fixture and who requests it")
    p.add_argument("name")
    p.set_defaults(run=cmd_fixture)

    p = sub.add_parser("doctor", parents=[common], help="resolved config, cache and versions")
    p.set_defaults(run=cmd_doctor)

    p = sub.add_parser("setup-order", parents=[common], help="fixture setup order of a test")
    p.add_argument("nodeid", help="pytest node id; a function, class or file matches its tests")
    p.set_defaults(run=cmd_setup_order)

    p = sub.add_parser("routes", parents=[common], help="list routes of the configured apps")
    p.add_argument("substring", nargs="?", default="", help="path or handler contains this")
    p.add_argument("--app", help="only apps whose spec contains this")
    p.add_argument("--access", help="only this access class from the route manifest")
    p.set_defaults(run=cmd_routes)

    p = sub.add_parser("route", parents=[common], help="one route in full")
    p.add_argument("method")
    p.add_argument("path")
    p.set_defaults(run=cmd_route)

    p = sub.add_parser("calls", parents=[common], help="HTTP calls in JavaScript and TypeScript")
    p.add_argument("substring", nargs="?", default="", help="path or file contains this")
    p.add_argument("--method", help="only this HTTP method")
    p.add_argument("--unresolved", action="store_true", help="only calls whose path is not static")
    p.set_defaults(run=cmd_calls)

    p = sub.add_parser(
        "callers", parents=[common], help="front-end calls of a route (client_repos joined)"
    )
    p.add_argument("method", nargs="?", help="with PATH: only the calls of this route")
    p.add_argument("path", nargs="?")
    p.add_argument("--unmatched", action="store_true", help="only calls that hit no route")
    p.set_defaults(run=cmd_callers)

    p = sub.add_parser(
        "def", parents=[common],
        help="where a Python name, or a name of an installed language pack, is defined",
    )
    p.add_argument(
        "name", nargs="?", help="a bare, qualified (Class.method) or full dotted name"
    )
    p.add_argument(
        "--kind",
        help="only definitions of this kind (class, function, module, provider, ...); without a "
        "name, every one of them",
    )
    p.set_defaults(run=cmd_def)

    p = sub.add_parser(
        "refs", parents=[common], help="call sites of a Python name, their shape, and patches"
    )
    p.add_argument("name", help="a bare, qualified (Class.method) or full dotted name")
    side = p.add_mutually_exclusive_group()
    side.add_argument("--tests", action="store_true", help="only call sites in tests")
    side.add_argument("--code", action="store_true", help="only call sites outside tests")
    p.add_argument(
        "--by-name", action="store_true",
        help="also list calls of the same name on a receiver whose type is unknown",
    )
    p.set_defaults(run=cmd_refs)

    p = sub.add_parser(
        "importers", parents=[common],
        help="where a Python module, or a name from it, is imported",
    )
    p.add_argument("module", help="a full dotted module name, or its last parts")
    side = p.add_mutually_exclusive_group()
    side.add_argument("--tests", action="store_true", help="only imports in tests")
    side.add_argument("--code", action="store_true", help="only imports outside tests")
    p.set_defaults(run=cmd_importers)

    p = sub.add_parser("imports", parents=[common], help="what a Python module imports")
    p.add_argument("module", help="a full dotted module name, or its last parts")
    p.set_defaults(run=cmd_imports)

    p = sub.add_parser(
        "classes", parents=[common], help="a Python class's bases and every subclass"
    )
    p.add_argument("name", help="a bare, qualified (Outer.Inner) or full dotted class name")
    p.set_defaults(run=cmd_classes)

    p = sub.add_parser(
        "impact", parents=[common],
        help="the tests that reach a Python name, through its callers and fixtures",
    )
    p.add_argument("name", help="a bare, qualified (Class.method) or full dotted name")
    p.add_argument(
        "--depth", type=int, default=impact.DEFAULT_DEPTH,
        help=f"levels of callers to follow (default {impact.DEFAULT_DEPTH}, "
        f"at most {impact.MAX_DEPTH})",
    )
    p.set_defaults(run=cmd_impact)

    p = sub.add_parser("refresh", parents=[common], help="rebuild cached views")
    p.add_argument(
        "view", nargs="?", choices=[*views.ALL_VIEWS, code_static.VIEW, LANGUAGES],
        help="default: all",
    )
    p.set_defaults(run=cmd_refresh)
    return parser


#: The methods ``callers METHOD PATH`` takes; anything else in that place is a misread command.
HTTP_METHODS = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "CONNECT", "ANY",
     "WEBSOCKET"}
)
_WINDOWS = os.name == "nt"
#: The last line of a text answer cut to its budget (``budget.cut_lines``).
_CUT_NOTICE = re.compile(r"\+\d+ more[:;]")


def _out(text: str, err: bool = False) -> None:
    """Print plain text with terminal controls escaped; ``--json`` output is escaped by json.

    When a cut answer goes to a pipe, its ``+N more`` line is repeated on stderr: ``| grep`` would
    otherwise drop it, and the answer would read as complete."""
    safe = terminal_safe(text)
    print(safe, file=sys.stderr if err else sys.stdout)
    last = safe.rsplit("\n", 1)[-1]
    if not err and _CUT_NOTICE.match(last) and not _isatty(sys.stdout):
        print(f"terrain: output cut: {last}", file=sys.stderr)


def _isatty(stream: Any) -> bool:
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


def _shell_path_note(value: str | None) -> list[str]:
    """A note when ``value`` looks like a path Git Bash rewrote: ``/me`` reaches terrain as
    ``C:/Program Files/Git/me`` in an MSYS shell, and then matches nothing."""
    if not value or not _WINDOWS or not re.match(r"[A-Za-z]:[/\\]", value):
        return []
    return [
        f"note: {field_safe(value)!r} looks like a path the shell rewrote (Git Bash turns '/me' "
        "into 'C:/Program Files/Git/me'); set MSYS_NO_PATHCONV=1 or write '//me'"
    ]


def _fields(value: Any) -> Any:
    """``value`` with every string escaped by ``field_safe``: repository text (names, paths, node
    ids, error lines) is made safe one field at a time, before it is joined into lines."""
    if isinstance(value, str):
        return field_safe(value)
    if isinstance(value, list):
        return [_fields(v) for v in value]
    if isinstance(value, dict):
        return {k: _fields(v) for k, v in value.items()}
    return value


#: The checkout the running command answers from, for the JSON envelope; set by ``main``.
_answer_root = ""


def _emit_json(view: str, built_at: str, rows: Any, **extra: Any) -> None:
    print(
        json.dumps(
            {"view": view, "built_at": built_at, "root": _answer_root, "rows": rows, **extra},
            indent=1,
        )
    )


def _warn_unparsed(data: dict[str, Any], as_json: bool) -> None:
    if data["errors"] and not as_json:
        _note_plain(
            f"warning: {len(data['errors'])} files could not be parsed "
            "(run with --json for details)"
        )


def _fixtures_record(ctx: Context) -> Any:
    record, _ = fixtures_static.build(ctx.cache, ctx.config, force=ctx.args.fresh)
    _warn_unparsed(record.data, ctx.args.json)
    return record


def _deps(fx: dict[str, Any]) -> str:
    return ",".join(fx["argnames"])


def _unlisted_text(trees: list[str]) -> str:
    named = ", ".join(_fields(trees))
    return (
        f"test_paths leaves out {named}, which hold{'s' if len(trees) == 1 else ''} a conftest.py "
        "or test modules: their fixtures are not seen; add them to test_paths"
    )


def _note_unlisted(ctx: Context) -> list[str]:
    """Name the test trees the fixture view does not read (a same-named fixture from another tree
    would otherwise be taken for theirs), on stderr; returns them for ``--json``."""
    trees = fixtures_static.unlisted_test_trees(ctx.config)
    if trees and not ctx.args.json:
        _note(_unlisted_text(trees))
    return trees


def cmd_fixtures(ctx: Context) -> int:
    args = ctx.args
    try:
        scope = fixtures_static.normalise_scope(args.scope) if args.scope else None
    except ValueError as exc:
        raise UsageError(str(exc)) from exc
    record = _fixtures_record(ctx)
    rows = fixtures_static.select(record.data, args.substring, args.autouse, scope)
    unlisted = _note_unlisted(ctx)
    if args.json:
        _emit_json(
            "fixtures", record.built_at, rows, errors=record.data["errors"],
            unlisted_test_paths=unlisted,
        )
        return 0
    if not rows:
        empty = not record.data["fixtures"]
        _out(
            "no fixtures found in test_paths: " + ", ".join(_fields(_path_status(ctx.config)))
            if empty
            else "no fixtures match"
        )
        return 0
    table = render_table(
        ["name", "scope", "autouse", "deps", "file:line", "overrides"],
        [
            [
                fx["name"],
                fx["scope"],
                "yes" if fx["autouse"] else "",
                _deps(fx),
                f"{fx['file']}:{fx['line']}",
                fx["overrides"],
            ]
            for fx in rows
        ],
        args.budget,
        "a name substring, --scope or --autouse",
    )
    _out(table)
    return 0


def cmd_calls(ctx: Context) -> int:
    args = ctx.args
    record, _ = calls_static.build(
        ctx.cache, ctx.config, force=args.fresh, readers=_call_readers()
    )
    _warn_unparsed(record.data, args.json)
    rows = calls_static.select(record.data, args.substring, args.method, args.unresolved)
    if args.json:
        _emit_json("calls", record.built_at, rows, errors=record.data["errors"])
        return 0
    if not rows:
        _out("no calls match" if record.data["calls"] else "no HTTP calls found")
        return 0
    table = render_table(
        ["method", "path", "file:line", "style"],
        [
            [c["method"], c["path"] or f"? {c['raw']}", f"{c['file']}:{c['line']}", c["style"]]
            for c in rows
        ],
        args.budget,
        "a path substring, --method or --unresolved",
    )
    _out(table)
    return 0


def _code_index(ctx: Context) -> tuple[str, code_static.Index]:
    """The code index, brought up to date; (when it last changed, the index)."""
    db = code_static.update(ctx.cache, ctx.config, force=ctx.args.fresh)
    found = code_static.summary(db)
    _warn_unparsed(found, ctx.args.json)
    return found["built_at"], code_static.Index(db)


def _no_definition(index: code_static.Index, name: str) -> int:
    close = index.similar(name)
    hint = f"; similar: {', '.join(_fields(close))}" if close else ""
    _out(f"no definition of {field_safe(name)}{hint}", err=True)
    if index.modules_named(name, 1):
        shown = field_safe(name)
        _note(f"{shown} is a module: terrain importers {shown} lists where it is imported")
    return 1


def _count(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _modules(index: code_static.Index, name: str) -> list[tuple[str, str]]:
    found = index.modules_named(name)
    if not found:
        _out(f"no module named {field_safe(name)}", err=True)
    return found


def _import_rows(rows: list[dict[str, Any]], first: str) -> list[list[str]]:
    return [
        [f"{r[first]}:{r['line']}" if first == "file" else str(r["line"]),
         r["scope"] or "(module)", r["imports"], r["as"]]
        for r in _fields(rows)
    ]


def cmd_importers(ctx: Context) -> int:
    args = ctx.args
    built_at, index = _code_index(ctx)
    modules = _modules(index, args.module)
    if not modules:
        return 1
    rows, cut = [], False
    for module, _ in modules:
        found = index.importers(module)
        cut = cut or len(found) >= code_static.MAX_IMPORTS
        rows += [
            {**r, "module": module} for r in found
            if (not args.tests or r["test"]) and (not args.code or not r["test"])
        ]
    if args.json:
        _emit_json(
            "importers", built_at, rows, modules=[m for m, _ in modules], truncated=cut
        )
        return 0
    files = {r["file"] for r in rows}
    in_tests = len({r["file"] for r in rows if r["test"]})
    lines = [f"module {field_safe(m)}  {field_safe(rel)}" for m, rel in modules]
    lines.append(
        f"{_count(len(rows), 'import')} in {_count(len(files), 'file')} "
        f"({len(files) - in_tests} in code, "
        f"{in_tests} in tests)" + (f"; cut at {code_static.MAX_IMPORTS} a module" if cut else "")
    )
    _out("\n".join(lines))
    if rows:
        _out(render_table(
            ["file:line", "scope", "imports", "as"], _import_rows(rows, "file"), args.budget,
            "--tests, --code or a full module name",
        ))
    return 0


def cmd_imports(ctx: Context) -> int:
    built_at, index = _code_index(ctx)
    modules = _modules(index, ctx.args.module)
    if not modules:
        return 1
    rows = [{**r, "module": m} for m, rel in modules for r in index.imports_of(rel)]
    if ctx.args.json:
        _emit_json("imports", built_at, rows, modules=[m for m, _ in modules])
        return 0
    for module, rel in modules:
        mine = [r for r in rows if r["module"] == module]
        _out(f"module {field_safe(module)}  {field_safe(rel)}: {_count(len(mine), 'import')}")
        if mine:
            _out(render_table(
                ["line", "scope", "imports", "as"], _import_rows(mine, "line"), ctx.args.budget,
                "--json",
            ))
    return 0


def _note_more_matches(
    index: code_static.Index, name: str, found: list[str], kind: str | None = None
) -> None:
    if len(found) >= code_static.MAX_MATCHES or index.find_cut:
        total = index.count_matches(name, kind)
        if total > len(found):
            _note(
                f"{total} definitions match {field_safe(_one_line(name, 200))}; the first "
                f"{len(found)} are used: give a qualified or full dotted name"
            )


def _call_readers() -> list[languages.Language]:
    """The installed language packs that read front-end calls; a refused pack is reported."""
    found, problems = languages.discover()
    for problem in problems:
        _note(f"warning: {field_safe(problem)}")
    return [lang for lang in found if lang.read_calls is not None]


def _languages(ctx: Context) -> list[tuple[languages.Language, dict[str, Any]]]:
    """Each installed language pack with its view, brought up to date; a refused pack is
    reported and left out."""
    found, problems = languages.discover()
    for problem in problems:
        _note(f"warning: {field_safe(problem)}")
    out = []
    for language in (lang for lang in found if lang.read is not None):
        data = languages.build(ctx.cache, language, force=ctx.args.fresh).data
        _warn_unparsed(data, ctx.args.json)
        out.append((language, data))
    return out


def _language_defs(
    ctx: Context, name: str, kind: str | None = None
) -> list[tuple[languages.Language, dict[str, Any], dict[str, Any]]]:
    """The definitions of ``name`` (of ``kind`` if given) in every language pack: (pack, its
    view, definition)."""
    return [
        (language, data, d)
        for language, data in _languages(ctx)
        for d in languages.find(data, name, kind)
    ]


#: Definitions ``terrain def --kind`` lists without a name.
MAX_KIND_ROWS = 1_000


def cmd_def(ctx: Context) -> int:
    name, kind = ctx.args.name, ctx.args.kind
    if name is None and not kind:
        _out("terrain def: give a name, --kind, or both", err=True)
        return 2
    built_at, index = _code_index(ctx)
    if name is None:
        found = index.of_kind(kind, MAX_KIND_ROWS + 1)
        others = [
            (language, data, d)
            for language, data in _languages(ctx)
            for d in data["defs"] if d["kind"] == kind
        ]
    else:
        # the kind filters before the match limit, so other kinds never crowd out the one asked
        found = index.find(name, kind=kind or None)
        others = _language_defs(ctx, name, kind or None)
        if not found and not others and not kind:
            return _no_definition(index, name)
        if found:
            _note_more_matches(index, name, found, kind or None)
    rows = []
    for full in found:
        definition = index.definition(full)
        assert definition is not None
        rel, d = definition
        rows.append({**d, "qualname": d["name"], "name": full, "file": rel})
    rows += [
        {**d, "qualname": d["name"], "sig": "", "language": language.name}
        for language, _, d in others
    ]
    if kind:
        rows = [r for r in rows if r["kind"] == kind]
        if len(rows) > MAX_KIND_ROWS:
            _note(f"more than {MAX_KIND_ROWS} definitions of kind {field_safe(kind)}; the first "
                  f"{MAX_KIND_ROWS} are shown: add a name")
            rows = rows[:MAX_KIND_ROWS]
        if not rows:
            known = sorted(
                {*index.kinds(), *(d["kind"] for _, data in _languages(ctx) for d in data["defs"])}
            )
            what = f" named {field_safe(name)}" if name is not None else ""
            _out(
                f"no definition of kind {field_safe(kind)}{what}; kinds here: "
                f"{', '.join(_fields(known)) or 'none'}",
                err=True,
            )
            return 1
    if ctx.args.json:
        _emit_json("def", built_at, rows)
        return 0
    table = render_table(
        ["kind", "name", "signature", "file:line", "doc"],
        [[r["kind"], r["name"], r["sig"], f"{r['file']}:{r['line']}", r["doc"]] for r in rows],
        ctx.args.budget,
        "a qualified or full dotted name",
    )
    _out(table)
    return 0


def _language_refs(
    ctx: Context, built_at: str,
    found: list[tuple[languages.Language, dict[str, Any], dict[str, Any]]],
) -> int:
    """``terrain refs`` for a name only a language pack defines."""
    targets = sorted({d["name"] for _, _, d in found})
    seen: set[str] = set()
    rows = []
    for language, data, _ in found:
        if language.name not in seen:
            seen.add(language.name)
            rows += [{**r, "language": language.name} for r in languages.references(data, targets)]
    if ctx.args.json:
        _emit_json(
            "refs", built_at, rows, targets=targets, patches=[], uses=[], candidates=[],
            candidate_count=0, counts={"refs": len(rows)}, truncated=False,
        )
        return 0
    lines = [f"{d['kind']} {d['name']}  {d['file']}:{d['line']}" for _, _, d in found]
    lines.append(f"{len(rows)} references")
    lines = _fields(lines)
    if rows:
        lines.append(
            render_table(
                ["file:line", "in", "name"],
                [[f"{r['file']}:{r['line']}", r["scope"] or "-", r["name"]] for r in rows],
                ctx.args.budget, "--json",
            )
        )
    _out("\n".join(lines))
    return 0


_USE_HOW = {
    "arg": "passed to {}", "assign": "assigned to {}", "augment": "combined into {}",
    "member": "in a {} literal",
    "default": "default of {}", "return": "returned", "compare": "compared", "read": "read",
}


def _use_text(use: dict[str, Any]) -> str:
    """How a value use reads the name: ``passed to db.put``, ``assigned to TABLE``."""
    return _USE_HOW.get(use["how"], "{}").format(use["within"] or "(…)")


def _via(row: dict[str, Any], note: str) -> str:
    """``note``, with the variable a followed row was reached through."""
    if "via" not in row:
        return note
    return f"via {row['via']}; {note}" if note else f"via {row['via']}"


def cmd_refs(ctx: Context) -> int:
    args = ctx.args
    built_at, index = _code_index(ctx)
    targets = index.find(args.name)
    if not targets:
        others = _language_defs(ctx, args.name)
        if others:
            return _language_refs(ctx, built_at, others)
        return _no_definition(index, args.name)
    _note_more_matches(index, args.name, targets)
    def keep(row: dict[str, Any]) -> bool:
        return (not args.tests or row["test"]) and (not args.code or not row["test"])

    found = code_static.references(index, targets, keep, follow=True)
    calls, patches, uses = found["calls"], found["patches"], found["uses"]
    candidates, counts = found["candidates"], found["counts"]
    if args.json:
        _emit_json(
            "refs", built_at, calls, targets=targets, patches=patches, uses=uses,
            candidates=candidates if args.by_name else [], candidate_count=counts["candidates"],
            counts=counts, truncated=found["truncated"],
        )
        return 0
    lines = []
    for full in targets:
        definition = index.definition(full)
        assert definition is not None
        rel, d = definition
        lines.append(f"{d['kind']} {full}{d['sig']}  {rel}:{d['line']}")
    in_tests = counts["calls_in_tests"]
    summary = (
        f"{counts['calls']} calls ({counts['calls'] - in_tests} in code, {in_tests} in tests), "
        f"{counts['problems']} with a shape problem, {counts['uses']} used as a value, "
        f"{counts['patches']} patched"
    )
    if counts["unchecked"]:
        summary += f", {counts['unchecked']} not checked for shape (signatures too large)"
    if counts["candidates"] and not args.by_name:
        summary += f", {counts['candidates']} more by name only (--by-name)"
    if found["truncated"]:
        summary += (
            f"; cut at {code_static.MAX_LISTED} sites of a kind, "
            f"{code_static.MAX_LISTED_BYTES} bytes of sites, "
            f"{code_static.MAX_ALIASES} renamed imports or {code_static.MAX_EXAMINED} rows read"
        )
    lines.append(summary)
    lines = _fields(lines)
    rows = [
        [f"{r['file']}:{r['line']}", "test" if r["test"] else "", r["scope"] or "-", r["shape"],
         _via(r, "; ".join(r["problems"]))]
        for r in calls
    ]
    rows += [
        [f"{u['file']}:{u['line']}", "test" if u["test"] else "", u["scope"] or "-",
         _use_text(u), _via(u, "")]
        for u in uses
    ]
    rows += [
        [f"{p['file']}:{p['line']}", "test" if p["test"] else "", p["scope"] or "-",
         f"{p['how'].removesuffix('(obj)')}({p['target']})",
         _via(p, "patched: the real one does not run here")]
        for p in patches
    ]
    if args.by_name:
        rows += [
            [f"{c['file']}:{c['line']}", "test" if c["test"] else "", c["scope"] or "-",
             c["shape"], f"by name only: {c['callee']}"]
            for c in candidates
        ]
    if rows:
        lines.append(
            render_table(
                ["file:line", "", "in", "shape", "note"], rows, args.budget,
                "--tests, --code or --json",
            )
        )
    _out("\n".join(lines))
    return 0


def cmd_classes(ctx: Context) -> int:
    args = ctx.args
    built_at, index = _code_index(ctx)
    found = index.find(args.name, kind="class")
    if not found:
        if index.find(args.name):
            _out(f"{field_safe(args.name)} is not a class: terrain def {field_safe(args.name)} "
                 "says what it is", err=True)
            return 1
        return _no_definition(index, args.name)
    _note_more_matches(index, args.name, found, "class")
    trees = [impact.class_tree(index, full) for full in found]
    if args.json:
        _emit_json("classes", built_at, trees)
        return 0
    for n, tree in enumerate(_fields(trees)):
        if n:
            _out("")
        bases = [
            f"{b['name']} ({b['file']}:{b['line']})" if b["file"] else f"{b['name']} (outside)"
            for b in tree["bases"]
        ]
        subs = tree["subclasses"]
        _out("\n".join([
            f"class {tree['name']}  {tree['file']}:{tree['line']}",
            f"bases: {', '.join(bases) or '-'}",
            f"mro: {' > '.join(tree['mro'])}",
            f"subclasses: {len(subs)}"
            + (f" (cut at {impact.MAX_SUBCLASSES})" if tree["truncated"] else ""),
        ]))
        if subs:
            _out(render_table(
                ["depth", "class", "file:line", "via"],
                [[str(r["depth"]), r["name"], f"{r['file']}:{r['line']}", r["base"]]
                 for r in subs],
                args.budget, "--json or a full dotted name",
            ))
    return 0


def cmd_impact(ctx: Context) -> int:
    args = ctx.args
    built_at, index = _code_index(ctx)
    targets = index.find(args.name)
    if not targets:
        return _no_definition(index, args.name)
    _note_more_matches(index, args.name, targets)
    fixtures = fixtures_static.build(ctx.cache, ctx.config, force=args.fresh)[0].data
    found = impact.impact(index, fixtures, targets, args.depth)
    tests, patched, counts = found["tests"], found["patched"], found["counts"]
    if args.json:
        _emit_json(
            "impact", built_at, tests, targets=targets, patched=patched, counts=counts,
            depth=found["depth"], truncated=found["truncated"],
        )
        return 0
    direct = sum(1 for t in tests if t["depth"] == 1)
    summary = (
        f"{len(tests)} {'test reaches' if len(tests) == 1 else 'tests reach'} "
        f"{', '.join(targets)} ({direct} directly), through "
        f"{counts['functions']} callers within {found['depth']} levels"
    )
    if patched:
        summary += (
            f"; {len(patched)} {'test patches' if len(patched) == 1 else 'tests patch'} it"
            " (the real one does not run there)"
        )
    unfollowed = [
        f"{counts['beyond_depth']} callers deeper than --depth" if counts["beyond_depth"] else "",
        f"{counts['module_level']} calls at module level" if counts["module_level"] else "",
        f"{counts['values']} places using it as a value" if counts["values"] else "",
        f"{counts['by_name']} calls by name only" if counts["by_name"] else "",
    ]
    if any(unfollowed):
        summary += "; not followed: " + ", ".join(u for u in unfollowed if u)
    if found["truncated"]:
        summary += (
            f"; cut at {impact.MAX_FOLLOWED} callers, {impact.MAX_FIXTURES} fixtures or "
            f"{code_static.MAX_EXAMINED} rows read"
        )
    lines = _fields([summary])
    rows = [
        [t["test"], str(t["depth"]), t["how"], " <- ".join(t["via"])] for t in tests
    ]
    rows += [[p["test"], "-", f"patches it at {p['file']}:{p['line']}", p["target"]]
             for p in patched]
    if rows:
        lines.append(
            render_table(
                ["test", "depth", "how", "via"], rows, args.budget, "--depth or --json",
            )
        )
    _out("\n".join(lines))
    return 0


def _definition_lines(fx: dict[str, Any]) -> list[str]:
    params = {0: "none", -1: "yes"}.get(fx["params"], f"{fx['params']} values")
    lines = [
        f"name: {fx['name']}",
        f"scope: {fx['scope']}",
        f"autouse: {'yes' if fx['autouse'] else 'no'}",
        f"params: {params}",
        f"deps: {_deps(fx) or '-'}",
        f"defined: {fx['file']}:{fx['line']}",
        f"overrides: {fx['overrides'] or '-'}",
    ]
    if fx["func"] != fx["name"]:
        lines.append(f"function: {fx['func']}")
    if fx["cls"]:
        lines.append(f"class: {fx['cls']}")
    if fx["dyn"]:
        lines.append(f"getfixturevalue: {','.join(fx['dyn'])}")
    return lines


def cmd_fixture(ctx: Context) -> int:
    record = _fixtures_record(ctx)
    found = [fx for fx in record.data["fixtures"] if fx["name"] == ctx.args.name]
    unlisted = _note_unlisted(ctx)
    if not found:
        names = {fx["name"] for fx in record.data["fixtures"]}
        close = sorted(n for n in names if ctx.args.name in n)
        hint = f"; similar: {', '.join(_fields(close[:10]))}" if close else ""
        _out(f"no fixture named {field_safe(ctx.args.name)}{hint}", err=True)
        if unlisted and ctx.args.json:
            # No JSON follows a miss, and the fixture may be in a tree that is not read.
            _note(_unlisted_text(unlisted))
        return 1
    # The tests pytest collects (inherited test methods too) and autouse reach come from the code
    # index, the way `impact` sees them. With several definitions, each dependent also names the
    # one it gets: the conftest to edit for it.
    deps = impact.fixture_dependents(
        _code_index(ctx)[1], record.data, ctx.args.name, resolve=len(found) > 1
    )
    if ctx.args.json:
        _emit_json(
            "fixture", record.built_at, found, dependents=deps, unlisted_test_paths=unlisted
        )
        return 0
    found, deps = _fields(found), _fields(deps)
    lines: list[str] = []
    for fx in found:
        lines.extend(_definition_lines(fx))
        lines.append("")
    lines.append(f"dependents ({len(deps)}):")
    keep = len(lines)
    lines.extend(
        f"{d['kind']}  {d['name']}  {d['file']}:{d['line']}  {d['via']}"
        + (f"  inherited from {d['inherited']}" if "inherited" in d else "")
        + (f"  gets {d['gets'] or '-'}" if "gets" in d else "")
        for d in deps
    )
    _out(cut_lines(lines, ctx.args.budget, "--budget N or --json", keep=keep))
    return 0


def _view_ages(ctx: Context) -> list[str]:
    lines = ["view      built                  age   stale-files"]
    for name in views.ALL_VIEWS:
        record = ctx.cache.read_view(name)
        if record is None:
            lines.append(f"{name:<9} never built")
            continue
        stale = views.stale_files(ctx.cache, ctx.config, record)
        line = f"{name:<9} {record.built_at}  {age_text(record.built_at):<5} {stale}"
        if _key_changed(ctx, name, record):
            line += "  needs refresh (config or environment changed)"
        lines.append(line)
    return lines


def _key_changed(ctx: Context, view: str, record: Any) -> bool:
    """A slow view built under another config, interpreter or version; doctor never builds."""
    if view not in views.SLOW_VIEWS:
        return False
    return record.key != views.view_key(ctx.cache, ctx.config, ctx.python, view)


def _path_status(config: Config) -> list[str]:
    return [p if (config.root / p).exists() else f"{p} (missing)" for p in config.test_paths]


def _one_line(text: str, limit: int = 200) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def _app_status(ctx: Context) -> list[str]:
    """One line per configured app from the cached route walk; doctor itself imports nothing."""
    record = ctx.cache.read_view(routes_view.VIEW)
    doc = record.data if record is not None else None
    changed = record is not None and _key_changed(ctx, routes_view.VIEW, record)
    if doc is not None:
        try:
            protocol.check(doc, protocol.ROUTES_SCHEMA)
        except protocol.ProtocolError:
            doc = None
    if changed:
        doc = None
    lines = []
    for app in ctx.config.apps:
        if changed:
            lines.append(f"  {app}: needs refresh (config or environment changed)")
            continue
        if doc is None:
            lines.append(f"  {app}: not imported yet (run: terrain refresh routes)")
            continue
        failure = next((e for e in doc["errors"] if e["app"] == app), None)
        if failure is not None:
            cause = (failure.get("chain") or failure["error"].splitlines())[-1]
            lines.append(f"  {app}: import failed: {_one_line(cause)}")
        else:
            count = sum(1 for r in doc["routes"] if r["app"] == app)
            lines.append(f"  {app}: ok, {count} routes")
    if doc is not None:
        lines.extend(f"  {n}" for n in doc.get("notes", []) if str(n).startswith("warning:"))
    return lines


def _python_hint(ctx: Context) -> list[str]:
    """Point at the target's venv when the import tier would run in codeterrain's interpreter."""
    venv = ctx.config.root / ".venv"
    own = os.path.realpath(ctx.python) == os.path.realpath(sys.executable)
    if not own or not venv.is_dir():
        return []
    exe = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return [
        "hint: the import tier runs in codeterrain's own interpreter but the target has a .venv; "
        "that interpreter belongs to the target repository, so if it is a venv you created, "
        f"pass --python {exe.as_posix()}"
    ]


def _checkouts_status(root: Path) -> list[str]:
    """``doctor``'s line on the other worktrees of the repository, when there are any."""
    found = checkouts(root)
    if found is None or not found.others:
        return []
    listed = ", ".join(field_safe(p.as_posix()) for p in found.others[:10])
    more = f" and {len(found.others) - 10} more" if len(found.others) > 10 else ""
    kind = "a worktree" if found.linked else "the main checkout"
    return [f"checkout: {kind}; other checkouts: {listed}{more}"]


def cmd_doctor(ctx: Context) -> int:
    config = ctx.config
    lines = [
        f"codeterrain {__version__}",
        f"root: {config.root.as_posix()}",
        *_checkouts_status(config.root),
        f"config: {config.source}",
        f"user config: {config.user_config or '-'}",
        f"apps: {len(config.apps) or '-'}",
        "test_paths: " + ", ".join(_path_status(config)),
        *(
            [f"warning: {_unlisted_text(trees)}"]
            if (trees := fixtures_static.unlisted_test_trees(config)) else []
        ),
        f"region: {config.region}",
        f"import_env: {', '.join(sorted(config.import_env)) or '-'}",
        f"pass_env: {', '.join(config.pass_env) or '-'}",
        f"respect_addopts: {'yes' if config.respect_addopts else 'no'}",
        f"route_manifest: {config.route_manifest.as_posix() if config.route_manifest else '-'}",
        f"plugins: {', '.join(config.plugins) or '-'}",
        *_pack_status(),
        f"cache dir: {ctx.cache.dir.as_posix()}",
        f"target python: {ctx.python}",
    ]
    try:
        found = probe_versions(ctx.python, config, ctx.cache.dir)
        shown = ", ".join(f"{k} {found.get(k) or 'not installed'}" for k in found)
        lines.append(f"versions: {shown}")
    except SandboxError as exc:
        lines.append(f"versions: unavailable ({_one_line(str(exc))})")
    lines.extend(_python_hint(ctx))
    lines[5:5] = _app_status(ctx)
    lines.extend(_view_ages(ctx))
    # every entry is one line that may carry repository text (apps, paths, import errors)
    _out("\n".join(field_safe(line) for line in lines))
    return 0


def _pack_status() -> list[str]:
    found, problems = languages.discover()
    shown = ", ".join(f"{lang.name} {lang.version}" for lang in found)
    return [f"language packs: {shown or '-'}", *(f"  refused: {p}" for p in problems)]


def _note_plain(message: str) -> None:
    _out(message, err=True)


#: Every error type codeterrain raises for a run that cannot be completed.
FAILURES = (
    code_static.CorruptIndex,
    sqlite3.Error,
    fixtures_order.ExtractError,
    protocol.ProtocolError,
    ManifestError,
    SandboxError,
    CacheBusy,
)


def _note(message: str) -> None:
    _out(f"terrain: {message}", err=True)


def _slow(ctx: Context, view: str) -> views.Served:
    cache_dir, python, config = ctx.cache.dir, ctx.python, ctx.config
    if view == fixtures_order.VIEW:
        schema = protocol.ORDER_SCHEMA

        def build() -> dict[str, Any]:
            return fixtures_order.extract(config, cache_dir, python)

    else:
        schema = protocol.ROUTES_SCHEMA

        def build() -> dict[str, Any]:
            return routes_view.extract(config, cache_dir, python)

    return views.serve_slow(ctx.cache, config, view, schema, build, python, ctx.args.fresh, _note)


def _stamp(served: views.Served) -> list[str]:
    if not served.stale:
        return []
    return [f"stale: {served.stale} files changed since {served.record.built_at}"]


def cmd_setup_order(ctx: Context) -> int:
    served = _slow(ctx, fixtures_order.VIEW)
    doc = served.record.data
    matched = fixtures_order.match_nodes(doc, ctx.args.nodeid)
    if not matched:
        _out(f"no collected test matches {field_safe(ctx.args.nodeid)}", err=True)
        if doc["errors"]:
            _out(
                f"note: {doc['errors']} collection errors in the extraction run; "
                "the test may be in a file that failed to import",
                err=True,
            )
        return 1
    found = fixtures_order.groups(doc, matched)
    if ctx.args.json:
        _emit_json(
            "order",
            served.record.built_at,
            found,
            stale_files=served.stale,
            collection_errors=doc["errors"],
        )
        return 0
    lines: list[str] = []
    for group in _fields(found):
        more = len(group["nodes"]) - 1
        lines.append(group["nodes"][0] + (f" (+{more} more with this setup)" if more else ""))
        for fx in group["fixtures"]:
            param = " [params]" if fx.get("params") else ""
            letter = fixtures_order.scope_letter(fx["scope"])
            lines.append(f"{letter} {fx['name']}{param}  {fx['file']}:{fx['line']}")
    if doc["errors"]:
        lines.append(f"note: {doc['errors']} collection errors in the extraction run")
    body = cut_lines(lines, ctx.args.budget, "a more specific node id")
    _out("\n".join([*_stamp(served), body]))
    return 0


def _warn_failed_apps(ctx: Context, doc: dict[str, Any]) -> None:
    missing = False
    for failure in doc["errors"]:
        last = (failure.get("chain") or failure["error"].strip().splitlines())[-1]
        app = field_safe(failure["app"])
        _note(f"app {app} failed to import: {field_safe(last)} (--json has the traceback)")
        missing = missing or "ModuleNotFoundError" in last
    own = os.path.realpath(ctx.python) == os.path.realpath(sys.executable)
    if missing and own:
        _note_plain(
            "hint: the apps were imported with codeterrain's own interpreter, which lacks the "
            f"project's packages; pass --python or set {PYTHON_ENV} to the project's interpreter"
        )
        for line in _python_hint(ctx):
            _note_plain(line)


def _joined(ctx: Context) -> tuple[views.Served, Any]:
    served = _slow(ctx, routes_view.VIEW)
    _warn_failed_apps(ctx, served.record.data)
    return served, routes_view.joined(served.record.data, ctx.config)


#: Names looked up when a route text matches no route (``Index.similar`` stops at 200).
MAX_NAMES_READ = 200


def routes_reading(
    index: code_static.Index, text: str, routes: list[dict[str, Any]]
) -> tuple[list[tuple[dict[str, Any], str]], bool]:
    """The routes whose handler reads or calls a name whose last part contains ``text``
    (``legal`` finds the handler that reads ``LEGAL_PRIVACY_VERSION``), each with the name; the
    handler's own body only, not the helpers it calls. The flag says the search stopped early:
    more names than ``MAX_NAMES_READ``, or more sites than one ``references`` budget reads."""
    handlers: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for route in routes:
        qualname = _handler_qualname(route, index.src_package)
        if qualname:
            handlers.setdefault((route["file"], qualname), []).append(route)
            # a callable instance is named by its class; its body is `__call__`
            handlers.setdefault((route["file"], f"{qualname}.__call__"), []).append(route)
    found: list[tuple[dict[str, Any], str]] = []
    seen: set[tuple[int, str]] = set()
    names = index.similar(text, limit=MAX_NAMES_READ + 1)
    truncated = len(names) > MAX_NAMES_READ
    budget = [code_static.MAX_EXAMINED]  # shared by every name
    for full in names[:MAX_NAMES_READ]:
        refs = code_static.references(index, [full], budget=budget)
        truncated = truncated or refs["truncated"] or budget[0] <= 0
        for site in [*refs["calls"], *refs["uses"]]:
            for route in handlers.get((site["file"], _body_scope(site["scope"])), []):
                if (id(route), full) not in seen:
                    seen.add((id(route), full))
                    found.append((route, full))
    return found, truncated


#: Scopes the code index gives code that runs in the body of the function around it.
_BODY_MARKERS = (".<listcomp>", ".<setcomp>", ".<dictcomp>", ".<genexpr>")


def _body_scope(scope: str) -> str:
    """The function whose body holds code in ``scope``: a comprehension or generator
    expression is part of it (``get_versions.<listcomp>`` is ``get_versions``); a lambda or a
    nested function is a function of its own."""
    while scope.endswith(_BODY_MARKERS):
        scope = scope.rsplit(".", 1)[0]
    return scope


def _handler_qualname(route: dict[str, Any], src_package: bool) -> str:
    """The handler's qualified name within its file (``Controller.version``). Its module is
    named as it was imported, which in a nested project is a tail of the name from the
    repository root (``routes`` for ``apps/web/routes.py``), so the longest such tail that
    prefixes the handler is the module; "" when none does."""
    parts = code_static.module_name(route["file"], src_package).split(".")
    for start in range(len(parts)):
        module = ".".join(parts[start:])
        if route["handler"].startswith(f"{module}."):
            # `create_app.<locals>.version` at run time is `create_app.version` in the index
            return route["handler"][len(module) + 1:].replace(".<locals>.", ".")
    return ""


def _reads_by_route(
    found: list[tuple[dict[str, Any], str]],
) -> list[tuple[dict[str, Any], list[str]]]:
    """One entry per route as listed (the same route served by two apps is one), with the full
    names its handler reads."""
    reads: dict[tuple[str, ...], tuple[dict[str, Any], list[str]]] = {}
    for route, name in found:
        key = (route["method"], route["path"], route["handler"], route["file"], str(route["line"]))
        names = reads.setdefault(key, (route, []))[1]
        if name not in names:
            names.append(name)
    return list(reads.values())


def _routes_reading_table(
    text: str, found: list[tuple[dict[str, Any], str]], truncated: bool, budget: int
) -> list[str]:
    shown = field_safe(_one_line(text, 200))
    partial = (
        [f"the search stopped early (more than {MAX_NAMES_READ} names or too many uses): "
         "give a longer name substring"]
        if truncated else []
    )
    if not found:
        return [f"no route handler reads a name containing {shown}", *partial]
    return [
        f"routes whose handler reads a name containing {shown}:",
        render_table(
            ["method", "path", "handler", "reads", "file:line"],
            [[r["method"], r["path"], r["handler"],
              ", ".join(name.rsplit(".", 1)[-1] for name in names), f"{r['file']}:{r['line']}"]
             for r, names in _reads_by_route(found)],
            budget,
            "a longer name substring",
        ),
        *partial,
    ]


def cmd_routes(ctx: Context) -> int:
    args = ctx.args
    served, result = _joined(ctx)
    rows = routes_view.select(result.rows, args.substring, args.app, args.access)
    # No path or handler holds the text: the routes whose handler reads a name holding it.
    reading: tuple[list[tuple[dict[str, Any], str]], bool] | None = None
    if not rows and args.substring and not _shell_path_note(args.substring):
        _, index = _code_index(ctx)
        filtered = routes_view.select(result.rows, "", args.app, args.access)
        reading = routes_reading(index, args.substring, filtered)
    if args.json:
        extra: dict[str, Any] = {}
        if reading is not None:
            extra["handlers_reading"] = [
                {**route, "reads": names} for route, names in _reads_by_route(reading[0])
            ]
            extra["handlers_reading_truncated"] = reading[1]
        _emit_json(
            "routes",
            served.record.built_at,
            rows,
            stale_files=served.stale,
            errors=served.record.data["errors"],
            routes_without_manifest_row=len(result.routes_without_row),
            manifest_rows_without_route=len(result.rows_without_route),
            **extra,
        )
        return 0
    if not rows:
        lines = [*_stamp(served), "no routes match", *_shell_path_note(args.substring)]
        if reading is not None:
            lines += _routes_reading_table(args.substring, *reading, args.budget)
        _out("\n".join(lines))
        return 0

    def omitted(shown: int) -> str:
        counts = collections.Counter(r["app"] for r in rows[shown:])
        return ", ".join(f"{field_safe(app)} {n}" for app, n in counts.most_common())

    table = render_table(
        ["method", "path", "app", "access", "handler", "file:line"],
        [
            [
                r["method"],
                r["path"],
                r["app"],
                r["access"] or "",
                r["handler"],
                f"{r['file']}:{r['line']}",
            ]
            for r in rows
        ],
        args.budget,
        "a path/handler substring, --app or --access (--budget 0 for every row)",
        omitted=omitted,
    )
    _out("\n".join([*_stamp(served), table]))
    return 0


def cmd_callers(ctx: Context) -> int:
    args = ctx.args
    if (args.method is None) != (args.path is None):
        if args.method is not None and args.method.upper() not in HTTP_METHODS:
            # The callers of a Python name are `refs`'s answer: give it rather than an error.
            _note(
                "callers is for routes (METHOD PATH); answering as "
                f"terrain refs {field_safe(args.method)}"
            )
            args.name, args.tests, args.code, args.by_name = args.method, False, False, False
            return cmd_refs(ctx)
        raise UsageError("callers takes METHOD and PATH together, or neither")
    if not ctx.config.client_repos:
        raise UsageError(
            "no client_repos configured; list the front-end checkouts in --config or the user "
            "config"
        )
    served = _slow(ctx, routes_view.VIEW)
    _warn_failed_apps(ctx, served.record.data)
    routes = served.record.data["routes"]
    clients, backend_state = calls_join.load_with_states(
        ctx.config.client_repos, ctx.config.root, args.cache_dir, args.fresh, _call_readers()
    )
    for client in clients:
        if client.error:
            _note(f"{client.name}: skipped ({_one_line(client.error)})")
    for name, state in [(ctx.config.root.name, backend_state)] + [
        (c.name, c.state) for c in clients
    ]:
        warning = calls_join.state_warning(name, state)
        if warning and not args.json:
            _note_plain(f"warning: {warning}")
    for client in clients:
        if client.errors and not args.json:
            _note_plain(
                f"warning: {field_safe(client.name)}: {len(client.errors)} files could not be "
                "parsed and their calls are missing (run with --json for details)"
            )
    every = calls_join.join(clients, routes)
    rows = every
    if args.method is not None:
        key = route_key(args.method, args.path)
        targets = [r for r in routes if route_key(r["method"], r["path"]) == key]
        if not targets:
            _out(f"no route {field_safe(key[0])} {field_safe(args.path)}", err=True)
            for line in _shell_path_note(args.path):
                _out(line, err=True)
            return 1
        rows = [
            r for r in rows
            if r["route"] and route_key(r["route"]["method"], r["route"]["path"]) == key
        ]
    if args.unmatched:
        rows = [r for r in rows if r["resolved"] and r["route"] is None]
    if args.json:
        _emit_json(
            "callers", served.record.built_at, rows, stale_files=served.stale,
            backend_state=backend_state,
            clients=[
                {
                    "repo": c.name, "root": c.root.as_posix(), "error": c.error,
                    "state": c.state, "errors": c.errors,
                }
                for c in clients
            ],
        )
        return 0
    matched = sum(1 for r in every if r["route"])
    unresolved = sum(1 for r in every if not r["resolved"])
    summary = (
        f"{len(every)} calls in {len(clients)} front ends: {matched} hit a route, "
        f"{len(every) - matched - unresolved} hit none, {unresolved} have no static path"
    )
    if not rows:
        _out("\n".join([*_stamp(served), summary, "no calls match"]))
        return 0
    table = render_table(
        ["repo", "method", "call path", "route", "file:line"],
        [
            [
                r["repo"],
                r["method"],
                r["path"] or f"? {r['raw']}",
                f"{r['route']['method']} {r['route']['path']}" if r["route"] else "-",
                f"{r['file']}:{r['line']}",
            ]
            for r in rows
        ],
        args.budget,
        "METHOD PATH or --unmatched",
    )
    _out("\n".join([*_stamp(served), summary, table]))
    return 0


def cmd_route(ctx: Context) -> int:
    served, result = _joined(ctx)
    method, path = ctx.args.method.upper(), ctx.args.path
    rows = [r for r in result.rows if r["method"] == method and r["path"] == path]
    if not rows:
        close = sorted({f"{r['method']} {r['path']}" for r in result.rows if path in r["path"]})
        hint = f"; similar: {', '.join(_fields(close[:8]))}" if close else ""
        _out(f"no route {field_safe(method)} {field_safe(path)}{hint}", err=True)
        return 1
    if ctx.args.json:
        _emit_json("route", served.record.built_at, rows, stale_files=served.stale)
        return 0
    lines = _stamp(served)
    for r in _fields(rows):
        lines += [
            f"route: {r['method']} {r['path']}",
            f"app: {r['app']}",
            f"access: {r['access'] or '-'}",
            f"handler: {r['handler']}",
            f"defined: {r['file']}:{r['line']}",
            f"dependencies: {', '.join(r['dependencies']) or '-'}",
            f"response_model: {r['response_model'] or '-'}",
            "",
        ]
    _out(cut_lines(lines, ctx.args.budget, "--json").rstrip())
    return 0


def cmd_refresh(ctx: Context) -> int:
    wanted = (
        [ctx.args.view] if ctx.args.view
        else [*views.ALL_VIEWS, code_static.VIEW, LANGUAGES]
    )
    ctx.args.fresh = True
    failed = 0
    for name in wanted:
        started = time.perf_counter()
        try:
            summary = _refresh_one(ctx, name)
        except FAILURES as exc:
            _note(f"{name} failed: {_one_line(str(exc), 400)}")
            failed += 1
            continue
        if summary is not None:
            _out(f"{name}: {summary} in {time.perf_counter() - started:.2f}s")
    return 1 if failed else 0


def _refresh_one(ctx: Context, name: str) -> str | None:
    if name == LANGUAGES:
        packs = _languages(ctx)
        if not packs:
            return "no language packs installed"
        return ", ".join(
            f"{lang.name} {len(data['defs'])} definitions, {len(data['refs'])} references"
            for lang, data in packs
        )
    if name == code_static.VIEW:
        found = code_static.summary(code_static.update(ctx.cache, ctx.config, force=True))
        _warn_unparsed(found, False)
        return f"{found['files']} files, {found['defs']} definitions"
    if name == "fixtures":
        data = fixtures_static.build(ctx.cache, ctx.config, force=True)[0].data
        _warn_unparsed(data, False)
        return f"{len(data['fixtures'])} fixtures, {len(data['tests'])} tests"
    if name == calls_static.VIEW:
        data = calls_static.build(ctx.cache, ctx.config, force=True, readers=_call_readers())[
            0
        ].data
        _warn_unparsed(data, False)
        resolved = sum(1 for c in data["calls"] if c["resolved"])
        return f"{len(data['calls'])} calls, {resolved} with a static path"
    if name == fixtures_order.VIEW:
        doc = _slow(ctx, name).record.data
        return f"{len(doc['nodes'])} tests, {len(doc['closures'])} distinct setups"
    if not ctx.config.apps:
        _out("routes: skipped (no apps configured)")
        return None
    doc = _slow(ctx, name).record.data
    _warn_failed_apps(ctx, doc)
    return f"{len(doc['routes'])} routes from {len(ctx.config.apps)} apps"


def _checkout_text(root: Path) -> str:
    """Which checkout ``root`` is, when its repository has others, else ""."""
    found = checkouts(root)
    if found is None or not found.others:
        return ""
    n = len(found.others)
    return (
        f"answered from {field_safe(found.top.as_posix())} "
        f"({'a worktree' if found.linked else 'the main checkout'}); the repository has {n} "
        f"other checkout{'' if n == 1 else 's'}: run terrain from the one you are editing"
    )


def _note_checkout(root: Path, explicit: bool) -> None:
    """Name the checkout an answer came from when the repository has others: an agent editing one
    worktree that asks from another gets an answer that misses its changes, and this says why.
    With ``--root``, also say when the current directory is a different checkout of it."""
    text = _checkout_text(root)
    if not text:
        return
    _note(text)
    if explicit:
        here, there = checkouts(Path(os.path.realpath(os.getcwd()))), checkouts(root)
        if here is not None and there is not None and here.top in there.others:
            _note(
                f"warning: --root is {field_safe(there.top.as_posix())}, but the current "
                f"directory is in another checkout of it, {field_safe(here.top.as_posix())}"
            )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    try:
        root = resolve_root(args.root)
        config = load_config(root, args.config)
        distrust([root, *(repo.root for repo in config.client_repos)])
        if config.ignored:
            _note(
                f"warning: ignored {', '.join(config.ignored)} from the repository's "
                f"{config.source.split()[0]}; set them with --config or in "
                f"{user_config_path().as_posix()}"
            )
        ctx = Context(config, Cache(root, args.cache_dir), args)
        global _answer_root
        _answer_root = root.as_posix()
        status = args.run(ctx)
        if args.run is not cmd_doctor:
            _note_checkout(root, args.root is not None)
        return status
    except (ConfigError, UsageError) as exc:
        # The message can quote repository text (a TOML key, a path): one field, one line.
        _out(f"terrain: {field_safe(str(exc))}", err=True)
        return 2
    except FAILURES as exc:
        _out(f"terrain: {field_safe(_one_line(str(exc), 400))}", err=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
