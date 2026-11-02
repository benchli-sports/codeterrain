"""TypeScript and JavaScript for codeterrain: the HTTP calls of a front end, read with tree-sitter.

``terrain calls`` and ``terrain callers`` read ``.ts``, ``.tsx``, ``.js``, ``.jsx``, ``.mjs``,
``.cjs``, ``.mts`` and ``.cts`` files through this pack when it is installed, in place of the
core's tokenizer. The facts are the same: ``<receiver>.<verb>(path, ...)``, ``fetch(path,
{method})`` and object literals with a ``url:`` path, each with its method, its path (``${expr}``
as ``{}``, a leading base URL dropped) or the expression when the path is not static.

A syntax tree makes exact what a tokenizer guesses: strings, templates and comments, type
arguments and assertions, object fields, spreads and computed keys. Names resolve through scopes:
a name is read from the declaration it refers to (a ``const`` in the function around the call, a
parameter, a module constant declared further down), not from every declaration of that name in
the file. A ``let`` or ``var`` bound to a string, a name declared twice with different values,
and a cycle stay unresolved rather than read as a wrong path.

The pack only parses the text it is given: it reads no other file and runs nothing. A file
tree-sitter cannot parse whole is reported with the lines of its syntax errors, and a call inside
one counts as unresolved, so a partial parse never looks whole.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import cache
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Any

# tree-sitter and its grammars load on the first file read, not when the pack is discovered:
# every terrain command discovers the packs, and most never read a front-end file.
if TYPE_CHECKING:
    from tree_sitter import Language, Node, Query

try:
    __version__ = version("codeterrain-typescript")
except PackageNotFoundError:  # run from a source tree
    __version__ = "0"

VERBS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})
METHODS = frozenset(verb.upper() for verb in VERBS)
#: Receivers whose ``.get(x)`` is an HTTP call even when ``x`` cannot be read statically; the
#: core's tokenizer uses the same set.
CLIENT_NAMES = frozenset(
    {"api", "apiClient", "axios", "axiosInstance", "client", "http", "httpClient", "request"}
)
GLOBALS = frozenset({"window", "globalThis", "self"})
#: A file larger than this is not read (the core's code index uses the same bound).
MAX_FILE_BYTES = 2_000_000
#: A string expression with more pieces or text than this is not a path this reader reads.
MAX_PIECES = 64
MAX_PATH_TEXT = 2048
RAW_LIMIT = 120
#: Steps one file may cost (nodes walked, scopes searched, pieces built), per byte plus a floor.
WORK_PER_BYTE = 32
WORK_FLOOR = 200_000
#: Lines of syntax errors named in a file's error.
MAX_ERROR_LINES = 5

#: The grammar each suffix is read with.
GRAMMAR_OF = {
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript", ".tsx": "tsx",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
}


@cache
def grammar(name: str) -> Language:
    from tree_sitter import Language

    if name == "javascript":
        import tree_sitter_javascript

        return Language(tree_sitter_javascript.language())
    import tree_sitter_typescript

    return Language(
        tree_sitter_typescript.language_tsx() if name == "tsx"
        else tree_sitter_typescript.language_typescript()
    )

#: Nodes that open a function scope (parameters, ``var``) and those that open a block scope.
_FUNCTIONS = frozenset({
    "program", "function_declaration", "function_expression", "function", "arrow_function",
    "method_definition", "generator_function_declaration", "generator_function",
})
_BLOCKS = frozenset({
    "statement_block", "for_statement", "for_in_statement", "catch_clause", "switch_body",
    "class_body",
})
#: Wrappers that say nothing about a value: ``(x)``, ``x as T``, ``x satisfies T``, ``x!``.
_TRANSPARENT = frozenset({
    "parenthesized_expression", "as_expression", "satisfies_expression", "non_null_expression",
})
_STRINGS = frozenset({"string", "template_string"})


def is_jsx_ampersand(node: Node) -> bool:
    """An ``ERROR`` the grammar makes of JSX text holding a bare ``&`` (``Staff & roles``):
    valid JSX that the grammar takes for the start of a character reference. It is text, not a
    syntax error."""
    parent = node.parent
    return (
        node.type == "ERROR" and parent is not None
        and parent.type in ("jsx_element", "jsx_fragment")
        and bool(node.children) and node.children[0].type == "&"
    )


def has_syntax_error(node: Node) -> bool:
    """Whether a syntax error lies in ``node``, a JSX ampersand (``is_jsx_ampersand``) aside."""
    stack = [node] if node.has_error else []
    while stack:
        current = stack.pop()
        if current.is_missing or current.type == "ERROR" and not is_jsx_ampersand(current):
            return True
        stack.extend(c for c in current.children if c.has_error)
    return False


def _inside_error(node: Node) -> bool:
    """Whether ``node`` lies inside a syntax error (a JSX ampersand aside)."""
    current = node.parent
    while current is not None:
        if current.type == "ERROR" and not is_jsx_ampersand(current):
            return True
        current = current.parent
    return False


#: The node kinds ``_Reader.walk`` visits: scopes, bindings and call sites.
_VISITED = _FUNCTIONS | _BLOCKS | frozenset({
    "lexical_declaration", "variable_declaration", "import_statement", "class_declaration",
    "call_expression", "object",
})
_QUERIES: dict[int, Query] = {}


def _query(language: Language) -> Query:
    """One capture per visited node kind the grammar has (the JavaScript grammar has no
    ``satisfies_expression``, for example), built once per grammar."""
    from tree_sitter import Query

    found = _QUERIES.get(id(language))
    if found is None:
        kinds = sorted(k for k in _VISITED if language.id_for_node_kind(k, True) is not None)
        found = _QUERIES[id(language)] = Query(language, " ".join(f"({k}) @node" for k in kinds))
    return found


class WorkBoundExceeded(Exception):
    """A file that costs more than ``WORK_PER_BYTE`` steps per byte to read."""


@dataclass
class Binding:
    kind: str  # "const", "let", "var" or "other" (a parameter, a pattern, an import, a function)
    value: Node | None
    scope: Scope


@dataclass
class Scope:
    parent: Scope | None
    function: bool
    names: dict[str, list[Binding]] = field(default_factory=dict)

    def bind(self, name: str, binding: Binding) -> None:
        self.names.setdefault(name, []).append(binding)


Pieces = list[tuple[str, str]]
_UNKNOWN = "unknown"


class _Reader:
    def __init__(self, data: bytes, language: Language) -> None:
        self.data = data
        self.language = language
        self.work = WORK_FLOOR + WORK_PER_BYTE * len(data)
        self.done: dict[int, Pieces | None] = {}
        self.busy: set[int] = set()

    def spend(self, steps: int = 1) -> None:
        self.work -= steps
        if self.work < 0:
            raise WorkBoundExceeded("too costly to read within the work bound; the file is skipped")

    def text(self, node: Node) -> str:
        return self.data[node.start_byte:node.end_byte].decode("utf-8", errors="replace")

    # --- scopes ------------------------------------------------------------------------------

    def walk(self, root: Node) -> list[tuple[Node, Scope, bool]]:
        """Every call and object literal with the scope it sits in and whether it lies inside a
        syntax error; every declaration is bound into its scope on the way."""
        sites: list[tuple[Node, Scope, bool]] = []
        top = Scope(None, True)
        # Only the nodes that open a scope, bind a name or may be a call are visited: the query
        # finds them in C, and one pass in source order (outer before inner) gives each the
        # innermost scope still open where it starts.
        from tree_sitter import QueryCursor

        nodes = QueryCursor(_query(self.language)).captures(root).get("node", [])
        nodes.sort(key=lambda n: (n.start_byte, -n.end_byte))
        open_scopes: list[tuple[int, Scope]] = [(len(self.data) + 1, top)]
        damaged = root.has_error
        for node in nodes:
            self.spend()
            while open_scopes[-1][0] <= node.start_byte:
                open_scopes.pop()
            scope = open_scopes[-1][1]
            kind = node.type
            if kind == "program":
                continue
            if kind in ("function_declaration", "generator_function_declaration",
                        "class_declaration"):
                name = node.child_by_field_name("name")
                if name is not None:
                    scope.bind(self.text(name), Binding("other", None, scope))
            if kind in _FUNCTIONS:
                scope = Scope(scope, True)
                self._bind_parameters(node, scope)
                open_scopes.append((node.end_byte, scope))
            elif kind in _BLOCKS:
                scope = Scope(scope, False)
                open_scopes.append((node.end_byte, scope))
            if kind in ("lexical_declaration", "variable_declaration"):
                self._bind_declaration(node, scope)
            elif kind == "for_in_statement":
                left = node.child_by_field_name("left")
                keyword = node.child_by_field_name("kind")
                if left is not None and keyword is not None:
                    # `for (var x of xs)` binds x in the function, `let` and `const` in the loop
                    target = scope
                    if keyword.type == "var":
                        while not target.function and target.parent is not None:
                            target = target.parent
                    for name in self._pattern_names(left):
                        target.bind(name, Binding("other", None, scope))
            elif kind == "catch_clause":
                parameter = node.child_by_field_name("parameter")
                if parameter is not None:
                    for name in self._pattern_names(parameter):
                        scope.bind(name, Binding("other", None, scope))
            elif kind == "import_statement":
                for name in self._import_names(node):
                    top.bind(name, Binding("other", None, top))
            elif kind in ("call_expression", "object"):
                sites.append((node, scope, damaged and _inside_error(node)))
        return sites

    def _bind_parameters(self, node: Node, scope: Scope) -> None:
        name = node.child_by_field_name("name")
        if name is not None and node.type in ("function_expression", "function"):
            scope.bind(self.text(name), Binding("other", None, scope))
        single = node.child_by_field_name("parameter")  # `url => ...`
        params = node.child_by_field_name("parameters")
        for holder in (single, params):
            if holder is not None:
                for found in self._pattern_names(holder):
                    scope.bind(found, Binding("other", None, scope))

    def _bind_declaration(self, node: Node, scope: Scope) -> None:
        keyword = node.children[0].type if node.children else ""
        kind = keyword if keyword in ("const", "let") else "var"
        target = scope
        if kind == "var":
            while not target.function and target.parent is not None:
                target = target.parent
        for declarator in node.named_children:
            if declarator.type != "variable_declarator":
                continue
            name = declarator.child_by_field_name("name")
            value = declarator.child_by_field_name("value")
            if name is None:
                continue
            if name.type == "identifier":
                target.bind(self.text(name), Binding(kind, value, scope))
            else:
                for found in self._pattern_names(name):
                    target.bind(found, Binding("other", None, scope))

    def _pattern_names(self, node: Node) -> Iterator[str]:
        """The names a parameter list or a binding pattern binds; default values and types are
        not names it binds."""
        stack = [node]
        while stack:
            current = stack.pop()
            self.spend()
            kind = current.type
            if kind in ("identifier", "shorthand_property_identifier_pattern"):
                yield self.text(current)
            elif kind in ("assignment_pattern", "object_assignment_pattern"):
                left = current.child_by_field_name("left")
                if left is not None:
                    stack.append(left)
            elif kind == "pair_pattern":
                value = current.child_by_field_name("value")
                if value is not None:
                    stack.append(value)
            elif kind in ("required_parameter", "optional_parameter"):
                pattern = current.child_by_field_name("pattern")
                if pattern is not None:
                    stack.append(pattern)
            elif kind in ("formal_parameters", "object_pattern", "array_pattern", "rest_pattern"):
                stack.extend(current.named_children)

    def _import_names(self, node: Node) -> Iterator[str]:
        stack = [c for c in node.named_children if c.type == "import_clause"]
        while stack:
            current = stack.pop()
            self.spend()
            if current.type == "identifier":
                yield self.text(current)
            elif current.type == "import_specifier":
                alias = current.child_by_field_name("alias") or current.child_by_field_name("name")
                if alias is not None:
                    yield self.text(alias)
            else:
                stack.extend(current.named_children)

    def lookup(self, name: str, scope: Scope) -> list[Binding] | None:
        current: Scope | None = scope
        while current is not None:
            self.spend()
            if name in current.names:
                return current.names[name]
            current = current.parent
        return None

    # --- values ------------------------------------------------------------------------------

    def name_pieces(self, name: str, scope: Scope) -> Pieces | None:
        """What the name ``name`` holds where ``scope`` reads it: its pieces when it is a
        constant this reader can read; ``unknown`` when it is bound to a string this reader
        cannot pin down; None when it is not a string constant (a parameter, an import, a base
        URL from the environment)."""
        bindings = self.lookup(name, scope)
        if not bindings:
            return None
        key = id(bindings)
        if key in self.done:
            return self.done[key]
        if key in self.busy:
            return [(_UNKNOWN, name)]
        constants = [b for b in bindings if b.kind == "const" and b.value is not None]
        others = [b for b in bindings if not (b.kind == "const" and b.value is not None)]
        result: Pieces | None
        if constants and others:
            result = [(_UNKNOWN, name)]
        elif others:
            mutable = [b for b in others if b.kind in ("let", "var") and b.value is not None]
            result = [(_UNKNOWN, name)] if any(self.stringish(b.value) for b in mutable) else None
        else:
            self.busy.add(key)
            found = [self.pieces(b.value, b.scope) for b in constants]
            self.busy.discard(key)
            readable = [p for p in found if p is not None and any(k != "expr" for k, _ in p)]
            if readable and len(readable) == len(found) and all(p == readable[0] for p in found):
                result = readable[0]
            elif readable or any(self.stringish(b.value) for b in constants):
                result = [(_UNKNOWN, name)]
            else:
                result = None
        self.done[key] = result
        return result

    def stringish(self, node: Node | None) -> bool:
        """Whether a string or template literal appears anywhere in ``node``."""
        stack = [node] if node is not None else []
        while stack:
            current = stack.pop()
            self.spend()
            if current.type in _STRINGS:
                return True
            stack.extend(current.named_children)
        return False

    def unwrap(self, node: Node) -> Node:
        while node.type in _TRANSPARENT and node.named_children:
            self.spend()
            node = node.named_children[0]
        if node.type == "type_assertion" and node.named_children:  # `<T>value`
            return self.unwrap(node.named_children[-1])
        return node

    def pieces(self, node: Node, scope: Scope) -> Pieces | None:
        """``node`` as ("text", s) / ("expr", source) / ("unknown", name) pieces, or None when
        it is not a string expression this reader reads: a string, a template, a name, or a
        ``+`` of those."""
        out: Pieces = []
        size = 0
        for more in self._operands(node, scope):
            if more is None:
                return None
            size += sum(len(value) for _, value in more)
            if len(out) + len(more) > MAX_PIECES or size > MAX_PATH_TEXT:
                return None
            out.extend(more)
        return out

    def _operands(self, node: Node, scope: Scope) -> Iterator[Pieces | None]:
        stack = [self.unwrap(node)]
        while stack:
            current = stack.pop()
            self.spend()
            kind = current.type
            if kind == "binary_expression":
                operator = current.child_by_field_name("operator")
                left = current.child_by_field_name("left")
                right = current.child_by_field_name("right")
                if operator is None or operator.type != "+" or left is None or right is None:
                    yield None
                    return
                stack.extend((self.unwrap(right), self.unwrap(left)))
            elif kind == "string":
                yield [("text", decode(self.text(current)[1:-1]))]
            elif kind == "template_string":
                yield self._template(current, scope)
            elif kind == "identifier":
                name = self.text(current)
                yield self.name_pieces(name, scope) or [("expr", name)]
            elif kind in ("member_expression", "number"):
                yield [("expr", self.text(current))]
            else:
                yield None
                return

    def _template(self, node: Node, scope: Scope) -> Pieces:
        out: Pieces = []
        at = node.start_byte + 1
        for child in node.named_children:
            if child.type != "template_substitution":
                continue
            if child.start_byte > at:
                out.append(("text", decode(self.data[at:child.start_byte].decode(
                    "utf-8", errors="replace"))))
            inner = [c for c in child.named_children if c.type != "comment"]
            expr = self.unwrap(inner[0]) if len(inner) == 1 else None
            found = None
            if expr is not None and expr.type == "identifier":
                found = self.name_pieces(self.text(expr), scope)
            out.extend(found or [("expr", " ".join(self.text(child)[2:-1].split()))])
            at = child.end_byte
        end = node.end_byte - 1
        if end > at:
            out.append(("text", decode(self.data[at:end].decode("utf-8", errors="replace"))))
        return out

    def path(self, node: Node, scope: Scope) -> str | None:
        pieces = self.pieces(node, scope)
        return None if pieces is None else path_from_pieces(pieces)

    def external(self, node: Node) -> bool:
        """``fetch('https://...')``: an absolute URL literal is not a call to the backend."""
        node = self.unwrap(node)
        if node.type == "string":
            return "://" in decode(self.text(node)[1:-1])
        if node.type == "template_string":
            first = next((c for c in node.named_children), None)
            stop = first.start_byte if first is not None and \
                first.type == "template_substitution" else node.end_byte - 1
            return "://" in self.data[node.start_byte + 1:stop].decode("utf-8", errors="replace")
        return False

    def raw(self, node: Node) -> str:
        return " ".join(self.text(node).split())[:RAW_LIMIT]

    # --- call sites --------------------------------------------------------------------------

    def fields(self, node: Node, scope: Scope) -> tuple[dict[str, Node], bool]:
        """Top-level fields of an object literal, and whether an entry could set any field: a
        ``...spread`` or a computed key that is not one string. Such an entry drops the fields
        before it; those after it are kept."""
        found: dict[str, Node] = {}
        overwrites = False
        for entry in node.named_children:
            self.spend()
            if entry.type == "spread_element":
                found.clear()
                overwrites = True
            elif entry.type == "shorthand_property_identifier":
                found[self.text(entry)] = entry
            elif entry.type == "pair":
                key = entry.child_by_field_name("key")
                value = entry.child_by_field_name("value")
                if key is None or value is None:
                    continue
                name = self.key(key)
                if name is None:
                    found.clear()
                    overwrites = True
                else:
                    found[name] = value
        return found, overwrites

    def key(self, key: Node) -> str | None:
        if key.type in ("property_identifier", "number"):
            return self.text(key)
        if key.type == "string":
            return decode(self.text(key)[1:-1])
        if key.type == "computed_property_name":
            inner = [self.unwrap(c) for c in key.named_children]
            if len(inner) == 1 and inner[0].type == "string":
                return decode(self.text(inner[0])[1:-1])
        return None

    def method(self, value: Node | None, scope: Scope, default: str | None) -> str:
        if value is None:
            return default or "?"
        if value.type == "shorthand_property_identifier":
            pieces = self.name_pieces(self.text(value), scope)
        else:
            pieces = self.pieces(value, scope)
        if pieces and all(kind == "text" for kind, _ in pieces):
            method = "".join(text for _, text in pieces).upper()
            return method if method in METHODS else "?"
        return "?"

    def calls(self, sites: list[tuple[Node, Scope, bool]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for node, scope, broken in sites:
            call = self._client(node, scope) if node.type == "call_expression" else None
            if call is None and node.type == "call_expression":
                call = self._fetch(node, scope)
            if call is None and node.type == "object":
                call = self._object(node, scope)
            if call is None:
                continue
            method, path, line, style, arg = call
            if broken or has_syntax_error(node):  # a syntax error in the call or around it
                path = None
            out.append({
                "method": method, "path": path or "", "line": line, "style": style,
                "resolved": path is not None, "raw": "" if path is not None else self.raw(arg),
            })
        out.sort(key=lambda c: c["line"])
        return out

    def _callee(self, node: Node) -> Node | None:
        """What ``node`` calls. The TypeScript grammar reads ``await api.get<T>(path)`` as a
        call of ``await api.get`` (with type arguments), so the ``await`` is looked through."""
        function = node.child_by_field_name("function")
        if function is not None and function.type == "await_expression" and \
                node.child_by_field_name("type_arguments") is not None:
            inner = [c for c in function.named_children if c.type != "comment"]
            return inner[0] if len(inner) == 1 else None
        return function

    def _arguments(self, node: Node) -> list[Node] | None:
        args = node.child_by_field_name("arguments")
        if args is None or args.type != "arguments":
            return None
        found = [c for c in args.named_children if c.type != "comment"]
        return found or None

    def _client(self, node: Node, scope: Scope) -> tuple[str, str | None, int, str, Node] | None:
        """``<receiver>.<verb>[<T>](path, ...)``."""
        function = self._callee(node)
        if function is None or function.type != "member_expression":
            return None
        prop = function.child_by_field_name("property")
        receiver = self._receiver(function.child_by_field_name("object"))
        if prop is None or prop.type != "property_identifier" or receiver is None:
            return None
        verb = self.text(prop)
        args = self._arguments(node)
        if verb not in VERBS or args is None:
            return None
        path = self.path(args[0], scope)
        if path is None and (self.external(args[0]) or receiver not in CLIENT_NAMES):
            return None
        return verb.upper(), path, prop.start_point[0] + 1, "client", args[0]

    def _receiver(self, node: Node | None) -> str | None:
        """The last name of a plain receiver chain (``this.api`` is ``api``), or None."""
        if node is None:
            return None
        if node.type in ("identifier", "this"):
            return self.text(node)
        last = None
        current = node
        while current.type == "member_expression":
            self.spend()
            prop = current.child_by_field_name("property")
            if prop is None or prop.type != "property_identifier":
                return None
            last = last or self.text(prop)
            current = current.child_by_field_name("object")
            if current is None:
                return None
        return last if current.type in ("identifier", "this") else None

    def _fetch(self, node: Node, scope: Scope) -> tuple[str, str | None, int, str, Node] | None:
        """``fetch(path, {method})`` and ``window.fetch(...)``; ``client.fetch`` is someone
        else's method."""
        function = self._callee(node)
        if function is None:
            return None
        if function.type == "identifier" and self.text(function) == "fetch":
            name = function
        elif function.type == "member_expression":
            name = function.child_by_field_name("property")
            owner = function.child_by_field_name("object")
            if name is None or self.text(name) != "fetch" or owner is None or \
                    owner.type != "identifier" or self.text(owner) not in GLOBALS:
                return None
        else:
            return None
        args = self._arguments(node)
        if args is None:
            return None
        path = self.path(args[0], scope)
        if path is None and self.external(args[0]):
            return None
        options = self.unwrap(args[1]) if len(args) > 1 else None
        fields: dict[str, Node] = {}
        default: str | None
        if options is None:
            default = "GET"
        elif options.type == "object":
            fields, overwrites = self.fields(options, scope)
            default = None if overwrites else "GET"
        else:
            default = None
        method = self.method(fields.get("method"), scope, default)
        return method, path, name.start_point[0] + 1, "fetch", args[0]

    def _object(self, node: Node, scope: Scope) -> tuple[str, str | None, int, str, Node] | None:
        """``{ url: path, method: 'post' }`` and the shorthand ``{ url, method: 'post' }``."""
        fields, overwrites = self.fields(node, scope)
        value = fields.get("url")
        if value is None:
            return None
        if value.type == "shorthand_property_identifier":
            pieces = self.name_pieces(self.text(value), scope)
            path = None if pieces is None else path_from_pieces(pieces)
            line = value.start_point[0] + 1
        else:
            path = self.path(value, scope)
            line = (value.parent or value).start_point[0] + 1
        if path is None:
            return None  # an object with a `url` that is not a path is not evidence of a call
        method = self.method(fields.get("method"), scope, None if overwrites else "GET")
        return method, path, line, "object", value


