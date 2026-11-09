"""Terraform for codeterrain: the definitions and references of ``.tf`` files.

Definitions are named the way Terraform code refers to them:

- ``resource "aws_s3_bucket" "logs"`` is ``aws_s3_bucket.logs``;
- ``data "aws_ami" "base"`` is ``data.aws_ami.base``;
- ``module "vpc"`` is ``module.vpc``, ``variable "region"`` is ``var.region``, an entry of a
  ``locals`` block is ``local.name`` and ``output "url"`` is ``output.url``;
- an entry of ``required_providers`` is ``provider.aws``, and ``provider "aws"`` is
  ``provider.aws`` too, or ``provider.aws.east`` with ``alias = "east"``.

A module call's doc is its ``source`` and ``version``, a required provider's the same; a
resource's ``provider = aws.east`` and a module's ``providers = { aws = aws.east }`` are
references to ``provider.aws.east``.

References are the expressions that name one of those (``var.region``, ``aws_s3_bucket.logs.arn``,
``module.vpc.id``), in attribute values and inside string and heredoc templates, with the
definition they sit in as their scope. The reader is a tokenizer, not an evaluator: it follows no
module source, expands no ``count`` or ``for_each`` and reads no ``.tfvars``.
"""

from __future__ import annotations

import re
import string
import sys
import textwrap
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Any

try:
    __version__ = version("codeterrain-terraform")
except PackageNotFoundError:  # run from a source tree
    __version__ = "0"

#: Labels each top-level block takes, and how its definition is named.
_BLOCKS = {"resource": 2, "data": 2, "module": 1, "variable": 1, "output": 1, "provider": 1}
_PREFIX = {"resource": "", "data": "data.", "module": "module.", "variable": "var.",
           "output": "output.", "provider": "provider."}
#: Objects whose attributes are never a definition of this configuration.
_BUILTIN = frozenset({"each", "count", "path", "terraform", "self"})
#: A resource type: a provider name, ``_``, then the type (``aws_s3_bucket``, ``random_id``).
_RESOURCE_TYPE = re.compile(r"[a-z][a-z0-9]*_[a-z0-9_]+\Z")
#: A Terraform identifier: Unicode letters, digits, ``_`` and ``-``, not starting with a digit.
_IDENT = re.compile(r"[^\W\d][\w-]*")
#: Operators of more than one character, kept as one token so ``==`` is never read as ``=``.
_OPERATOR = re.compile(r"==|!=|<=|>=|=>|&&|\|\||\.\.\.|::")
_HEREDOC = re.compile(r"<<-?([^\W\d][\w-]*)[ \t]*\r?\n")
#: What a quoted string's single-character escapes stand for.
_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}
#: Deepest nesting of templates inside templates read before the file is given up.
MAX_NESTING = 64
MAX_DOC = 200


class TerraformError(ValueError):
    pass


@dataclass
class Tok:
    #: "ident", "str" (a literal string: its text), "punct", "mark" (a directive's ``%{`` or end)
    kind: str
    value: str
    line: int


