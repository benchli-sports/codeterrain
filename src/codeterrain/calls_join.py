"""Front-end calls joined to backend routes: which route a call hits, and who calls a route.

Paths are compared segment by segment after the query string and a trailing ``/`` are dropped. A
route parameter (``{item_id}``, ``{slug:path}``) matches any segment (``:path`` the rest of the
path), and one inside a segment (``{name}.json``) any text there; a ``{}`` in a call path (an
expression the scanner could not read) matches any text inside its segment. When several routes
match, the most literal match wins, then the first route in registration order, which is the order
the application tries them.

The join reads working copies, so ``repo_state`` reports for each input checkout whether it is
behind its upstream (as of its last fetch; nothing is fetched) or holds uncommitted changes: a stale
or dirty checkout can show a gap the deployed code does not have.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from codeterrain import calls_static
from codeterrain.cache import Cache, git_env
from codeterrain.config import ClientRepo, ConfigError, client_config
from codeterrain.executables import resolve_executable

if TYPE_CHECKING:
    from codeterrain.languages import Language

_PARAM = re.compile(r"^\{[^{}:]+(?::([^{}]+))?\}$")
_INNER_PARAM = re.compile(r"\{[^{}:]+(?::[^{}]+)?\}")


def segments(path: str) -> list[str]:
    """The segments of ``path`` without its query and fragment. Only the leading and a trailing
    ``/`` are dropped: ``/v1//items`` is not ``/v1/items`` to the application."""
    path = re.split(r"[?#]", path, maxsplit=1)[0]
    parts = path.split("/")
    if parts and not parts[0]:
        parts = parts[1:]
    if parts and not parts[-1]:
        parts = parts[:-1]
    return parts


#: A path split for matching: its segments and whether one is a ``{x:path}`` parameter.
Split = tuple[tuple[str, ...], bool]


def _split(path: str) -> Split | None:
    """``segments(path)`` and whether one of them is a ``{x:path}`` parameter; None for a path
    over ``MAX_PATH`` characters or ``MAX_SEGMENTS`` segments, which matches nothing. Splits are
    made once per join and not kept after it: call paths come from front-end checkouts."""
    if len(path) > MAX_PATH:
        return None
    parts = tuple(segments(path))
    if len(parts) > MAX_SEGMENTS:
        return None
    return parts, any(_is_rest(part) for part in parts)


def _glob(literals: list[str], text: str, non_empty: bool) -> bool:
    """Whether ``text`` is ``literals`` with a wildcard between each pair (``non_empty``: each
    wildcard takes at least one character). Each literal is taken at its leftmost place after the
    previous one, which is enough for wildcards that take any text, so no input backtracks: the
    cost is linear in ``text`` per literal, where a ``.*a.*a...`` regex is exponential."""
    if len(literals) == 1:
        return text == literals[0]
    gap = 1 if non_empty else 0
    first, last = literals[0], literals[-1]
    end = len(text) - len(last)
    if not text.startswith(first) or end < len(first) or text[end:] != last:
        return False
    pos = len(first)
    for literal in literals[1:-1]:
        found = text.find(literal, pos + gap, end)
        if found < 0:
            return False
        pos = found + len(literal)
    return end - pos >= gap


#: Two-sided wildcard segments longer than this are compared the one-sided way (the call's pattern
#: against the route's template text): the overlap test below costs the product of the lengths.
MAX_OVERLAP = 64
_STAR, _ONE = object(), object()


def _elements(literals: list[str], non_empty: bool) -> list[Any]:
    """``literals`` as characters with a wildcard between each pair: ``_STAR`` (any text), or
    ``_ONE`` then ``_STAR`` for one character or more."""
    out: list[Any] = list(literals[0])
    for literal in literals[1:]:
        out += [_ONE, _STAR] if non_empty else [_STAR]
        out += list(literal)
    return out