def path_from_pieces(pieces: Pieces) -> str | None:
    """The path the pieces spell, ``${expr}`` as ``{}``; a leading expression followed by ``/``
    is a base URL and dropped. None unless the result starts with ``/``, and None when a piece is
    a name bound to a string this reader cannot read."""
    if any(kind == _UNKNOWN for kind, _ in pieces):
        return None
    if len(pieces) > 1 and pieces[0][0] == "expr" and pieces[1][0] == "text" and \
            pieces[1][1].startswith("/"):
        pieces = pieces[1:]
    path = "".join(value if kind == "text" else "{}" for kind, value in pieces)
    if not path.startswith("/") or path.startswith("//"):
        return None
    return path


_SIMPLE_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
_ESCAPE = re.compile(
    r"\\(?:x([0-9A-Fa-f]{2})|u([0-9A-Fa-f]{4})|u\{([0-9A-Fa-f]{1,6})\}|(\r\n|[\s\S]))"
)


def decode(text: str) -> str:
    """The value of a string's or template text's source: ``\\n`` and the like, ``\\xHH``,
    ``\\uHHHH``, ``\\u{H...}``, a line continuation (nothing), any other character as itself. A
    malformed hex escape keeps its letters, so the path it spells cannot match a route by
    accident."""
    def one(match: re.Match[str]) -> str:
        hexed = next((g for g in match.groups()[:3] if g is not None), None)
        if hexed is not None:
            code = int(hexed, 16)
            return chr(code) if code <= 0x10FFFF else match.group(0)[1:]
        char = match.group(4)
        if char in ("\n", "\r\n", "\r", "\u2028", "\u2029"):
            return ""
        return _SIMPLE_ESCAPES.get(char, char)

    return _ESCAPE.sub(one, text) if "\\" in text else text


