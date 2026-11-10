"""pydantic-settings fields as the environment variables they read, from ``ast`` only.

A class whose bases reach ``BaseSettings`` (``pydantic_settings`` or pydantic 1's) reads each of its
fields, its bases' included, from the environment variable named by the class's ``env_prefix`` and
the field's name, matched without regard to case unless ``case_sensitive`` is set; a field with a
literal ``alias`` or ``validation_alias`` (``env`` in pydantic 1) reads that name as written. The
prefix and the flag come from ``model_config = SettingsConfigDict(...)`` or an inner ``Config``,
inherited from the nearest base that sets them. Only literals count: a prefix built at run time
leaves the class's fields unmatched. Nothing is imported or run.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from codeterrain import code_static, impact

#: Where ``BaseSettings`` is defined, once a base resolves through the file's imports.
BASE_SETTINGS = frozenset({
    "pydantic_settings.BaseSettings", "pydantic_settings.main.BaseSettings",
    "pydantic.BaseSettings", "pydantic.v1.BaseSettings", "pydantic.env_settings.BaseSettings",
})
#: Settings classes looked at, and the files read for them, at most.
MAX_CLASSES = 500


def _literal(node: ast.expr | None) -> Any:
    return node.value if isinstance(node, ast.Constant) else None


def _config(body: list[ast.stmt]) -> dict[str, Any]:
    """``env_prefix`` and ``case_sensitive`` as a class body sets them: the literal, or None for
    one that is set but cannot be read (a name, or a ``**spread`` that may hold it)."""
    wanted = ("env_prefix", "case_sensitive")
    out: dict[str, Any] = {}
    for stmt in body:
        value = stmt.value if isinstance(stmt, ast.Assign | ast.AnnAssign) else None
        targets = (
            stmt.targets if isinstance(stmt, ast.Assign)
            else [stmt.target] if isinstance(stmt, ast.AnnAssign) else []
        )
        if any(isinstance(t, ast.Name) and t.id == "model_config" for t in targets):
            if isinstance(value, ast.Call):
                given = {kw.arg: _literal(kw.value) for kw in value.keywords if kw.arg in wanted}
                if any(kw.arg is None for kw in value.keywords):
                    out.update(dict.fromkeys(wanted))
                out.update(given)
            elif isinstance(value, ast.Dict):
                # In order: a later entry wins, a spread included.
                for key, item in zip(value.keys, value.values, strict=True):
                    if key is None:
                        out.update(dict.fromkeys(wanted))
                    elif _literal(key) in wanted:
                        out[_literal(key)] = _literal(item)
            elif value is not None:
                out.update(dict.fromkeys(wanted))
        elif isinstance(stmt, ast.ClassDef) and stmt.name == "Config":
            for inner in stmt.body:
                if isinstance(inner, ast.Assign):
                    for t in inner.targets:
                        if isinstance(t, ast.Name) and t.id in wanted:
                            out[t.id] = _literal(inner.value)
    return out


#: The keywords of ``Field`` that name what a field reads, the one that wins first.
_ALIAS_KEYWORDS = ("validation_alias", "alias", "env")


def _alias(node: ast.expr) -> list[str]:
    """The names an alias written as ``node`` reads: a string, or the strings of an
    ``AliasChoices(...)``; none for one that cannot be read (a name, an ``AliasPath``)."""
    if isinstance(_literal(node), str):
        return [_literal(node)]
    if isinstance(node, ast.Call) and code_static._dotted(node.func).rpartition(".")[2] == (
        "AliasChoices"
    ):
        return [_literal(arg) for arg in node.args if isinstance(_literal(arg), str)]
    return []


def _fields(body: list[ast.stmt]) -> list[tuple[str, int, list[str] | None]]:
    """(name, line, aliases) of each annotated field of a class body. ``aliases`` is None for a
    field without one, which reads its own name; else the names its alias reads, which is no
    name at all for an alias that is not a literal."""
    out = []
    for stmt in body:
        if not isinstance(stmt, ast.AnnAssign) or not isinstance(stmt.target, ast.Name):
            continue
        name = stmt.target.id
        annotation = ast.unparse(stmt.annotation) if stmt.annotation else ""
        if name.startswith("_") or name == "model_config" or "ClassVar" in annotation:
            continue
        # `Field(...)` as the default, or inside `Annotated[str, Field(...)]`.
        calls = [n for n in ast.walk(stmt.annotation) if isinstance(n, ast.Call)]
        if isinstance(stmt.value, ast.Call):
            calls.append(stmt.value)
        given = {kw.arg: kw.value for call in calls for kw in call.keywords}
        aliases = next((_alias(given[k]) for k in _ALIAS_KEYWORDS if k in given), None)
        out.append((name, stmt.lineno, aliases))
    return out


class _Files:
    """Class bodies by (file, qualified name), each file parsed once."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._bodies: dict[str, dict[str, list[ast.stmt]]] = {}

    def body(self, rel: str, cls: str) -> list[ast.stmt]:
        if rel not in self._bodies:
            found: dict[str, list[ast.stmt]] = {}
            try:
                data = (self.root / rel).read_bytes()
                tree = ast.parse(data) if len(data) <= code_static.MAX_FILE_BYTES else None
            except (OSError, SyntaxError, ValueError, RecursionError, MemoryError):
                tree = None

            def walk(nodes: list[ast.stmt], prefix: str) -> None:
                for node in nodes:
                    if isinstance(node, ast.ClassDef):
                        name = f"{prefix}{node.name}"
                        found[name] = node.body
                        walk(node.body, f"{name}.")

            if tree is not None:
                walk(tree.body, "")
            self._bodies[rel] = found
        return self._bodies[rel].get(cls.replace("::", "."), [])


