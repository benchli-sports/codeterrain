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
    """``env_prefix`` and ``case_sensitive`` set as literals in a class body."""
    out: dict[str, Any] = {}
    for stmt in body:
        value = stmt.value if isinstance(stmt, ast.Assign | ast.AnnAssign) else None
        targets = (
            stmt.targets if isinstance(stmt, ast.Assign)
            else [stmt.target] if isinstance(stmt, ast.AnnAssign) else []
        )
        if any(isinstance(t, ast.Name) and t.id == "model_config" for t in targets):
            if isinstance(value, ast.Call):
                for kw in value.keywords:
                    if kw.arg in ("env_prefix", "case_sensitive"):
                        out[kw.arg] = _literal(kw.value)
            elif isinstance(value, ast.Dict):
                for key, item in zip(value.keys, value.values, strict=True):
                    if _literal(key) in ("env_prefix", "case_sensitive"):
                        out[_literal(key)] = _literal(item)
        elif isinstance(stmt, ast.ClassDef) and stmt.name == "Config":
            for inner in stmt.body:
                if isinstance(inner, ast.Assign):
                    for t in inner.targets:
                        if isinstance(t, ast.Name) and t.id in ("env_prefix", "case_sensitive"):
                            out[t.id] = _literal(inner.value)
    return out


def _fields(body: list[ast.stmt]) -> list[tuple[str, int, str | None]]:
    """(name, line, alias) of each annotated field of a class body."""
    out = []
    for stmt in body:
        if not isinstance(stmt, ast.AnnAssign) or not isinstance(stmt.target, ast.Name):
            continue
        name = stmt.target.id
        annotation = ast.unparse(stmt.annotation) if stmt.annotation else ""
        if name.startswith("_") or name == "model_config" or "ClassVar" in annotation:
            continue
        alias = None
        if isinstance(stmt.value, ast.Call):
            for kw in stmt.value.keywords:
                if kw.arg in ("validation_alias", "alias", "env") and isinstance(
                    _literal(kw.value), str
                ):
                    alias = _literal(kw.value)
                    if kw.arg == "validation_alias":
                        break
        out.append((name, stmt.lineno, alias))
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
    out: list[dict[str, Any]] = []
    for rel, cls in found:
        # The class and its bases, nearest first: settings and fields come from the nearest.
        mro = [c for c in classes.mro(rel, cls) if c in found]
        config: dict[str, Any] = {}
        for c in reversed(mro):
            config.update(_config(files.body(*c)))
        prefix = config.get("env_prefix") if isinstance(config.get("env_prefix"), str) else ""
        sensitive = config.get("case_sensitive") is True
        seen: set[str] = set()
        for c in mro:
            for field, line, alias in _fields(files.body(*c)):
                if field in seen:
                    continue
                seen.add(field)
                env = alias if alias is not None else f"{prefix}{field}"
                if env == name or not sensitive and env.lower() == name.lower():
                    module = code_static.module_name(c[0], index.src_package)
                    owner = ".".join(filter(None, (module, c[1].replace("::", "."))))
                    here = code_static.module_name(rel, index.src_package)
                    out.append({
                        "cls": ".".join(filter(None, (here, cls.replace("::", ".")))),
                        "field": f"{owner}.{field}", "file": c[0], "line": line, "env": env,
                        "prefix": prefix, "case_sensitive": sensitive,
                    })
    return out