def _syntax_errors(root: Node, reader: _Reader) -> list[int]:
    lines: list[int] = []
    stack = [root]
    while stack and len(lines) < MAX_ERROR_LINES:
        node = stack.pop()
        reader.spend()
        if is_jsx_ampersand(node):
            continue
        if node.type == "ERROR" or node.is_missing:
            line = node.start_point[0] + 1
            if line not in lines:
                lines.append(line)
            continue
        if node.has_error:
            stack.extend(reversed(node.children))
    return sorted(lines)


def read_calls(source: str, suffix: str) -> dict[str, Any]:
    """The HTTP calls of one file's text: ``{"calls": [...], "error": str | None}``."""
    data = source.encode("utf-8", errors="replace")
    if len(data) > MAX_FILE_BYTES:
        return {"calls": [], "error": f"file larger than {MAX_FILE_BYTES} bytes; not read"}
    from tree_sitter import Parser

    language = grammar(GRAMMAR_OF[suffix])
    tree = Parser(language).parse(data)
    reader = _Reader(data, language)
    try:
        calls = reader.calls(reader.walk(tree.root_node))
        error = None
        lines = ", ".join(str(n) for n in _syntax_errors(tree.root_node, reader))
        if lines:
            error = f"syntax errors at line {lines}; calls there are unresolved"
    except (WorkBoundExceeded, RecursionError) as exc:
        return {"calls": [], "error": f"{type(exc).__name__}: {exc}"}
    return {"calls": calls, "error": error}


class _Language:
    interface = 1
    call_suffixes = tuple(GRAMMAR_OF)

    @staticmethod
    def read_calls(source: str, suffix: str) -> dict[str, Any]:
        return read_calls(source, suffix)


language = _Language()