class _Lexer:
    def __init__(self, src: str) -> None:
        self.src = src
        self.i = 0
        self.line = 1
        self.out: list[Tok] = []

    def _advance(self, to: int) -> None:
        self.line += self.src.count("\n", self.i, to)
        self.i = to

    def run(self) -> list[Tok]:
        self._code(stop=None, depth=0)
        return self.out

    def _code(self, stop: str | None, depth: int) -> None:
        """Tokens up to the ``}`` that closes an interpolation (``stop``), or the end."""
        if depth > MAX_NESTING:
            raise TerraformError("templates nested too deeply")
        src, braces = self.src, 0
        while self.i < len(src):
            c = src[self.i]
            if c == "\n" or c in " \t\r":
                self._advance(self.i + 1)
            elif c == "#" or src.startswith("//", self.i):
                end = src.find("\n", self.i)
                self._advance(len(src) if end < 0 else end)
            elif src.startswith("/*", self.i):
                end = src.find("*/", self.i + 2)
                self._advance(len(src) if end < 0 else end + 2)
            elif c == '"':
                self._string(depth)
            elif src.startswith("<<", self.i) and (m := _HEREDOC.match(src, self.i)):
                self._heredoc(m, depth)
            elif m := _IDENT.match(src, self.i):
                self.out.append(Tok("ident", m.group(), self.line))
                self._advance(m.end())
            elif m := _OPERATOR.match(src, self.i):
                self.out.append(Tok("punct", m.group(), self.line))
                self._advance(m.end())
            elif c in "{[(":
                braces += c == "{"
                self.out.append(Tok("punct", c, self.line))
                self._advance(self.i + 1)
            elif c == "}" and stop is not None and braces == 0:
                self._advance(self.i + 1)
                return
            else:
                braces -= c == "}"
                self.out.append(Tok("punct", c, self.line))
                self._advance(self.i + 1)

    def _template(self, end: int, depth: int, quoted: bool = True) -> str:
        """Read template text up to ``end`` (exclusive), or in a quoted string up to its closing
        quote or the end of its line, lexing each ``${...}`` as code: the code lexer finds where
        an interpolation ends, so its comments, strings and braces never end the template. The
        literal text, for a block label. A backslash escapes only in a quoted string: a heredoc
        keeps it as text."""
        src, text = self.src, []
        while self.i < end:
            if quoted and src[self.i] in '"\n':
                break
            if src.startswith(("$${", "%%{"), self.i):
                text.append(src[self.i + 1:self.i + 3])
                self._advance(self.i + 3)
            elif src.startswith(("${", "%{"), self.i):
                directive = src[self.i] == "%"
                if directive:
                    self.out.append(Tok("mark", "%{", self.line))
                # a ``~`` strip marker (``%{~ endfor }``) is part of the opener, not code
                self._advance(self.i + 2 + src.startswith("~", self.i + 2))
                self._code(stop="}", depth=depth + 1)
                if directive:
                    self.out.append(Tok("mark", "%}", self.line))
            elif quoted and src[self.i] == "\\" and self.i + 1 < end:
                text.append(self._escape())
            else:
                text.append(src[self.i])
                self._advance(self.i + 1)
        return "".join(text)

    def _escape(self) -> str:
        """The character a quoted string's escape at ``self.i`` stands for (``\\n``, ``\\t``,
        ``\\u00e9``), moving past it; an unknown escape keeps the character after the backslash."""
        src, i = self.src, self.i
        kind = src[i + 1]
        width = {"u": 4, "U": 8}.get(kind)
        if width:
            digits = src[i + 2:i + 2 + width]
            if len(digits) == width and all(c in string.hexdigits for c in digits):
                code = int(digits, 16)
                if code <= sys.maxunicode and not 0xD800 <= code <= 0xDFFF:  # no lone surrogate
                    self._advance(i + 2 + width)
                    return chr(code)
        self._advance(i + 2)
        return _ESCAPES.get(kind, kind)

    def _string(self, depth: int) -> None:
        line = self.line
        self._advance(self.i + 1)
        text = self._template(len(self.src), depth)
        self.out.append(Tok("str", text, line))
        if self.i < len(self.src) and self.src[self.i] == '"':
            self._advance(self.i + 1)

    def _heredoc(self, m: re.Match[str], depth: int) -> None:
        line = self.line
        self._advance(m.end())
        close = re.compile(rf"^[ \t]*{re.escape(m.group(1))}[ \t]*\r?$", re.M)
        found = close.search(self.src, self.i)
        end = found.start() if found else len(self.src)
        text = self._template(end, depth, quoted=False)
        # the literal text, for a description: ``<<-`` strips the common indentation
        self.out.append(Tok("str", textwrap.dedent(text).strip(), line))
        if found:
            self._advance(found.end())


