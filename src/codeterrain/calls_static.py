"""Static HTTP call sites in JavaScript and TypeScript: a tokenizer in Python, nothing is run.

Per file the pass records every call that reaches an HTTP path: ``<receiver>.<verb>(path, ...)``,
``fetch(path, {method})`` and object literals with a ``url:`` key (``useCustom({url, method})``,
axios configs). A path written as a string or template literal, a ``+`` concatenation of those, or a
same-file ``const`` of one is resolved; ``${expr}`` becomes ``{}``. A call whose path cannot be read
statically is recorded as unresolved, so coverage stays visible. Facts are keyed by content hash in
the cache, like the fixture index, so an edit re-reads one file.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from codeterrain.cache import Cache, ViewRecord
from codeterrain.config import Config

if TYPE_CHECKING:
    from codeterrain.languages import Language

VIEW = "calls"
#: Bump the number whenever ``parse_source`` would produce different facts for the same text: cached
#: facts are keyed by content hash only, so a parser change would otherwise keep serving old facts.
OBJECT_KIND = "calls-16"
SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
VERBS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})
#: Receivers whose ``.get(x)`` is an HTTP call even when ``x`` cannot be read statically. Any other
#: receiver counts only when its first argument reads as a path, which keeps ``map.get(key)`` out.
CLIENT_NAMES = frozenset(
    {"api", "apiClient", "axios", "axiosInstance", "client", "http", "httpClient", "request"}
)
CALL_FACT_TYPES = {
    "method": str,
    "path": str,
    "line": "int",
    "style": str,
    "resolved": bool,
    "raw": str,
}
#: Tests, mocks (``__mocks__``, msw's ``mocks/``) and build output (a Next.js export in ``out/``,
#: coverage reports). The cache walk already skips ``build``, ``dist`` and ``node_modules``.
_SKIP_DIRS = frozenset(
    {"__tests__", "__mocks__", "__fixtures__", "mocks", "test", "tests", "e2e", "out", "coverage",
     "storybook-static"}
)
_SKIP_NAME = re.compile(r"\.(test|spec|stories|d|min)\.[cm]?[jt]sx?$")
# A `/` after one of these starts a regular expression literal, not a division.
_REGEX_AFTER_PUNCT = frozenset("(,=:[!&|?{};+-*%<>~^")
_REGEX_AFTER_WORD = frozenset(
    {"return", "typeof", "case", "do", "else", "in", "of", "new", "delete", "void", "throw",
     "yield", "await"}
)
_RAW_LIMIT = 120
GLOBALS = frozenset({"window", "globalThis", "self"})


@dataclass
class Tok:
    kind: str  # "id", "punct", "str", "tpl", "num", "regex"
    value: Any  # str for most kinds; for "tpl" a list of ("text", s) / ("expr", source)
    line: int
    #: For "tpl": per part, the source lines between the template's start and where the part's
    #: text begins (an expression's first character), counted in the source, not the value.
    lines: tuple[int, ...] = ()


def is_skipped_client_file(rel: str) -> bool:
    path = PurePosixPath(rel)
    return bool(_SKIP_NAME.search(path.name)) or any(p in _SKIP_DIRS for p in path.parts[:-1])


# --- tokenizer ---------------------------------------------------------------------------------


_SIMPLE_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
_HEX = re.compile(r"x([0-9A-Fa-f]{2})|u([0-9A-Fa-f]{4})|u\{([0-9A-Fa-f]{1,6})\}")


def _escape(src: str, i: int) -> tuple[str, int]:
    """The text the escape at ``src[i] == '\\'`` stands for and the index after it: ``\\n`` and
    the like, ``\\xHH``, ``\\uHHHH``, ``\\u{H...}``, a line continuation (nothing), any other
    character as itself. A malformed hex escape keeps its letters, so the path it spells cannot
    match a route by accident."""
    c = src[i + 1]
    if c == "\n":
        return "", i + 2
    if c in "xu":
        hexed = _HEX.match(src, i + 1)
        if hexed:
            code = int(next(g for g in hexed.groups() if g is not None), 16)
            if code <= 0x10FFFF:
                return chr(code), hexed.end()
    return _SIMPLE_ESCAPES.get(c, c), i + 2


def _skip_string(src: str, i: int, quote: str) -> tuple[str, int]:
    """The value of the string starting at ``src[i] == quote``, and the index after it. An
    unterminated string ends at the newline (JSX text such as ``Don't`` must not eat the file)."""
    out: list[str] = []
    i += 1
    n = len(src)
    while i < n:
        c = src[i]
        if c == "\\" and i + 1 < n:
            value, i = _escape(src, i)
            out.append(value)
            continue
        if c == quote:
            return "".join(out), i + 1
        if c == "\n":
            return "".join(out), i
        out.append(c)
        i += 1
    return "".join(out), i


def _skip_template(src: str, i: int) -> tuple[list[tuple[str, str]], int, tuple[int, ...]]:
    """The parts of the template literal starting at ``src[i] == '`'``, the index after it, and
    each part's line offset from the template's first line (``Tok.lines``)."""
    parts: list[tuple[str, str]] = []
    lines: list[int] = []
    text: list[str] = []
    seen, newlines = i, 0
    text_start = i + 1

    def offset(at: int) -> int:
        nonlocal seen, newlines
        newlines += src.count("\n", seen, at)
        seen = at
        return newlines

    i += 1
    n = len(src)
    while i < n:
        c = src[i]
        if c == "\\" and i + 1 < n:
            value, i = _escape(src, i)
            text.append(value)
            continue
        if c == "`":
            if text:
                parts.append(("text", "".join(text)))
                lines.append(offset(text_start))
            return parts, i + 1, tuple(lines)
        if c == "$" and i + 1 < n and src[i + 1] == "{":
            if text:
                parts.append(("text", "".join(text)))
                lines.append(offset(text_start))
                text = []
            end = _skip_braces(src, i + 2)
            raw = src[i + 2 : end - 1]
            parts.append(("expr", raw.strip()))
            lines.append(offset(i + 2 + len(raw) - len(raw.lstrip())))
            i = text_start = end
            continue
        text.append(c)
        i += 1
    if text:
        parts.append(("text", "".join(text)))
        lines.append(offset(text_start))
    return parts, i, tuple(lines)


#: Characters after which a `/` inside ``${...}`` starts a regex literal, not a division.
_REGEX_BEFORE = "({[,;:=!&|?+-*%<>~^"


def _skip_braces(src: str, i: int) -> int:
    """The index after the ``}`` closing an expression that starts at ``i`` (inside ``${``).
    Strings, templates, comments and regex literals (``/}/``) are stepped over whole, so a brace
    inside one does not count."""
    depth = 1
    n = len(src)
    last = "{"  # the last character that is not space: a `/` after an operator opens a regex
    while i < n:
        c = src[i]
        if c in "'\"":
            _, i = _skip_string(src, i, c)
            last = c
            continue
        if c == "`":
            _, i, _ = _skip_template(src, i)
            last = c
            continue
        if c == "/" and i + 1 < n and src[i + 1] in "/*":
            i = _skip_comment(src, i)
            continue
        if c == "/" and last in _REGEX_BEFORE:
            end = _regex_end(src, i)
            if end is not None:
                i, last = end, "/"
                continue
        if not c.isspace():
            last = c
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return i


def _skip_comment(src: str, i: int) -> int:
    if src[i + 1] == "/":
        end = src.find("\n", i)
        return len(src) if end == -1 else end
    end = src.find("*/", i + 2)
    return len(src) if end == -1 else end + 2


def _regex_end(src: str, i: int) -> int | None:
    """The index after a regex literal starting at ``src[i] == '/'``, or None when there is no
    closing ``/`` on the line (then it was a division or JSX after all)."""
    n = len(src)
    j = i + 1
    in_class = False
    while j < n:
        c = src[j]
        if c == "\n":
            return None
        if c == "\\":
            j += 2
            continue
        if c == "[":
            in_class = True
        elif c == "]":
            in_class = False
        elif c == "/" and not in_class:
            j += 1
            while j < n and (src[j].isalnum() or src[j] in "_$"):
                j += 1
            return j
        j += 1
    return None


def _regex_allowed(prev: Tok | None, src: str, i: int) -> bool:
    if i + 1 < len(src) and src[i + 1] == ">":
        return False  # JSX self-closing `/>`
    if prev is None:
        return True
    if prev.kind == "punct":
        return prev.value in _REGEX_AFTER_PUNCT and prev.value != "<"  # `</div>` is JSX
    return prev.kind == "id" and prev.value in _REGEX_AFTER_WORD


def tokenize(src: str) -> list[Tok]:
    """Tokens of a JavaScript or TypeScript source, comments dropped. Good enough to find calls:
    strings, templates (with nested templates), regex literals and JSX text do not derail it past
    the end of a line."""
    toks: list[Tok] = []
    i, n, line = 0, len(src), 1
    while i < n:
        c = src[i]
        if c == "\n":
            line += 1
            i += 1
            continue
        if c.isspace():
            i += 1
            continue
        start = i
        if c == "/" and i + 1 < n and src[i + 1] in "/*":
            i = _skip_comment(src, i)
        elif c == "'" and 0 < i < n - 1 and src[i - 1].isalnum() and src[i + 1].isalpha():
            # An apostrophe inside a word (JSX text: `Don't`) never opens a string in code.
            toks.append(Tok("punct", c, line))
            i += 1
        elif c in "'\"":
            value, i = _skip_string(src, i, c)
            toks.append(Tok("str", value, line))
        elif c == "`":
            parts, i, offsets = _skip_template(src, i)
            toks.append(Tok("tpl", parts, line, offsets))
        elif c.isalpha() or c in "_$" or ord(c) > 127:
            while i < n and (src[i].isalnum() or src[i] in "_$" or ord(src[i]) > 127):
                i += 1
            toks.append(Tok("id", src[start:i], line))
        elif c.isdigit():
            while i < n and (src[i].isalnum() or src[i] in "._"):
                i += 1
            toks.append(Tok("num", src[start:i], line))
        elif c == "/" and _regex_allowed(toks[-1] if toks else None, src, i):
            end = _regex_end(src, i)
            if end is None:
                toks.append(Tok("punct", "/", line))
                i += 1
            else:
                toks.append(Tok("regex", src[i:end], line))
                i = end
        elif c == "?" and i + 1 < n and src[i + 1] == "." and not src[i + 2 : i + 3].isdigit():
            toks.append(Tok("punct", ".", line))  # optional chaining reads like `.`
            i += 2
        else:
            toks.append(Tok("punct", c, line))
            i += 1
        line += src.count("\n", start, i)
    return toks


# --- paths -------------------------------------------------------------------------------------


def _is(tok: Tok | None, kind: str, value: str | None = None) -> bool:
    return tok is not None and tok.kind == kind and (value is None or tok.value == value)


class ScanBudgetExceeded(Exception):
    """A file whose calls cost more than ``WORK_PER_TOKEN`` steps per token to read."""


#: Argument tokens one file may make the scanner walk, per token of the file (plus a floor).
#: Nested calls re-read their enclosing arguments, so a file of thousands of calls nested in one
#: another costs the square of its size; ordinary code stays far below the bound.
WORK_PER_TOKEN = 64
WORK_FLOOR = 100_000
_work = [0]


def _spend(steps: int) -> None:
    _work[0] -= steps
    if _work[0] < 0:
        raise ScanBudgetExceeded(
            "calls nested too deeply to read within the work bound; the file is skipped"
        )


def _split_args(toks: list[Tok], open_index: int) -> tuple[list[list[Tok]], int]:
    """Arguments of the call whose ``(`` is at ``open_index``, and the index after its ``)``."""
    args: list[list[Tok]] = [[]]
    depth = 0
    i = open_index + 1
    while i < len(toks):
        tok = toks[i]
        if tok.kind == "punct" and tok.value in "([{":
            depth += 1
        elif tok.kind == "punct" and tok.value in ")]}":
            if depth == 0:
                _spend(i - open_index)
                return ([] if args == [[]] else args), i + 1
            depth -= 1
        elif tok.kind == "punct" and tok.value == "," and depth == 0:
            args.append([])
            i += 1
            continue
        args[-1].append(tok)
        i += 1
    _spend(i - open_index)
    return ([] if args == [[]] else args), i


def _operands(expr: list[Tok]) -> list[list[Tok]] | None:
    """``a + b + c`` as its operands, or None when ``expr`` holds anything else at the top level."""
    out: list[list[Tok]] = [[]]
    for tok in expr:
        if tok.kind == "punct" and tok.value == "+":
            out.append([])
        elif tok.kind in ("str", "tpl", "id", "num") or (tok.kind == "punct" and tok.value == "."):
            out[-1].append(tok)
        else:
            return None
    return out if all(out) else None


def _text(expr: list[Tok]) -> str:
    pieces: list[str] = []
    size = 0
    for tok in expr:
        if size > _RAW_LIMIT:
            break  # the rest would be cut anyway
        if tok.kind == "str":
            pieces.append(repr(tok.value))
        elif tok.kind == "tpl":
            pieces.append("`" + "".join(v if k == "text" else "${" + v + "}" for k, v in tok.value)
                          + "`")
        else:
            pieces.append(str(tok.value))
        size += len(pieces[-1]) + 1
    return " ".join(pieces)[:_RAW_LIMIT]


Pieces = list[tuple[str, str]]
#: The file's constants: a plain mapping, or the ``_Resolver`` that works them out on demand.
Consts = "dict[str, Pieces] | _Resolver"


#: A string expression with more pieces or text than this is not a path this scanner reads.
MAX_PIECES = 64
MAX_PATH_TEXT = 2048


def _piece_list(expr: list[Tok], consts: Consts) -> Pieces | None:
    """``expr`` as ("text", s) / ("expr", source) pieces, or None when it is not a string-ish
    expression this scanner reads."""
    operands = _operands(expr)
    if operands is None:
        return None
    pieces: list[tuple[str, str]] = []
    size = 0
    for more in _operand_pieces(operands, consts):
        # Every constant was bounded when it was read, so one step adds a bounded amount: a
        # chain of `const b = a + a` stops here instead of doubling until memory runs out.
        size += sum(len(value) for _, value in more)
        if len(pieces) + len(more) > MAX_PIECES or size > MAX_PATH_TEXT:
            return None
        pieces.extend(more)
    return pieces


def _operand_pieces(operands: list[list[Tok]], consts: Consts) -> Iterator[Pieces]:
    for operand in operands:
        if len(operand) == 1 and operand[0].kind == "str":
            yield [("text", operand[0].value)]
        elif len(operand) == 1 and operand[0].kind == "tpl":
            for kind, value in operand[0].value:
                # `${BASE}/users` with a same-file `const BASE = ...` reads through the constant.
                yield consts[value] if kind == "expr" and value in consts else [(kind, value)]
        elif len(operand) == 1 and operand[0].kind == "id" and operand[0].value in consts:
            yield consts[operand[0].value]
        else:
            yield [("expr", _text(operand))]


def _path_from_pieces(pieces: list[tuple[str, str]]) -> str | None:
    """The path the pieces spell, ``${expr}`` as ``{}``; a leading expression followed by ``/`` is
    a base URL and dropped. None unless the result starts with ``/``, and None when a piece is a
    name the file binds to a string it cannot read (``unknown``)."""
    if any(kind == "unknown" for kind, _ in pieces):
        return None
    if len(pieces) > 1 and pieces[0][0] == "expr" and pieces[1][0] == "text" and \
            pieces[1][1].startswith("/"):
        pieces = pieces[1:]
    path = "".join(v if k == "text" else "{}" for k, v in pieces)
    if not path.startswith("/") or path.startswith("//"):
        return None
    return path


def resolve_path(expr: list[Tok], consts: dict[str, list[tuple[str, str]]]) -> str | None:
    pieces = _piece_list(expr, consts)
    return None if pieces is None else _path_from_pieces(pieces)


def _initializer(toks: list[Tok], i: int) -> int | None:
    """The index of the ``=`` after the name at ``toks[i]``, past a type annotation
    (``BASE: string``, ``R: Record<string, () => void>``); None when there is none."""
    j = i + 1
    if j < len(toks) and _is(toks[j], "punct", "="):
        return j
    if not (j < len(toks) and _is(toks[j], "punct", ":")):
        return None
    depth = 0
    for k in range(j + 1, len(toks)):
        tok = toks[k]
        if tok.kind != "punct":
            continue
        if tok.value in "([{<":
            depth += 1
        elif tok.value in ")]}" or (tok.value == ">" and not _is(toks[k - 1], "punct", "=")):
            depth -= 1
            if depth < 0:
                _spend(k - i)
                return None
        elif tok.value == "=" and depth == 0 and not _is(toks[k + 1] if k + 1 < len(toks)
                                                          else None, "punct", ">"):
            _spend(k - i)
            return k
        elif tok.value in ";," and depth == 0:
            _spend(k - i)
            return None
    _spend(len(toks) - i)
    return None


#: Operators that end a type and continue the value after a ``as T`` (``|`` and ``&`` are type
#: unions and intersections).
_VALUE_OPS = "+-*/%?:,!"


def _without_assertion(expr: list[Tok]) -> list[Tok] | None:
    """``expr`` without a top-level TypeScript ``as T`` or ``satisfies T`` tail
    (``'/v1' as const``), which says nothing about the value. None when more expression follows
    the type (``'/v1' as string + '/items'``, ``as string ? '/a' : '/b'``): cutting there would
    drop part of the value."""
    depth = 0
    cut: int | None = None
    for k, tok in enumerate(expr):
        # Angle brackets nest only inside the asserted type (`as Record<string, X>`).
        opening, closing = ("([{<", ")]}>") if cut is not None else ("([{", ")]}")
        if tok.kind == "punct" and tok.value in opening:
            depth += 1
        elif tok.kind == "punct" and tok.value in closing:
            depth -= 1
        elif depth == 0 and k and tok.kind == "id" and tok.value in ("as", "satisfies"):
            cut = k if cut is None else cut
        elif depth == 0 and cut is not None and tok.kind == "punct" and tok.value in _VALUE_OPS:
            return None
    return expr if cut is None else expr[:cut]


#: A line ending in one of these, or starting with one, continues the expression before it, as
#: JavaScript reads it: `'/v1'` then `.concat('/items')` on the next line is one value.
_OPERATORS = ".([+-*/%?:|&=<>^"


def _continues(last: Tok, first: Tok) -> bool:
    """Whether the line starting with ``first`` continues the expression ending with ``last``."""
    if last.kind == "punct" and last.value in _OPERATORS and last.value not in ")]":
        return True
    return first.kind == "tpl" or (first.kind == "punct" and first.value in _OPERATORS)


def _declarator_value(toks: list[Tok], j: int) -> tuple[list[Tok], int]:
    """The initializer starting at ``toks[j]`` and the index of what ends it: a ``;``, a top-level
    ``,`` (the next declarator of ``const A = '/a', B = 5``) or a new line that does not continue
    the expression (``_continues``)."""
    expr: list[Tok] = []
    depth = 0
    while j < len(toks):
        tok = toks[j]
        if _is(tok, "punct", ";") or (depth == 0 and _is(tok, "punct", ",")):
            break
        if expr and tok.line > expr[-1].line and not _continues(expr[-1], tok):
            break
        if tok.kind == "punct" and tok.value in "([{":
            depth += 1
        elif tok.kind == "punct" and tok.value in ")]}":
            depth -= 1
        expr.append(tok)
        j += 1
    _spend(len(expr) + 1)
    return expr, j


def _constants(toks: list[Tok]) -> dict[str, list[tuple[str, str]]]:
    """``const NAME = <string expression>`` anywhere in the file, as pieces; every declarator of
    ``const A = ..., B = ...`` counts. A name the file binds to a string this scanner cannot pin
    down maps to an ``unknown`` piece, so a path built on it stays unresolved instead of being
    shortened as if the name were a base URL: a ``let`` or ``var`` (it can be reassigned before
    the request), a name declared more than once with different values, an initializer that holds
    a string but is not a plain string expression (``cond ? '/a' : '/b'``). Identical
    redeclarations are kept. Declarations are gathered first and resolved after, so a constant
    can use one declared further down the file (a function body runs after the module's top
    level); a cycle leaves its names unknown."""
    decls: dict[str, list[list[Tok]]] = {}
    unknown: set[str] = set()
    mutable: set[str] = set()
    for i, tok in enumerate(toks):
        if tok.kind != "id" or tok.value not in ("const", "let", "var"):
            continue
        k = i + 1
        while k < len(toks) and toks[k].kind == "id":
            name = toks[k].value
            eq = _initializer(toks, k)
            if eq is None:
                break
            expr, j = _declarator_value(toks, eq + 1)
            if tok.value == "const":
                decls.setdefault(name, []).append(expr)
            elif any(t.kind in ("str", "tpl") for t in expr):
                unknown.add(name)
            else:
                mutable.add(name)
            if not (j < len(toks) and _is(toks[j], "punct", ",")):
                break
            k = j + 1
    # A `let url = getUrl()` may shadow a `const url` where a call reads it; a `let` that shadows
    # nothing keeps its base-URL reading.
    unknown |= (_shadowing_names(toks) | mutable) & decls.keys()
    resolver = _Resolver(decls, unknown)
    return {name: pieces for name in [*decls, *unknown] if (pieces := resolver.get(name))}


class _Resolver:
    """Each constant's pieces, worked out once on demand, so the order of declarations does not
    matter. Behaves as the read-only mapping ``_piece_list`` looks names up in."""

    def __init__(self, decls: dict[str, list[list[Tok]]], unknown: set[str]) -> None:
        self.decls, self.unknown = decls, unknown
        self.done: dict[str, Pieces | None] = {}
        self.busy: set[str] = set()

    def get(self, name: str) -> Pieces | None:
        """The pieces of ``name``; ``unknown`` for a name bound to a string this scanner cannot
        read; None for a name that is not a string constant (a base URL from the environment)."""
        if name in self.unknown or name in self.busy:
            return [("unknown", name)]
        if name in self.done:
            return self.done[name]
        if name not in self.decls:
            return None
        self.busy.add(name)
        found: list[Pieces | None] = []
        for expr in self.decls[name]:
            value = _without_assertion(expr)
            found.append(None if value is None else _piece_list(value, self))
        self.busy.discard(name)
        readable = [p for p in found if p is not None and any(k != "expr" for k, _ in p)]
        stringish = any(t.kind in ("str", "tpl") for expr in self.decls[name] for t in expr)
        if readable and len(readable) == len(found) and all(p == readable[0] for p in readable):
            result: Pieces | None = readable[0]
        elif readable or stringish:
            result = [("unknown", name)]
        else:
            result = None
        self.done[name] = result
        return result

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self.get(name) is not None

    def __getitem__(self, name: str) -> Pieces:
        pieces = self.get(name)
        if pieces is None:
            raise KeyError(name)
        return pieces


def _arrow_at(toks: list[Tok], i: int) -> bool:
    """``=>`` (two tokens) at ``toks[i]``."""
    return i + 1 < len(toks) and _is(toks[i], "punct", "=") and _is(toks[i + 1], "punct", ">")


#: Keywords whose ``( ... )`` is a condition or a loop head, not a parameter list.
_NOT_PARAMS = ("if", "while", "for", "switch", "with")


def _shadowing_names(toks: list[Tok]) -> set[str]:
    """Names the file binds other than by a readable ``const``: parameters (``function f(url)``,
    ``(url: string) => ...``, ``url => ...``, ``load(url) {``, ``catch (url)``, destructured or
    typed), destructured declarations and declarations without a value (``for (const url of
    urls)``). Scopes are not tracked: a constant of the same name could be shadowed where a call
    reads it, so it is left unresolved everywhere, never read as the wrong path."""
    names: set[str] = set()
    opens: list[int] = []
    for i, tok in enumerate(toks):
        prev = toks[i - 1] if i else None
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        if tok.kind == "id" and tok.value in ("const", "let", "var") and nxt is not None:
            if nxt.kind == "id" and _initializer(toks, i + 1) is None:
                names.add(nxt.value)
            elif _is(nxt, "punct", "{") or _is(nxt, "punct", "["):
                _, end = _split_args(toks, i + 1)
                names.update(t.value for t in toks[i + 2:end] if t.kind == "id")
        elif tok.kind == "id" and _arrow_at(toks, i + 1) and not _is(prev, "punct", "."):
            names.add(tok.value)  # url => ...
        elif _is(tok, "punct", "("):
            opens.append(i)
        elif _is(tok, "punct", ")") and opens:
            start = opens.pop()
            before = toks[start - 1] if start else None
            if before is not None and before.kind == "id" and before.value in _NOT_PARAMS:
                continue
            if _is(nxt, "punct", "{") or _is(nxt, "punct", ":") or _arrow_at(toks, i + 1):
                _spend(i - start)
                names.update(t.value for t in toks[start + 1:i] if t.kind == "id")
    return names


# --- call sites --------------------------------------------------------------------------------


def _skip_type_args(toks: list[Tok], i: int) -> int:
    """The index after ``<...>`` type arguments starting at ``toks[i] == '<'`` (``>>`` is two
    tokens), or ``i`` when they do not close: an unmatched ``)``, ``}`` or ``]``, or a ``;``
    outside braces, ends the scan. Parentheses inside (``(User | null)[]``, ``() => void``) are
    balanced, and the ``>`` of ``=>`` closes nothing."""
    depth = 0
    brace_depth = 0
    bracket_depth = 0
    paren_depth = 0
    j = i
    while j < len(toks):
        tok = toks[j]
        if _is(tok, "punct", "<"):
            depth += 1
        elif _is(tok, "punct", ">") and not _is(toks[j - 1], "punct", "="):
            depth -= 1
            if depth == 0:
                _spend(j - i)
                return j + 1
        elif _is(tok, "punct", "{"):
            brace_depth += 1
        elif _is(tok, "punct", "}"):
            brace_depth -= 1
            if brace_depth < 0:
                _spend(j - i)
                return i
        elif _is(tok, "punct", "["):
            bracket_depth += 1
        elif _is(tok, "punct", "]"):
            bracket_depth -= 1
            if bracket_depth < 0:
                _spend(j - i)
                return i
        elif _is(tok, "punct", "("):
            paren_depth += 1
        elif _is(tok, "punct", ")"):
            paren_depth -= 1
            if paren_depth < 0:
                _spend(j - i)
                return i
        elif tok.kind == "punct" and tok.value == ";" and brace_depth == 0:
            _spend(j - i)
            return i
        j += 1
    _spend(j - i)
    return i


def _receiver(toks: list[Tok], dot: int) -> tuple[str, int] | None:
    """The receiver chain ending before ``toks[dot] == '.'`` (``this.api``, ``client``) and the
    index of its first token; None when it is not a plain name chain."""
    j = dot - 1
    if j < 0 or toks[j].kind != "id":
        return None
    while j - 2 >= 0 and _is(toks[j - 1], "punct", ".") and toks[j - 2].kind == "id":
        j -= 2
    _spend(dot - j)
    return toks[dot - 1].value, j


def _object_fields(arg: list[Tok]) -> dict[str, list[Tok]]:
    """Top-level ``key: value`` pairs of an object literal argument (``{`` ... ``}``); a computed
    key that is a string (``['method']: 'POST'``) counts as that key. A ``...spread`` entry or a
    computed key the scanner cannot read (``[key]: value``) can overwrite any field before it, so
    those are dropped; fields after it are kept."""
    if not arg or not _is(arg[0], "punct", "{"):
        return {}
    fields: dict[str, list[Tok]] = {}
    inner, _ = _split_args([Tok("punct", "(", 0), *arg[1:]], 0)
    for entry in inner:
        if _overwrites_any(entry):
            fields.clear()
        elif len(entry) >= 5 and _is(entry[0], "punct", "[") and entry[1].kind == "str" and \
                _is(entry[2], "punct", "]") and _is(entry[3], "punct", ":"):
            fields[entry[1].value] = entry[4:]
        elif len(entry) >= 3 and entry[0].kind in ("id", "str") and _is(entry[1], "punct", ":"):
            fields[entry[0].value] = entry[2:]
        elif len(entry) == 1 and entry[0].kind == "id":
            fields[entry[0].value] = entry  # shorthand `{ url }`
    return fields


def _overwrites_any(entry: list[Tok]) -> bool:
    """A ``...spread`` entry, or a computed key other than one string (``[key]: value``)."""
    if len(entry) >= 3 and all(_is(t, "punct", ".") for t in entry[:3]):
        return True
    if entry and _is(entry[0], "punct", "["):
        return not (len(entry) >= 3 and entry[1].kind == "str" and _is(entry[2], "punct", "]"))
    return False


def _has_spread(arg: list[Tok]) -> bool:
    """True when the object literal ``arg`` has a top-level entry that could set any field: a
    ``...spread`` or a computed key the scanner cannot read."""
    if not arg or not _is(arg[0], "punct", "{"):
        return False
    inner, _ = _split_args([Tok("punct", "(", 0), *arg[1:]], 0)
    return any(_overwrites_any(entry) for entry in inner)


def _method_of(value: list[Tok] | None, default: str | None) -> str:
    if value is None:
        return default or "?"
    if len(value) == 1 and value[0].kind == "str":
        return value[0].value.upper()
    return "?"


def _fact(method: str, path: str | None, line: int, style: str, raw: list[Tok]) -> dict[str, Any]:
    return {
        "method": method,
        "path": path or "",
        "line": line,
        "style": style,
        "resolved": path is not None,
        "raw": "" if path is not None else _text(raw),
    }


#: How deep ``${...}`` inside templates inside ``${...}`` is scanned for calls.
TEMPLATE_DEPTH = 8


def extract_calls(
    toks: list[Tok], consts: dict[str, list[tuple[str, str]]] | None = None, depth: int = 0
) -> list[dict[str, Any]]:
    """The call sites in ``toks``, in line order, those inside a template's ``${...}`` included
    (``consts``: the file's constants, for an expression scanned on its own). Raises
    ``ScanBudgetExceeded`` past ``WORK_PER_TOKEN`` steps per token."""
    if depth == 0:
        _work[0] = WORK_FLOOR + WORK_PER_TOKEN * len(toks)
    if consts is None:
        consts = _constants(toks)
    else:
        # `${items.map(url => fetch(url))}`: a parameter inside the expression shadows too.
        shadowed = _shadowing_names(toks) & consts.keys()
        if shadowed:
            consts = {**consts, **{name: [("unknown", name)] for name in shadowed}}
    calls: list[dict[str, Any]] = []
    for i, tok in enumerate(toks):
        prev = toks[i - 1] if i else None
        # <receiver>.<verb>[<T>](path, ...)
        if tok.kind == "id" and tok.value in VERBS and _is(prev, "punct", "."):
            found = _receiver(toks, i - 1)
            if found is None:
                continue
            name, first = found
            if first and _is(toks[first - 1], "id", "typeof"):
                continue
            j = i + 1
            if _is(toks[j] if j < len(toks) else None, "punct", "<"):
                j = _skip_type_args(toks, j)
            if not _is(toks[j] if j < len(toks) else None, "punct", "("):
                continue
            args, _ = _split_args(toks, j)
            if not args:
                continue
            path = resolve_path(args[0], consts)
            if path is None and resolve_path_is_external(args[0]):
                continue
            if path is not None or name in CLIENT_NAMES:
                calls.append(_fact(tok.value.upper(), path, tok.line, "client", args[0]))
        # fetch(path, { method })
        elif _is(tok, "id", "fetch") and not _is(prev, "id", "function"):
            j = i + 1
            if not _is(toks[j] if j < len(toks) else None, "punct", "("):
                continue
            if _is(prev, "punct", ".") and not (i >= 2 and toks[i - 2].value in GLOBALS):
                continue  # `client.fetch(...)` is someone else's method
            args, _ = _split_args(toks, j)
            if not args:
                continue
            path = resolve_path(args[0], consts)
            if path is None and resolve_path_is_external(args[0]):
                continue
            options = _object_fields(args[1]) if len(args) > 1 else {}
            second = args[1] if len(args) > 1 else []
            if not second:
                default: str | None = "GET"
            elif _is(second[0], "punct", "{"):
                default = None if _has_spread(second) else "GET"
            else:
                default = None
            calls.append(_fact(_method_of(options.get("method"), default), path, tok.line,
                               "fetch", args[0]))
        # { url: path, method: 'post' }, or the shorthand { url, method: 'post' }
        elif (tok.kind in ("id", "str") and tok.value == "url" and i + 1 < len(toks)
              and (_is(toks[i + 1], "punct", ":") or tok.kind == "id"
                   and toks[i + 1].kind == "punct" and toks[i + 1].value in (",", "}"))
              and prev is not None and prev.kind == "punct" and prev.value in "{,"):
            start = _object_start(toks, i)
            if start is None:
                continue
            body, _ = _split_args(toks, start)
            obj = [toks[start], *[t for arg in body for t in arg + [Tok("punct", ",", 0)]]]
            fields = _object_fields(obj)
            value = fields.get("url")
            if not value:
                continue
            path = resolve_path(value, consts)
            if path is None:
                continue  # an object with a `url` that is not a path is not evidence of a call
            default = None if _has_spread(obj) else "GET"
            calls.append(_fact(_method_of(fields.get("method"), default), path, tok.line,
                               "object", value))
    if depth < TEMPLATE_DEPTH:
        for tok in toks:
            if tok.kind == "tpl":
                calls.extend(_template_calls(tok, consts, depth))
    calls.sort(key=lambda c: c["line"])
    return calls


def _template_calls(
    tok: Tok, consts: dict[str, list[tuple[str, str]]], depth: int
) -> list[dict[str, Any]]:
    """Calls inside the ``${...}`` expressions of the template ``tok``; the literal text is never
    scanned."""
    found: list[dict[str, Any]] = []
    for (kind, value), offset in zip(tok.value, tok.lines, strict=True):
        if kind == "expr":
            for call in extract_calls(tokenize(value), consts, depth + 1):
                found.append({**call, "line": call["line"] + tok.line + offset - 1})
    return found


def _object_start(toks: list[Tok], i: int) -> int | None:
    """The index of the ``{`` that opens the object literal holding the key at ``i``."""
    depth = 0
    j = i - 1
    while j >= 0:
        tok = toks[j]
        if tok.kind == "punct" and tok.value in ")]}":
            depth += 1
        elif tok.kind == "punct" and tok.value in "([{":
            if depth == 0:
                _spend(i - j)
                return j if tok.value == "{" else None
            depth -= 1
        j -= 1
    _spend(i)
    return None


def resolve_path_is_external(expr: list[Tok]) -> bool:
    """``fetch('https://...')``, ``fetch('/config.json')``-style static files are not API calls:
    an absolute URL literal is external."""
    if len(expr) == 1 and expr[0].kind == "str":
        return "://" in expr[0].value
    if len(expr) == 1 and expr[0].kind == "tpl" and expr[0].value and expr[0].value[0][0] == "text":
        return "://" in expr[0].value[0][1]
    return False


def parse_source(source: str) -> dict[str, Any]:
    """Facts for one file's text: ``{"calls": [...], "error": str|None}``."""
    try:
        return {"calls": extract_calls(tokenize(source)), "error": None}
    except (RecursionError, MemoryError, IndexError, ScanBudgetExceeded) as exc:
        return {"calls": [], "error": f"{type(exc).__name__}: {exc}"}


# --- the view ----------------------------------------------------------------------------------


def _typed(row: Any, types: dict[str, Any]) -> bool:
    if not isinstance(row, dict) or set(row) != set(types):
        return False
    for key, kind in types.items():
        value = row[key]
        if kind == "int":
            if not isinstance(value, int) or isinstance(value, bool):
                return False
        elif not isinstance(value, kind):
            return False
    return True


def _valid_facts(facts: Any) -> bool:
    return (
        isinstance(facts, dict)
        and set(facts) == {"calls", "error"}
        and (facts["error"] is None or isinstance(facts["error"], str))
        and isinstance(facts["calls"], list)
        and all(_typed(c, CALL_FACT_TYPES) for c in facts["calls"])
    )


#: What a language pack's ``read_calls`` may return for one file, past which it is refused.
MAX_PACK_CALLS = 20_000
_STYLES = frozenset({"client", "fetch", "object"})
_METHODS = frozenset({verb.upper() for verb in VERBS} | {"?"})


def _pack_call(call: Any) -> bool:
    return (
        _typed(call, CALL_FACT_TYPES)
        and call["method"] in _METHODS and call["style"] in _STYLES and call["line"] >= 1
        and len(call["path"]) <= MAX_PATH_TEXT and len(call["raw"]) <= _RAW_LIMIT
        and call["resolved"] == bool(call["path"])
    )


def read_with_pack(reader: Language, source: str, suffix: str) -> dict[str, Any]:
    """The checked calls a language pack reads in one file: a pack that raises or returns
    anything malformed gives a file error, never a crash or an unchecked field."""
    assert reader.read_calls is not None
    try:
        got = reader.read_calls(source, suffix)
    except Exception as exc:  # the pack's own failure on one file
        return {"calls": [], "error": f"{type(exc).__name__}: {str(exc)[:_RAW_LIMIT]}"}
    if not (
        isinstance(got, dict) and set(got) == {"calls", "error"}
        and (got["error"] is None or isinstance(got["error"], str) and len(got["error"]) <= 1000)
        and isinstance(got["calls"], list) and len(got["calls"]) <= MAX_PACK_CALLS
        and all(_pack_call(c) for c in got["calls"])
    ):
        return {"calls": [], "error": f"language pack {reader.name} returned malformed calls"}
    return {"calls": sorted(got["calls"], key=lambda c: c["line"]), "error": got["error"]}


def _readers_for(readers: Sequence[Language]) -> dict[str, Language]:
    """The pack that reads each suffix: the first by name that offers it."""
    by_suffix: dict[str, Language] = {}
    for reader in sorted(readers, key=lambda r: r.name):
        if reader.read_calls is None:
            continue
        for suffix in reader.call_suffixes:
            by_suffix.setdefault(suffix, reader)
    return by_suffix


def object_kind(readers: Sequence[Language] = ()) -> str:
    """The facts format: the built-in reader's, plus each pack that reads some suffix in its
    place and its version, so installing, upgrading or removing a pack re-reads the files."""
    chosen = sorted(
        {(r.name, r.version, suffix) for suffix, r in _readers_for(readers).items()}
    )
    if not chosen:
        return OBJECT_KIND
    tag = hashlib.sha256(repr(chosen).encode()).hexdigest()[:12]
    return f"{OBJECT_KIND}+{tag}"


def _valid_view(data: Any, kind: str = OBJECT_KIND) -> bool:
    """A view built from facts of the current format: the facts of unchanged files written by
    an earlier scanner would otherwise outlive a scanner fix."""
    return (
        isinstance(data, dict)
        and data.get("format") == kind
        and isinstance(data.get("calls"), list)
        and isinstance(data.get("errors"), list)
        and all(_typed(c, {**CALL_FACT_TYPES, "file": str}) for c in data["calls"])
        and all(_typed(e, {"file": str, "error": str}) for e in data["errors"])
    )


def current_fingerprint(cache: Cache, config: Config) -> dict[str, str]:
    scanned = cache.scan(config.client_paths or None, SUFFIXES)
    return {rel: sha for rel, sha in scanned.items() if not is_skipped_client_file(rel)}


def _fact_key(rel: str, sha: str, by_suffix: dict[str, Language]) -> str:
    """Where a file's facts are cached: by content, and for a file a pack reads also by its
    suffix, since the same text is read with another grammar, or by another reader, under
    another suffix."""
    suffix = PurePosixPath(rel).suffix
    return f"{sha}{suffix}" if suffix in by_suffix else sha


def build(
    cache: Cache, config: Config, force: bool = False, readers: Sequence[Language] = ()
) -> tuple[ViewRecord, bool]:
    """The ``calls`` view, rebuilt incrementally when any client file changed. Test files
    (``*.test.ts``, ``__tests__/`` and the like) and declaration files are left out. A file
    whose suffix one of the language packs ``readers`` reads is read by that pack, any other by
    the built-in tokenizer."""
    fingerprint = current_fingerprint(cache, config)
    kind = object_kind(readers)
    by_suffix = _readers_for(readers)
    record = cache.read_view(VIEW)
    if (
        record is not None
        and not force
        and record.fingerprint == fingerprint
        and (record := cache.served(record, kind, lambda d: _valid_view(d, kind))) is not None
    ):
        cache.flush()
        return record, False
    objects = cache.load_objects(kind)
    keys = {rel: _fact_key(rel, sha, by_suffix) for rel, sha in fingerprint.items()}
    added = False
    for rel, key in keys.items():
        if not _valid_facts(objects.get(key)):
            text = (cache.root / rel).read_bytes().decode("utf-8", errors="replace")
            suffix = PurePosixPath(rel).suffix
            reader = by_suffix.get(suffix)
            objects[key] = (
                read_with_pack(reader, text, suffix) if reader else parse_source(text)
            )
            added = True
    if added:
        cache.save_objects(kind, objects)
    calls: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for rel in sorted(fingerprint):
        facts = objects[keys[rel]]
        if facts["error"]:
            errors.append({"file": rel, "error": facts["error"]})
        calls.extend({**c, "file": rel} for c in facts["calls"])
    record = cache.write_view(
        VIEW, {"format": kind, "calls": calls, "errors": errors}, fingerprint,
        sign=True,  # built from checked facts: a warm read need not check every row again
    )
    cache.flush()
    return record, True


def select(
    data: dict[str, Any], substring: str = "", method: str | None = None,
    unresolved: bool = False,
) -> list[dict[str, Any]]:
    needle = substring.lower()
    return [
        c
        for c in data["calls"]
        if any(needle in c[key].lower() for key in ("path", "file", "raw"))
        and (method is None or c["method"] == method.upper())
        and (not unresolved or not c["resolved"])
    ]