def settings_fields(index: code_static.Index, root: Path, name: str) -> list[dict[str, Any]]:
    """The settings fields that read environment variable ``name``: ``cls`` and ``field`` (full
    names), ``file`` and ``line`` of the field, ``env`` (the name it reads) and ``prefix``."""
    classes = impact._Classes(index)
    roots: list[tuple[str, str]] = []
    # A base may be written under an import alias (`BaseSettings as Base`).
    for tail in sorted(index.aliases({"BaseSettings"})):
        for rel, cls in index.subclasses_named(tail):
            place = (rel, cls.replace(".", "::"))
            if place not in roots and any(b in BASE_SETTINGS for b in index.bases(rel, cls)):
                roots.append(place)
    found: list[tuple[str, str]] = []
    for place in roots:
        for c in [place, *classes.subclasses(*place)]:
            if c not in found and len(found) < MAX_CLASSES:
                found.append(c)
    files = _Files(root)
    configs: dict[tuple[str, str], dict[str, Any]] = {}

    def merged(place: tuple[str, str], depth: int = 0) -> dict[str, Any]:
        """The configuration of a class as pydantic merges it: that of each of its bases, in
        the order it names them (a later base wins), then its own."""
        if place not in configs:
            config: dict[str, Any] = {}
            if depth < impact.MAX_BASES:
                for base in classes._bases(*place):
                    config.update(merged(base, depth + 1))
            config.update(_config(files.body(*place)))
            configs[place] = config
        return configs[place]

    out: list[dict[str, Any]] = []
    for rel, cls in found:
        # The class and its bases, nearest first: settings and fields come from the nearest. A
        # base that is not itself a settings class (`class Common(BaseModel)`) still gives its
        # fields to the settings class below it.
        mro = classes.mro(rel, cls)
        config = merged((rel, cls))
        prefix = config.get("env_prefix", "")
        # A prefix that is not a literal is unknown, not empty: only a literal alias, which
        # ignores the prefix, can then be matched.
        known = isinstance(prefix, str)
        sensitive = config.get("case_sensitive") is True
        seen: set[str] = set()
        for c in mro:
            for field, line, aliases in _fields(files.body(*c)):
                if field in seen:
                    continue
                seen.add(field)
                if aliases is None:
                    aliases = [f"{prefix}{field}"] if known else []
                for env in aliases:
                    if env == name or not sensitive and env.lower() == name.lower():
                        module = code_static.module_name(c[0], index.src_package)
                        owner = ".".join(filter(None, (module, c[1].replace("::", "."))))
                        here = code_static.module_name(rel, index.src_package)
                        out.append({
                            "cls": ".".join(filter(None, (here, cls.replace("::", ".")))),
                            "field": f"{owner}.{field}", "file": c[0], "line": line, "env": env,
                            "prefix": prefix if known else "", "case_sensitive": sensitive,
                        })
                        break
    return out