def _punct(toks: list[Tok], i: int, value: str) -> bool:
    """Whether ``toks[i]`` is the punctuation ``value``: never a string's text that reads the same
    (the ``.`` that ends ``"${var.domain}."``)."""
    return 0 <= i < len(toks) and toks[i].kind == "punct" and toks[i].value == value


def _entry(toks: list[Tok], i: int) -> bool:
    """Whether ``toks[i]`` separates an object entry's key from its value: ``=`` or ``:``. A
    block body's attributes take only ``=``."""
    return _punct(toks, i, "=") or _punct(toks, i, ":")


def _mark(toks: list[Tok], i: int, value: str) -> bool:
    """Whether ``toks[i]`` is where a template directive starts (``%{``) or ends (``%}``)."""
    return 0 <= i < len(toks) and toks[i].kind == "mark" and toks[i].value == value


def _traversal(toks: list[Tok], i: int) -> list[str]:
    """The names of ``a.b.c`` starting at ``toks[i]``, up to an index or the end."""
    parts = [toks[i].value]
    j = i + 1
    while j + 1 < len(toks) and _punct(toks, j, ".") and toks[j + 1].kind == "ident":
        parts.append(toks[j + 1].value)
        j += 2
    return parts


def _iterators(toks: list[Tok]) -> frozenset[str]:
    """The iterator names of the file's ``dynamic`` blocks: the label (``dynamic "rule"`` gives
    ``rule.value``), or the ``iterator = name`` that replaces it, written directly in that
    block's body."""
    found = set()
    depth = 0
    bodies: list[int] = []  # the brace depth of each open dynamic block's body
    for i, tok in enumerate(toks):
        if tok.kind == "punct" and tok.value == "{":
            depth += 1
        elif tok.kind == "punct" and tok.value == "}":
            depth -= 1
            while bodies and bodies[-1] > depth:
                bodies.pop()
        elif i + 2 < len(toks) and tok.kind == "ident":
            if tok.value == "dynamic" and toks[i + 1].kind == "str" and _punct(toks, i + 2, "{"):
                found.add(toks[i + 1].value)
                bodies.append(depth + 1)
            elif (
                tok.value == "iterator" and bodies and bodies[-1] == depth
                and _punct(toks, i + 1, "=") and toks[i + 2].kind == "ident"
                and not _punct(toks, i + 3, ".")
            ):
                found.add(toks[i + 2].value)
    return frozenset(found)


def _bound(toks: list[Tok]) -> set[int]:
    """The indexes of the tokens that name a ``for`` binding rather than the configuration:
    ``local`` in ``[for local in var.items : local.name]`` reads the element. An expression's
    names hold from its ``:`` to its closing bracket; a template directive's from the end of
    ``%{ for x in xs }`` to its ``%{ endfor }``."""
    found: set[int] = set()
    frames: list[set[str]] = []  # the names bound inside each open bracket
    questions: list[int] = []  # the ``?`` of each open bracket still waiting for their ``:``
    directives: list[set[str]] = []
    # Each ``for`` whose collection is still being read: its names, its bracket depth (None: a
    # directive) and the directive it opens (how many were open with it). A ``for`` nested in
    # that collection stacks above it.
    pending: list[tuple[set[str], int | None, int]] = []
    open_marks = 0  # directives opened and not yet ended
    for i, tok in enumerate(toks):
        if tok.kind == "punct" and tok.value in "{[(":
            frames.append(set())
            questions.append(0)
        elif tok.kind == "punct" and tok.value in "}])":
            if frames:
                closed = len(frames)
                frames.pop()
                questions.pop()
                pending = [p for p in pending if p[1] is None or p[1] < closed]
        elif tok.kind == "punct" and tok.value == "?" and questions:
            questions[-1] += 1
        elif tok.kind == "punct" and tok.value == ":" and questions and questions[-1]:
            questions[-1] -= 1  # the ``:`` of a conditional
        elif tok.kind == "punct" and tok.value == ":" and pending and pending[-1][1] == len(frames):
            if frames:
                frames[-1] |= pending[-1][0]
            pending.pop()
        elif _mark(toks, i, "%{"):
            open_marks += 1
        elif _mark(toks, i, "%}"):
            # only the end of the ``for`` directive's own ``%{ ... }`` starts its binding
            if pending and pending[-1][1] is None and pending[-1][2] == open_marks:
                directives.append(pending.pop()[0])
            open_marks = max(open_marks - 1, 0)
        elif tok.kind == "ident" and tok.value == "endfor" and _mark(toks, i - 1, "%{"):
            if directives:
                directives.pop()
        elif tok.kind == "ident" and tok.value == "for":
            names, j = set(), i + 1
            while j < len(toks) and toks[j].kind == "ident" and toks[j].value != "in":
                names.add(toks[j].value)
                j += 1 + _punct(toks, j + 1, ",")
            if j < len(toks) and toks[j].kind == "ident" and toks[j].value == "in" and names:
                if _punct(toks, i - 1, "{") or _punct(toks, i - 1, "["):
                    pending.append((names, len(frames), 0))
                elif _mark(toks, i - 1, "%{"):
                    pending.append((names, None, open_marks))
        elif (
            tok.kind == "ident" and not _punct(toks, i - 1, ".")
            and any(tok.value in names for names in (*frames, *directives))
        ):
            found.add(i)
    return found