def _patterns_overlap(call: list[str], route: list[str], template: str) -> bool:
    """Whether some text matches both the call segment's pattern (``{}``: any text) and the route
    segment's (a parameter: one character or more), as ``report-{}.json`` and ``{name}.json`` do.
    A table over the two patterns' positions, each step consuming from one side or both."""
    a, b = _elements(call, non_empty=False), _elements(route, non_empty=True)
    if len(a) > MAX_OVERLAP or len(b) > MAX_OVERLAP:
        return _glob(call, template, non_empty=False)
    seen = [[False] * (len(b) + 1) for _ in range(len(a) + 1)]
    seen[0][0] = True
    for i in range(len(a) + 1):
        for j in range(len(b) + 1):
            if not seen[i][j]:
                continue
            x = a[i] if i < len(a) else None
            y = b[j] if j < len(b) else None
            if x is _STAR:
                seen[i + 1][j] = True  # the call's wildcard takes nothing more
                if y is not None:
                    seen[i][j + 1] = True  # ... or takes what the route's next element spells
            if y is _STAR:
                seen[i][j + 1] = True
                if x is not None and x is not _STAR:
                    seen[i + 1][j] = True
            if x is not None and y is not None and x is not _STAR and y is not _STAR and (
                x is _ONE or y is _ONE or x == y
            ):
                seen[i + 1][j + 1] = True
    return seen[len(a)][len(b)]


def _call_literals(segment: str) -> list[str] | None:
    """The literal text around each ``{}`` of a call segment, None for a literal segment."""
    return segment.split("{}") if "{}" in segment else None


def _route_literals(segment: str) -> list[str] | None:
    """The literal text around each parameter inside a route segment (``{name}.json``), None for
    a literal one. The parameter takes what Starlette's default convertor does: one or more
    characters (never a ``/``, which a segment cannot hold)."""
    if not _INNER_PARAM.search(segment):
        return None
    return _INNER_PARAM.split(segment)


#: Paths longer than these match nothing: no real route is this long, and the match below costs
#: segments of the call times segments of the route, each pair at most the length of the two.
MAX_SEGMENTS = 256
MAX_PATH = 2048


def _is_rest(part: str) -> bool:
    param = _PARAM.match(part)
    return bool(param and param.group(1) == "path")


def score(call_path: str, route_path: str) -> int | None:
    """How literally ``call_path`` matches ``route_path`` (higher is closer), None for no match."""
    call, route = _split(call_path), _split(route_path)
    return None if call is None or route is None else _score(call[0], route)


def _score(call: tuple[str, ...], split: Split) -> int | None:
    route, rest = split
    if len(call) != len(route) and not rest:
        return None  # without a `{x:path}`, each route segment takes exactly one call segment
    # best[r][c]: the score of call[c:] against route[r:], filled from the end, so a route made of
    # many `{x:path}` parameters costs len(call) * len(route) steps, not one per way to split.
    n = len(call)
    best: list[list[int | None]] = [[None] * (n + 1) for _ in range(len(route) + 1)]
    best[len(route)][n] = 0
    for r in range(len(route) - 1, -1, -1):
        part = route[r]
        param = _PARAM.match(part)
        if param and param.group(1) == "path":
            # One or more segments, then whatever the route still requires after them.
            tail: int | None = None
            for c in range(n - 1, -1, -1):
                after = best[r + 1][c + 1]
                if after is not None and (tail is None or after > tail):
                    tail = after
                best[r][c] = None if tail is None else 1 + tail
            continue
        for c in range(n - 1, -1, -1):
            after = best[r + 1][c + 1]
            if after is None:
                continue
            here = _segment_score(call[c], part, param is not None)
            if here is not None:
                best[r][c] = here + after
    return best[0][0]


