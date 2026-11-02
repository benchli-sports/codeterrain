"""Language packs: installed distributions (``codeterrain-terraform`` and the like) that add the
definitions and references of another language to ``terrain def`` and ``terrain refs``.

A pack registers one entry point in the ``codeterrain.languages`` group, named after its language,
pointing at an object with these attributes:

- ``interface``: the version of this contract it implements (``INTERFACE``);
- ``suffixes`` and ``read(source: str) -> dict``, for ``def`` and ``refs``: the file suffixes it
  reads (``(".tf",)``) and the facts of one file's text, ``{"defs": [...], "refs": [...]}``, each
  definition ``{"name", "kind", "line", "doc"}`` and each reference ``{"name", "line", "scope"}``
  (``scope`` is the name of the definition the reference sits in, or ``""``);
- ``call_suffixes`` and ``read_calls(source: str, suffix: str) -> dict``, for ``calls`` and
  ``callers``: the front-end file suffixes it reads in place of the built-in reader, and the HTTP
  calls of one file, ``{"calls": [...], "error": str | None}`` in the shape
  ``calls_static.CALL_FACT_TYPES`` describes.

A pack offers either pair or both. ``read`` and ``read_calls`` are pure functions of the text:
they read no other file and run nothing. The core walks
the repository, caches the facts of each file by content, checks every field the pack returns and
answers the queries, so a pack never sees the cache or the repository. A pack is code that runs in
this process, so the import path is first cleared of every entry a repository being read controls
(``executables.drop_distrusted_imports``), and a pack whose distribution or module still lies inside
one is refused before it is imported.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import importlib.util
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codeterrain import code_static
from codeterrain.cache import Cache, ViewRecord
from codeterrain.executables import distrusted, drop_distrusted_imports

#: The version of the pack contract this core implements.
INTERFACE = 1
GROUP = "codeterrain.languages"
_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")
_SUFFIX = re.compile(r"\.[A-Za-z0-9_.-]{1,16}\Z")

MAX_FILE_BYTES = code_static.MAX_FILE_BYTES
MAX_FILE_FACTS = code_static.MAX_FILE_FACTS
#: Longest name, kind, scope or doc text kept from a pack.
MAX_TEXT = 1_000
MAX_MATCHES = code_static.MAX_MATCHES

DEF_TYPES = {"name": str, "kind": str, "line": "int", "doc": str}
REF_TYPES = {"name": str, "line": "int", "scope": str}


@dataclass(frozen=True)
class Language:
    name: str
    version: str
    suffixes: tuple[str, ...]
    read: Callable[[str], Any] | None
    call_suffixes: tuple[str, ...] = ()
    read_calls: Callable[[str, str], Any] | None = None

    @property
    def view(self) -> str:
        return f"lang-{self.name}"

    @property
    def object_kind(self) -> str:
        """Facts are cached per pack version: a new version of a pack re-reads every file."""
        tag = hashlib.sha256(f"{INTERFACE}\0{self.version}".encode()).hexdigest()[:12]
        return f"lang-{self.name}-{tag}"


def _origins(module: str) -> list[Path] | None:
    """Every place the top-level package of ``module`` would be imported from (all the parts of
    a namespace package), found without importing it; None when it cannot be found."""
    try:
        spec = importlib.util.find_spec(module.partition(".")[0])
    except (ImportError, ValueError):
        return None
    if spec is None:
        return None
    places = [Path(p) for p in spec.submodule_search_locations or []]
    if spec.origin and spec.origin not in ("built-in", "frozen", "namespace"):
        places.append(Path(spec.origin))
    return places or None


def _load(entry: importlib.metadata.EntryPoint, dist: importlib.metadata.Distribution) -> Any:
    """The object ``entry`` names; raises ``ValueError`` for a pack the repository could have
    planted (its distribution, or the module the name would import, inside it)."""
    located = Path(str(dist.locate_file("")))
    if not located.is_absolute() or distrusted(located):
        raise ValueError("installed inside the repository; not loaded")
    origins = _origins(entry.module)
    if origins is None:
        raise ValueError(f"module {entry.module} not found")
    if any(distrusted(origin) for origin in origins):
        raise ValueError(
            f"module {entry.module} resolves inside the repository; not loaded"
        )
    return entry.load()


def _language(name: str, version: str, found: Any) -> Language:
    interface = getattr(found, "interface", None)
    if interface != INTERFACE:
        raise ValueError(f"implements interface {interface!r}; this codeterrain reads {INTERFACE}")
    pairs = []
    for suffixes_name, read_name in (("suffixes", "read"), ("call_suffixes", "read_calls")):
        suffixes = getattr(found, suffixes_name, None)
        read = getattr(found, read_name, None)
        if suffixes is None and read is None:
            pairs.append(((), None))
            continue
        if (
            not isinstance(suffixes, tuple | list) or not suffixes
            or not all(isinstance(s, str) and _SUFFIX.match(s) for s in suffixes)
        ):
            raise ValueError(
                f"{suffixes_name} must be a non-empty list of file suffixes such as '.tf'"
            )
        if not callable(read):
            raise ValueError(f"has {suffixes_name} but no {read_name} function")
        pairs.append((tuple(suffixes), read))
    if all(read is None for _, read in pairs):
        raise ValueError("has no read function")
    (suffixes, read), (call_suffixes, read_calls) = pairs
    return Language(name, version, suffixes, read, call_suffixes, read_calls)


def discover(
    path: list[str] | None = None,
) -> tuple[list[Language], list[str]]:
    """The installed language packs, and a line for each one that was refused. The import path
    is first cleared of what the repositories being read control, so neither a pack nor anything
    it imports later can be resolved inside one."""
    drop_distrusted_imports()
    found: dict[str, Language] = {}
    problems: list[str] = []
    for dist in importlib.metadata.distributions(**({"path": path} if path is not None else {})):
        for entry in dist.entry_points:
            if entry.group != GROUP or entry.name in found:
                continue
            label = f"language pack {entry.name!r} ({dist.metadata['Name']})"
            if not _NAME.match(entry.name):
                problems.append(f"{label}: the name must be lowercase letters, digits or _")
                continue
            try:
                found[entry.name] = _language(
                    entry.name, dist.version or "", _load(entry, dist)
                )
            except Exception as exc:  # a broken pack must not stop the other answers
                problems.append(f"{label}: {exc}")
    return sorted(found.values(), key=lambda lang: lang.name), problems


# --- facts ---------------------------------------------------------------------------------------


def _typed(row: Any, types: dict[str, Any]) -> bool:
    if not isinstance(row, dict) or set(row) != set(types):
        return False
    for key, kind in types.items():
        value = row[key]
        if kind == "int":
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                return False
        elif not isinstance(value, kind) or len(value) > MAX_TEXT:
            return False
    return True


def _valid_facts(facts: Any) -> bool:
    return (
        isinstance(facts, dict)
        and set(facts) == {"defs", "refs", "error"}
        and (facts["error"] is None or isinstance(facts["error"], str))
        and isinstance(facts["defs"], list)
        and isinstance(facts["refs"], list)
        and len(facts["defs"]) + len(facts["refs"]) <= MAX_FILE_FACTS
        and all(_typed(d, DEF_TYPES) for d in facts["defs"])
        and all(_typed(r, REF_TYPES) for r in facts["refs"])
    )


def _failed(error: str) -> dict[str, Any]:
    return {"defs": [], "refs": [], "error": error}


def read_file(language: Language, data: bytes) -> dict[str, Any]:
    """The checked facts of one file: a pack that raises or returns anything malformed gives a
    file error, never a crash or an unchecked field."""
    if len(data) > MAX_FILE_BYTES:
        return _failed(f"file larger than {MAX_FILE_BYTES} bytes; not indexed")
    assert language.read is not None
    try:
        got = language.read(data.decode("utf-8", errors="replace"))
    except Exception as exc:  # the pack's own failure on one file
        return _failed(f"{type(exc).__name__}: {str(exc)[:MAX_TEXT]}")
    facts = {
        "defs": got.get("defs") if isinstance(got, dict) else None,
        "refs": got.get("refs") if isinstance(got, dict) else None,
        "error": None,
    }
    if not _valid_facts(facts):
        return _failed(f"language pack {language.name} returned malformed facts")
    return facts


def _valid_view(data: Any, language: Language) -> bool:
    return (
        isinstance(data, dict)
        and data.get("format") == language.object_kind
        and isinstance(data.get("defs"), list)
        and isinstance(data.get("refs"), list)
        and isinstance(data.get("errors"), list)
        and all(_located(d, DEF_TYPES) for d in data["defs"])
        and all(_located(r, REF_TYPES) for r in data["refs"])
    )


def _located(row: Any, types: dict[str, Any]) -> bool:
    """A view row: the fact with the ``file`` it was read from."""
    return (
        isinstance(row, dict) and isinstance(row.get("file"), str)
        and _typed({k: v for k, v in row.items() if k != "file"}, types)
    )


def build(cache: Cache, language: Language, force: bool = False) -> ViewRecord:
    """The view of one language: its definitions and references in every file it reads,
    rebuilt incrementally when a file changed."""
    fingerprint = cache.scan(None, language.suffixes)
    record = cache.read_view(language.view)
    if (
        record is not None and not force and record.fingerprint == fingerprint
        and (
            record := cache.served(
                record, language.object_kind, lambda d: _valid_view(d, language)
            )
        ) is not None
    ):
        cache.flush()
        return record
    objects = cache.load_objects(language.object_kind)
    added = False
    for rel, sha in fingerprint.items():
        if not _valid_facts(objects.get(sha)):
            with open(cache.root / rel, "rb") as f:
                # never more than the cap, whatever the file's size
                objects[sha] = read_file(language, f.read(MAX_FILE_BYTES + 1))
            added = True
    if added:
        cache.save_objects(language.object_kind, objects)
    defs: list[dict[str, Any]] = []
    refs: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for rel in sorted(fingerprint):
        facts = objects[fingerprint[rel]]
        if facts["error"]:
            errors.append({"file": rel, "error": facts["error"]})
        defs.extend({**d, "file": rel} for d in facts["defs"])
        refs.extend({**r, "file": rel} for r in facts["refs"])
    record = cache.write_view(
        language.view,
        {"format": language.object_kind, "defs": defs, "refs": refs, "errors": errors},
        fingerprint,
        sign=True,  # built from checked facts: a warm read need not check every row again
    )
    cache.flush()
    return record


# --- queries -------------------------------------------------------------------------------------


def find(data: dict[str, Any], name: str, kind: str | None = None) -> list[dict[str, Any]]:
    """The definitions named ``name``, or whose last dotted part is ``name`` (``logs`` finds
    ``aws_s3_bucket.logs``), of ``kind`` if given; at most ``MAX_MATCHES``."""
    defs = [d for d in data["defs"] if kind is None or d["kind"] == kind]
    exact = [d for d in defs if d["name"] == name]
    found = exact or [d for d in defs if d["name"].endswith(f".{name}")]
    return found[:MAX_MATCHES]


def references(data: dict[str, Any], names: Iterable[str]) -> list[dict[str, Any]]:
    wanted = set(names)
    return [r for r in data["refs"] if r["name"] in wanted]