def _reference(parts: list[str], iterators: frozenset[str] = frozenset()) -> str | None:
    head = parts[0]
    if head in iterators and parts[1] in ("key", "value"):
        return None
    if head in ("var", "local", "module") and len(parts) >= 2:
        return f"{head}.{parts[1]}"
    if head == "data" and len(parts) >= 3:
        return f"data.{parts[1]}.{parts[2]}"
    if head not in _BUILTIN and len(parts) >= 2 and _RESOURCE_TYPE.match(head):
        return f"{head}.{parts[1]}"
    return None


#: Attribute and block names whose map, block or list sets environment variables:
#: ``environment { variables = {...} }`` (Lambda), ``environment = [{name, value}]`` (ECS
#: container definitions), ``env { name value }`` (Kubernetes), ``app_settings`` (App Service).
_ENV_KEYS = frozenset({
    "environment", "variables", "env", "environment_variables", "env_vars", "app_settings",
})
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*\Z")


def _envs(toks: list[Tok]) -> list[dict[str, Any]]:
    """The environment variables a file sets, as ``env.NAME`` definitions whose doc is the
    definition they are set in. Only names: a value can be a secret and is never kept.

    A name is a key of a map or block opened right after one of ``_ENV_KEYS``
    (``variables = { DEBUG = "0" }``), or the literal ``name`` of an object or block there
    (``environment = [{ name = "DEBUG", value = "0" }]``, ``env { name = "DEBUG" }``)."""
    out: list[dict[str, Any]] = []
    # Each open bracket: its kind ("{" or "["), whether it is an env region, its entries.
    stack: list[tuple[str, bool, list[tuple[str, int, bool]], list[tuple[str, int]]]] = []
    scope = ""
    for i, tok in enumerate(toks):
        if tok.kind == "punct" and tok.value in "{[":
            if not stack:
                # A top-level block: `resource "type" "name" {` names its definition.
                j, labels = i - 1, []
                while j >= 0 and toks[j].kind in ("str", "ident"):
                    labels.insert(0, toks[j].value)
                    j -= 1
                kind, labels = (labels[0], labels[1:]) if labels else ("", [])
                wanted = _BLOCKS.get(kind)
                scope = (
                    _PREFIX[kind] + ".".join(labels)
                    if wanted is not None and len(labels) == wanted else ""
                )
            key = i - 2 if _entry(toks, i - 1) else i - 1
            opened_by = toks[key] if 0 <= key < len(toks) else None
            named = opened_by is not None and opened_by.kind in ("ident", "str") and (
                opened_by.value in _ENV_KEYS
            )
            # An object inside an env list (`environment = [{ ... }]`) is an env region too.
            inside = bool(stack) and stack[-1][0] == "[" and stack[-1][1]
            stack.append((tok.value, bool(stack) and named or inside, [], []))
            continue
        if tok.kind == "punct" and tok.value in "}]":
            if not stack:
                continue
            kind, env, entries, names = stack.pop()
            if env and kind == "{":
                if names:
                    out += [{"name": f"env.{n}", "kind": "env", "line": line, "doc": scope}
                            for n, line in names]
                else:
                    out += [
                        {"name": f"env.{k}", "kind": "env", "line": line, "doc": scope}
                        for k, line, nested in entries
                        if not nested and k not in _ENV_KEYS and _ENV_NAME.match(k)
                    ]
            continue
        if (
            stack and stack[-1][1] and stack[-1][0] == "{" and tok.kind in ("ident", "str")
            and _entry(toks, i + 1) and not _punct(toks, i - 1, ".")
            and (_punct(toks, i - 1, "{") or _punct(toks, i - 1, ",")
                 or toks[i - 1].line < tok.line)
        ):
            value = toks[i + 2] if i + 2 < len(toks) else None
            nested = value is not None and value.kind == "punct" and value.value in "{["
            stack[-1][2].append((tok.value, tok.line, nested))
            if tok.value == "name" and value is not None and value.kind == "str" and (
                _ENV_NAME.match(value.value)
            ):
                stack[-1][3].append((value.value, value.line))
    return out