def _segment_score(mine: str, part: str, param: bool) -> int | None:
    """How literally the call segment ``mine`` matches the route segment ``part``; ``param`` when
    ``part`` is a whole-segment parameter."""
    if param:
        if not mine:
            return None  # a parameter takes at least one character
        return 3 if mine == "{}" else 2
    pattern = _call_literals(mine)
    inner = _route_literals(part)
    if inner is not None:
        if pattern is None:
            return 2 if _glob(inner, mine, non_empty=True) else None
        if _INNER_PARAM.sub("{}", part) == mine:
            return 3
        return 1 if _patterns_overlap(pattern, inner, part) else None
    if pattern is None:
        return 3 if mine == part else None
    return 1 if _glob(pattern, part, non_empty=False) else None


def _method_matches(method: str, route: dict[str, Any]) -> bool:
    if route["method"] == "WEBSOCKET":
        return False  # every call the scanner finds is an HTTP request
    if method == "?" or route["method"] in (method, "ANY"):
        return True
    return method == "HEAD" and route["method"] == "GET" and bool(route.get("also_head"))


def best_route(call: dict[str, Any], routes: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The route ``call`` hits; ``routes`` in registration order. Unresolved calls hit none; a call
    whose method is unknown (``?``) matches any method, and so does a route registered for any
    method (``ANY``, a class-based endpoint); a HEAD call hits a GET route that answers HEAD."""
    return _best(call, _prepare(routes))


def _prepare(routes: list[dict[str, Any]]) -> list[tuple[dict[str, Any], Split | None]]:
    """Each route with its path split once, for scoring many calls against it."""
    return [(route, _split(route["path"])) for route in routes]


def _best(
    call: dict[str, Any], prepared: list[tuple[dict[str, Any], Split | None]]
) -> dict[str, Any] | None:
    if not call["resolved"]:
        return None
    mine = _split(call["path"])
    if mine is None:
        return None
    best: tuple[int, dict[str, Any]] | None = None
    for route, split in prepared:
        if split is None or not _method_matches(call["method"], route):
            continue
        found = _score(mine[0], split)
        if found is not None and (best is None or found > best[0]):
            best = (found, route)
    return best[1] if best else None


#: ``git status`` with the repository's file-system monitor switched off: ``core.fsmonitor`` in a
#: checkout's own ``.git/config`` names a command git would run. No optional locks, so nothing is
#: written to the checkout's index.
_STATUS = ["--no-optional-locks", "-c", "core.fsmonitor=false", "status", "--porcelain=v2",
           "--branch"]


#: Where the deployed code usually is: the remote's default branch, then the common names.
_DEFAULT_REFS = ("origin/HEAD", "origin/main", "origin/master")


def _git(root: Path, *args: str) -> str | None:
    try:
        return subprocess.run(
            [resolve_executable("git", untrusted=root), "-C", str(root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
            timeout=30,
            env=git_env(),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def _behind_default(root: Path) -> tuple[str, int] | None:
    """(``origin/main``, commits on it that HEAD lacks), from the first of ``_DEFAULT_REFS``
    that exists."""
    for ref in _DEFAULT_REFS:
        name = _git(root, "rev-parse", "--abbrev-ref", "--verify", "--quiet", ref)
        if name and name.strip():
            count = _git(root, "rev-list", "--count", f"HEAD..{name.strip()}")
            return (name.strip(), int(count)) if count and count.strip().isdigit() else None
    return None


def repo_state(root: Path) -> dict[str, Any] | None:
    """``{"branch", "upstream", "ahead", "behind", "default", "behind_default", "dirty"}`` for a
    git checkout, None when git is missing or ``root`` is not one. ``behind`` counts commits on the
    upstream and ``behind_default`` commits on the remote's default branch (``origin/HEAD``, else
    ``origin/main`` or ``origin/master``) that the checkout lacks, as of the last fetch; ``dirty``
    counts changed and untracked paths. A feature branch level with its own upstream can still be
    far behind what is deployed, which is why both are kept."""
    if not root.is_dir():
        return None
    out = _git(root, *_STATUS)
    if out is None:
        return None
    state: dict[str, Any] = {
        "branch": None, "upstream": None, "ahead": 0, "behind": 0, "default": None,
        "behind_default": 0, "dirty": 0,
    }
    for line in out.splitlines():
        if line.startswith("# branch.head "):
            state["branch"] = line[len("# branch.head "):]
        elif line.startswith("# branch.upstream "):
            state["upstream"] = line[len("# branch.upstream "):]
        elif line.startswith("# branch.ab "):
            ahead, behind = line[len("# branch.ab "):].split()
            state["ahead"], state["behind"] = abs(int(ahead)), abs(int(behind))
        elif line and not line.startswith("#"):
            state["dirty"] += 1
    default = _behind_default(root)
    if default is not None:
        state["default"], state["behind_default"] = default
    return state


def state_warning(name: str, state: dict[str, Any] | None) -> str | None:
    """One line when a checkout may not show what is deployed, else None."""
    if not state:
        return None
    parts = []
    if state["behind"]:
        parts.append(f"{state['behind']} commits behind {state['upstream']} (as of its last fetch)")
    if state["behind_default"] and state["default"] != state["upstream"]:
        parts.append(
            f"on {state['branch']}, {state['behind_default']} commits behind {state['default']}"
        )
    if state["dirty"]:
        parts.append(f"{state['dirty']} uncommitted changes")
    if not parts:
        return None
    return f"{name}: {' and '.join(parts)}; a gap may be the checkout, not the code"


@dataclass
class Client:
    root: Path
    name: str
    calls: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    state: dict[str, Any] | None = None
    #: Files of this front end that could not be read: their calls are missing from the join.
    errors: list[dict[str, str]] = field(default_factory=list)


def load_clients(
    repos: tuple[ClientRepo, ...], cache_dir: str | None, fresh: bool = False,
    readers: Sequence[Language] = (),
) -> list[Client]:
    """Each front end's calls view, built with its own configuration (``client_paths``,
    ``path_prefix``, overridden by its ``client_repos`` entry) and cached under its own root id;
    ``path_prefix`` is put in front of every resolved path. A missing or misconfigured front end
    is reported, not fatal. ``readers``: the language packs that read calls."""
    clients: list[Client] = []
    for repo in repos:
        root = repo.root
        client = Client(root, root.name)
        clients.append(client)
        if not root.is_dir():
            client.error = "not a directory"
            continue
        try:
            config = client_config(repo)
        except ConfigError as exc:
            client.error = str(exc)
            continue
        record, _ = calls_static.build(
            Cache(root, cache_dir), config, force=fresh, readers=readers
        )
        client.errors = record.data["errors"]
        client.calls = [
            {**c, "path": config.path_prefix + c["path"] if c["resolved"] else c["path"]}
            for c in record.data["calls"]
        ]
    return clients


def load_with_states(
    repos: tuple[ClientRepo, ...], backend: Path, cache_dir: str | None, fresh: bool = False,
    readers: Sequence[Language] = (),
) -> tuple[list[Client], dict[str, Any] | None]:
    """``load_clients``, with ``repo_state`` of the backend and of every front end, and the
    backend's state. ``git status`` is most of a warm ``terrain callers``, so the checkouts are
    asked side by side, while the calls views load."""
    roots = [backend, *(repo.root for repo in repos)]
    with ThreadPoolExecutor(max_workers=len(roots)) as pool:
        pending = [pool.submit(repo_state, root) for root in roots]
        clients = load_clients(repos, cache_dir, fresh, readers)
        backend_state, *states = [future.result() for future in pending]
    for client, state in zip(clients, states, strict=True):
        client.state = state
    return clients, backend_state


def join(clients: list[Client], routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per call: the call (``repo`` set to the front end's name) and the route it hits."""
    rows: list[dict[str, Any]] = []
    prepared = _prepare(routes)
    for client in clients:
        for call in client.calls:
            route = _best(call, prepared)
            rows.append(
                {
                    **call,
                    "repo": client.name,
                    "route": None
                    if route is None
                    else {k: route[k] for k in ("method", "path", "app", "handler")},
                }
            )
    return rows