def read(source: str) -> dict[str, Any]:
    toks = _Lexer(source).run()
    iterators = _iterators(toks)
    bound = _bound(toks)
    defs: list[dict[str, Any]] = []
    refs: list[dict[str, Any]] = []
    depth, nest, scope, kind = 0, 0, "", ""  # nest: open ( and [ inside the block body
    current: dict[str, Any] | None = None
    #: ``source`` and ``version`` of a module or required provider, for its doc
    settings: dict[int, dict[str, str]] = {}
    first_ref = 0  # the first reference of the current block, renamed with a provider alias
    required = 0  # the depth of the body of ``required_providers``, while inside it
    wanted_provider: dict[str, Any] | None = None  # the required provider being read
    providers = 0  # the depth of the body of a module's ``providers`` map, while inside it
    i = 0
    while i < len(toks):
        tok = toks[i]
        if tok.kind == "punct":
            if tok.value == "{":
                depth += 1
            elif tok.value == "}":
                depth = max(depth - 1, 0)
                if depth < required:
                    required = 0
                if depth <= required:
                    wanted_provider = None
                if depth < providers:
                    providers = 0
                if depth == 0:
                    scope, kind, current, nest = "", "", None, 0
            elif tok.value in "([":
                nest += 1
            elif tok.value in ")]":
                nest = max(nest - 1, 0)
            i += 1
            continue
        if depth == 0 and tok.kind == "ident":
            labels = []
            j = i + 1
            while j < len(toks) and toks[j].kind in ("str", "ident"):
                labels.append(toks[j].value)
                j += 1
            if _punct(toks, j, "{"):
                kind = tok.value
                wanted = _BLOCKS.get(kind)
                if wanted is not None and len(labels) == wanted:
                    scope = _PREFIX[kind] + ".".join(labels)
                    current = {"name": scope, "kind": kind, "line": tok.line, "doc": ""}
                    defs.append(current)
                else:
                    scope, current = "", None
                first_ref = len(refs)
                depth = 1
                i = j + 1
                continue
        if (
            kind == "terraform" and depth == 1 and nest == 0 and tok.kind == "ident"
            and tok.value == "required_providers" and _punct(toks, i + 1, "{")
        ):
            required = 2
            i += 1
            continue
        # A required provider: ``aws = { source = "hashicorp/aws", version = "~> 5.0" }``, or
        # the older ``aws = "~> 5.0"``.
        if (
            required and depth == required and nest == 0 and tok.kind == "ident"
            and _punct(toks, i + 1, "=")
        ):
            wanted_provider = {
                "name": f"provider.{tok.value}", "kind": "required_provider", "line": tok.line,
                "doc": "",
            }
            defs.append(wanted_provider)
            if i + 2 < len(toks) and toks[i + 2].kind == "str":
                settings[id(wanted_provider)] = {"version": toks[i + 2].value}
                i += 3
            else:
                i += 2
            continue
        if (
            wanted_provider is not None and depth == required + 1 and nest == 0
            and tok.kind == "ident" and tok.value in ("source", "version")
            and _entry(toks, i + 1) and i + 2 < len(toks) and toks[i + 2].kind == "str"
        ):
            settings.setdefault(id(wanted_provider), {})[tok.value] = toks[i + 2].value
            i += 3
            continue
        # The provider configurations of a module call: ``providers = { aws = aws.east }``.
        if (
            providers and depth == providers and tok.kind == "ident"
            and _entry(toks, i - 1)
        ):
            parts = _traversal(toks, i)
            refs.append({"name": "provider." + ".".join(parts[:2]), "line": tok.line,
                         "scope": scope})
            i += 2 * len(parts) - 1
            continue
        # An attribute: ``name =`` starting a line of the block body, outside brackets.
        if (
            tok.kind == "ident" and depth == 1 and nest == 0
            and _punct(toks, i + 1, "=")
            and (i == 0 or toks[i - 1].line < tok.line or _punct(toks, i - 1, "{"))
        ):
            if kind == "locals":
                scope = f"local.{tok.value}"
                defs.append({"name": scope, "kind": "local", "line": tok.line, "doc": ""})
                i += 2
                continue
            if (
                current is not None and tok.value == "description" and i + 2 < len(toks)
                and toks[i + 2].kind == "str"
            ):
                current["doc"] = toks[i + 2].value[:MAX_DOC]
            literal = toks[i + 2].value if i + 2 < len(toks) and toks[i + 2].kind == "str" else None
            if current is not None and kind == "module" and tok.value in ("source", "version"):
                if literal is not None:
                    settings.setdefault(id(current), {})[tok.value] = literal
            elif current is not None and kind == "provider" and tok.value == "alias":
                # ``provider "aws" { alias = "east" }`` is what ``provider = aws.east`` names
                if literal is not None and _IDENT.fullmatch(literal):
                    scope = current["name"] = f"{current['name']}.{literal}"
                    for ref in refs[first_ref:]:
                        ref["scope"] = scope
            elif kind in ("resource", "data") and tok.value == "provider" and i + 2 < len(toks):
                if toks[i + 2].kind == "ident":
                    parts = _traversal(toks, i + 2)
                    refs.append({"name": "provider." + ".".join(parts[:2]), "line": tok.line,
                                 "scope": scope})
                    i += 2 + 2 * len(parts) - 1
                    continue
            elif kind == "module" and tok.value == "providers" and _punct(toks, i + 2, "{"):
                providers = 2
        if tok.kind == "ident" and not _punct(toks, i - 1, "."):
            parts = _traversal(toks, i)
            name = _reference(parts, iterators) if len(parts) > 1 and i not in bound else None
            if name is not None:
                refs.append({"name": name, "line": tok.line, "scope": scope})
            i += max(1, 2 * len(parts) - 1)
            continue
        i += 1
    for d in defs:
        found = settings.get(id(d))
        if found and not d["doc"]:
            d["doc"] = ", ".join(
                f"{key} {found[key]}" for key in ("source", "version") if key in found
            )[:MAX_DOC]
    return {"defs": defs + _envs(toks), "refs": refs}


class _Language:
    interface = 1
    suffixes = (".tf",)
    read = staticmethod(read)


#: The entry point codeterrain loads (``codeterrain.languages``: ``terraform``).
language = _Language()
