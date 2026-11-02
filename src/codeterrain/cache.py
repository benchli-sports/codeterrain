"""The on-disk store: content-hash keyed, memoised on (mtime_ns, size), shared by every worktree.

Layout under the cache dir:

- ``objects-<kind>.json``: content sha -> parsed facts (path independent, so worktrees share it).
- ``stat-<root-id>.json``: per-checkout memo of (mtime_ns, size, sha) per file.
- ``views/<view>-<root-id>.json``: a rendered view, its build time, the file fingerprint it was
  built from and (slow tier) the key of the config and environment it was built under; the slow
  tier is served from here and stamped stale when the fingerprint moved.

Objects and views carry the format number and the codeterrain version that wrote them; a file with
another format or version, a missing field or unparseable JSON reads as absent and is rebuilt.

The cache lives outside the target: the git common dir, else a per-user cache directory.
"""

from __future__ import annotations

import calendar
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codeterrain import __version__
from codeterrain.executables import resolve_executable

CACHE_ENV = "CODETERRAIN_CACHE_DIR"
FORMAT = 1
ON_WINDOWS = os.name == "nt"
REPLACE_ATTEMPTS = 5
KEY_BYTES = 32
GIT_ENV_VARS = frozenset(
    {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY"}
)
SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "node_modules",
        "__pycache__",
        ".tox",
        ".nox",
        "build",
        "dist",
        "site-packages",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".codeterrain",
    }
)


class CacheBusy(RuntimeError):
    """A cache file stayed locked by another process; try again in a moment."""


def git_env() -> dict[str, str]:
    """The environment for a ``git`` subprocess, without variables that redirect the repository."""
    return {k: v for k, v in os.environ.items() if k.upper() not in GIT_ENV_VARS}


def root_id(root: Path) -> str:
    """A stable short id of a checkout path (case-folded only where the filesystem is)."""
    text = root.as_posix()
    if ON_WINDOWS:
        text = text.lower()
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def user_base_dir() -> Path:
    """``codeterrain`` in the per-user cache dir: ``%LOCALAPPDATA%`` on Windows,
    ``$XDG_CACHE_HOME`` (default ``~/.cache``) elsewhere."""
    if ON_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "codeterrain"


def user_cache_dir(root: Path) -> Path:
    """Per-user cache for a checkout git cannot place one in; never inside the checkout."""
    return user_base_dir() / root_id(root)


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def age_text(built_at: str, now: float | None = None) -> str:
    """Human age such as ``42s``, ``7m``, ``3h``, ``2d`` for an ISO ``built_at`` stamp."""
    built = calendar.timegm(time.strptime(built_at, "%Y-%m-%dT%H:%M:%SZ"))
    seconds = max(0, int((time.time() if now is None else now) - built))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _contains(outer: Path, inner: Path) -> bool:
    try:
        inner.relative_to(outer)
    except ValueError:
        return False
    return True


def find_cache_dir(root: Path, override: str | os.PathLike[str] | None = None) -> Path:
    """``--cache-dir``, then ``$CODETERRAIN_CACHE_DIR``, then ``<git-common-dir>/codeterrain``,
    then a per-user cache directory when git cannot place one, or when the repository git finds
    does not contain the root (a parent directory's repository is not this checkout's).
    Never inside the checkout."""
    chosen = override or os.environ.get(CACHE_ENV)
    if chosen:
        return Path(os.path.realpath(chosen))
    try:
        out, top = (
            subprocess.run(
                [resolve_executable("git", untrusted=root), "-C", str(root), "rev-parse", flag],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
                env=git_env(),
            ).stdout.strip()
            for flag in ("--git-common-dir", "--show-toplevel")
        )
    except (OSError, subprocess.SubprocessError):
        return user_cache_dir(root)
    if not _contains(Path(os.path.realpath(top)), root):
        return user_cache_dir(root)
    common = Path(out)
    if not common.is_absolute():
        common = root / common
    return Path(os.path.realpath(common)) / "codeterrain"


#: How many worktrees of one repository ``checkouts`` reads at most.
MAX_WORKTREES = 1_000


@dataclass(frozen=True)
class Checkouts:
    """The git checkout holding a root: its top folder, whether it is a linked worktree (not
    the main checkout), and the other checkouts of the same repository."""

    top: Path
    linked: bool
    others: tuple[Path, ...]


def _read_line(path: Path) -> str:
    with open(path, encoding="utf-8") as f:
        return f.readline(4096).strip()


def checkouts(root: Path) -> Checkouts | None:
    """The checkout ``root`` is in and the repository's other worktrees, read from git's own
    files (``.git``, ``commondir``, ``worktrees/*/gitdir``) without running git, so it costs a
    few reads; None outside a git checkout or when they cannot be read."""
    try:
        for top in (root, *root.parents):
            dot = top / ".git"
            if dot.is_dir():
                common, linked = dot, False
                break
            if dot.is_file():
                line = _read_line(dot)
                if not line.startswith("gitdir:"):
                    return None
                gitdir = Path(line.removeprefix("gitdir:").strip())
                gitdir = gitdir if gitdir.is_absolute() else top / gitdir
                pointer = gitdir / "commondir"
                # Only a linked worktree's git dir has `commondir`; a main checkout made with
                # `git init --separate-git-dir` has a `.git` file too, pointing at the common dir.
                linked = pointer.is_file()
                # `commondir` is relative (`../..`): resolved, so its folder name can be read.
                common = Path(os.path.realpath(
                    gitdir / _read_line(pointer) if linked else gitdir
                ))
                break
        else:
            return None
        # The main checkout: the folder holding `.git`, or this one when it is the main checkout.
        # From a linked worktree of a `--separate-git-dir` repository it cannot be known (git
        # itself lists the git dir in its place), so it is left out.
        if common.name == ".git":
            tops = [common.parent]
        else:
            tops = [top] if not linked else []
        listed = common / "worktrees"
        if listed.is_dir():
            for entry in sorted(listed.iterdir())[:MAX_WORKTREES]:
                pointer = entry / "gitdir"
                if pointer.is_file():
                    tops.append(Path(_read_line(pointer)).parent)
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    here = os.path.realpath(top)
    others = []
    seen = {here}
    for other in tops:
        try:
            # strict: one lookup both resolves the path and says it still exists
            real = os.path.realpath(other, strict=True)
        except OSError:
            continue
        if real not in seen and os.path.isdir(real):
            seen.add(real)
            others.append(Path(real))
    return Checkouts(Path(here), linked, tuple(others))


def _private_dir(path: Path) -> None:
    """Create ``path`` and any missing parents; on POSIX each one it creates is 0700 whatever the
    umask (existing directories, such as ``.git``, are left alone)."""
    if path.is_dir():
        return
    _private_dir(path.parent)
    try:
        path.mkdir()
    except FileExistsError:
        return
    if not ON_WINDOWS:
        os.chmod(path, 0o700)


def _create_private(path: Path, data: bytes) -> None:
    """Write ``data`` to a new file that only the user can read (0600 on POSIX), never through a
    symlink and never into a file that already exists."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    try:
        if not ON_WINDOWS:
            os.fchmod(fd, 0o600)
        os.write(fd, data)
    finally:
        os.close(fd)


def _atomic_write(path: Path, payload: Any) -> bytes:
    """Write ``payload`` as JSON in one replace; returns the bytes written."""
    data = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    _atomic_write_bytes(path, data)
    return data


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    _private_dir(path.parent)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        _create_private(tmp, data)
    except FileExistsError:
        # left behind by an earlier process with the same pid
        tmp.unlink()
        _create_private(tmp, data)
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS - 1:
                tmp.unlink(missing_ok=True)
                raise CacheBusy(f"cache file is locked by another process: {path}") from None
            time.sleep(0.05 * (attempt + 1))


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError, RecursionError):
        return None


def _memo_entry(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 3
        and isinstance(value[0], int)
        and isinstance(value[1], int)
        and isinstance(value[2], str)
    )


def _stamped(raw: Any) -> bool:
    return (
        isinstance(raw, dict)
        and raw.get("format") == FORMAT
        and raw.get("codeterrain") == __version__
    )


def _sha256_file(path: Path) -> str:
    """Hashed in chunks: a huge file in the checkout costs time, not memory."""
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


@dataclass(frozen=True)
class ViewRecord:
    view: str
    built_at: str
    fingerprint: dict[str, str]
    data: Any
    key: str | None = None
    #: The file carries a valid MAC under the per-user key: it is what this core wrote, after
    #: checking it, so a reader need not check it again.
    verified: bool = False

    def signed_as(self, kind: str) -> bool:
        """Signed by this core and of the format ``kind``: its rows were checked before it was
        written, so a warm answer need not check every one again."""
        return (
            self.verified and isinstance(self.data, dict) and self.data.get("format") == kind
        )


class Cache:
    def __init__(self, root: Path, cache_dir: str | os.PathLike[str] | None = None) -> None:
        self.root = root
        self.dir = find_cache_dir(root, cache_dir)
        self.root_id = root_id(root)
        self._stat_path = self.dir / f"stat-{self.root_id}.json"
        self._stat_memo: dict[str, list[Any]] | None = None
        self._stat_dirty = False
        #: per walk start: the suffixes its listing has served, and the listing
        self._listings: dict[str, tuple[set[str], list[tuple[str, os.DirEntry[str]]]]] = {}

    def _memo(self) -> dict[str, list[Any]]:
        if self._stat_memo is None:
            loaded = _read_json(self._stat_path)
            self._stat_memo = loaded if isinstance(loaded, dict) else {}
            for rel in [r for r, v in self._stat_memo.items() if not _memo_entry(v)]:
                del self._stat_memo[rel]
        return self._stat_memo

    def _walk(
        self, start: Path, suffixes: tuple[str, ...]
    ) -> Iterable[tuple[str, os.stat_result]]:
        for rel, entry in self._files(str(start), suffixes):
            if entry.name.endswith(suffixes) and self._readable(entry):
                yield rel, entry.stat()

    def _files(
        self, start: str, suffixes: tuple[str, ...]
    ) -> list[tuple[str, os.DirEntry[str]]]:
        """Every file under ``start`` outside the skipped folders. The code index and each
        language pack scan the same tree for different suffixes, so one walk serves each suffix
        once; a second scan for a suffix already served walks again, and sees what changed."""
        listing = self._listings.get(start)
        if listing is not None and not listing[0] & set(suffixes):
            listing[0].update(suffixes)
            return listing[1]
        # Paths stay strings: a warm answer walks the whole tree, and building a Path and asking
        # `relative_to` for every file was most of its cost.
        prefix = os.path.join(str(self.root), "")
        found: list[tuple[str, os.DirEntry[str]]] = []
        stack = [start]
        while stack:
            current = stack.pop()
            try:
                entries = list(os.scandir(current))
            except OSError:
                continue
            # A virtual environment is skipped whole. Its `pyvenv.cfg` shows in the listing
            # already read, which costs nothing, where asking every folder for one did.
            if current != start and any(e.name == "pyvenv.cfg" for e in entries):
                continue
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    if (
                        entry.name in SKIP_DIRS
                        or entry.name.startswith(".")
                        or entry.name.endswith(".egg-info")
                        or entry.is_junction()
                    ):
                        continue
                    stack.append(entry.path)
                else:
                    found.append((self._relative(entry.path, prefix), entry))
        self._listings[start] = (set(suffixes), found)
        return found

    def _relative(self, path: str, prefix: str) -> str:
        """``path`` relative to the root, in POSIX form, as ``Path.relative_to().as_posix()``."""
        if not path.startswith(prefix):
            return Path(path).relative_to(self.root).as_posix()
        rel = path[len(prefix):]
        return rel.replace(os.sep, "/") if os.sep != "/" else rel

    def _readable(self, entry: os.DirEntry[str]) -> bool:
        """A regular file, or a symlink to one that stays inside the root; never a device, pipe
        or a link out of the checkout."""
        if not entry.is_symlink():
            return entry.is_file(follow_symlinks=False)
        target = Path(os.path.realpath(entry.path))
        return _contains(self.root, target) and target.is_file()

    def scan(
        self, subpaths: Iterable[str] | None = None, suffixes: tuple[str, ...] = (".py",)
    ) -> dict[str, str]:
        """Relative POSIX path -> sha256 for every matching file under ``subpaths`` (None = all)."""
        memo = self._memo()
        listed = list(subpaths) if subpaths is not None else None
        starts = [self.root / p for p in listed] if listed is not None else [self.root]
        prefixes = (
            tuple(Path(p).as_posix().rstrip("/") + "/" for p in listed)
            if listed is not None
            else None
        )
        seen: dict[str, str] = {}
        for start in starts:
            if not _contains(self.root, Path(os.path.realpath(start))):
                continue  # a link out of the checkout; config refuses it, this is the backstop
            if start.is_file():
                rel = start.relative_to(self.root).as_posix()
                found: Iterable[tuple[str, os.stat_result]] = [(rel, start.stat())]
            elif start.is_dir():
                found = self._walk(start, suffixes)
            else:
                continue
            for rel, st in found:
                known = memo.get(rel)
                if known is not None and known[0] == st.st_mtime_ns and known[1] == st.st_size:
                    seen[rel] = known[2]
                    continue
                sha = _sha256_file(self.root / rel)
                memo[rel] = [st.st_mtime_ns, st.st_size, sha]
                self._stat_dirty = True
                seen[rel] = sha
        gone = [
            rel
            for rel in memo
            if rel not in seen
            and rel.endswith(suffixes)
            and (prefixes is None or rel.startswith(prefixes))
        ]
        for rel in gone:
            del memo[rel]
            self._stat_dirty = True
        return seen

    def hash_files(self, paths: Iterable[Path]) -> dict[str, str]:
        """sha256 of individual files (pyproject, the route manifest) keyed by relative path."""
        out: dict[str, str] = {}
        for path in paths:
            if _contains(self.root, Path(os.path.realpath(path))) and path.is_file():
                try:
                    rel = path.relative_to(self.root).as_posix()
                except ValueError:
                    rel = path.as_posix()
                out[rel] = _sha256_file(path)
        return out

    def cached_probe(self, exe: Path, compute: Callable[[], Any]) -> Any:
        """``compute()`` once per (interpreter path, mtime, size), remembered in the stat memo.

        The memo slot for the sha holds the JSON text of the result; a failing ``compute`` stores
        nothing, so the next call tries again."""
        try:
            st = exe.stat()
        except OSError:
            return compute()
        memo = self._memo()
        name = f"probe:{exe.as_posix()}"
        known = memo.get(name)
        if known is not None and known[0] == st.st_mtime_ns and known[1] == st.st_size:
            try:
                return json.loads(known[2])
            except ValueError:
                pass
        value = compute()
        memo[name] = [st.st_mtime_ns, st.st_size, json.dumps(value, sort_keys=True)]
        self._stat_dirty = True
        return value

    def secret_key(self) -> bytes:
        """A random 32-byte per-user key (0600 on POSIX), created once; it keys the digests of
        ``pass_env`` values so they cannot be brute-forced from a copied view file. It lives in
        the per-user dir (``user_base_dir``), never in the checkout's cache, so a copied ``.git``
        does not carry it."""
        base = user_base_dir()
        path = base / "key"
        try:
            key = path.read_bytes()
        except FileNotFoundError:
            key = b""
        if len(key) == KEY_BYTES:
            return key
        _private_dir(base)
        if key:
            path.unlink(missing_ok=True)  # truncated or foreign: replace it
        try:
            _create_private(path, secrets.token_bytes(KEY_BYTES))
        except FileExistsError:
            pass  # another process created it first; use theirs
        return path.read_bytes()

    def flush(self) -> None:
        if self._stat_dirty and self._stat_memo is not None:
            _atomic_write(self._stat_path, self._stat_memo)
            self._stat_dirty = False

    def load_objects(self, kind: str) -> dict[str, Any]:
        loaded = _read_json(self.dir / f"objects-{kind}.json")
        if not _stamped(loaded) or not isinstance(loaded.get("objects"), dict):
            return {}
        return loaded["objects"]

    def save_objects(self, kind: str, objects: dict[str, Any]) -> None:
        _atomic_write(
            self.dir / f"objects-{kind}.json",
            {"format": FORMAT, "codeterrain": __version__, "objects": objects},
        )

    def _view_path(self, view: str) -> Path:
        return self.dir / "views" / f"{view}-{self.root_id}.json"

    def _view_mac(self, data: bytes) -> str | None:
        """The MAC of a view file under the per-user key; None when the key cannot be read or
        created (a read-only or missing user cache dir): the view is then unsigned and checked."""
        try:
            key = self.secret_key()
        except OSError:
            return None
        return hmac.new(key, data, hashlib.sha256).hexdigest()

    def read_view(self, view: str) -> ViewRecord | None:
        path = self._view_path(view)
        try:
            data = path.read_bytes()
            raw = json.loads(data)
        except (OSError, ValueError, RecursionError):
            return None
        try:
            mac = path.with_suffix(".mac").read_text(encoding="ascii").strip()
        except (OSError, ValueError):
            mac = ""
        expected = self._view_mac(data) if mac else None
        verified = expected is not None and hmac.compare_digest(mac, expected)
        if not _stamped(raw) or "data" not in raw:
            return None
        built_at, fingerprint, key = raw.get("built_at"), raw.get("fingerprint"), raw.get("key")
        if not isinstance(built_at, str) or not isinstance(fingerprint, dict):
            return None
        if key is not None and not isinstance(key, str):
            return None
        try:
            age_text(built_at)
        except ValueError:
            return None
        return ViewRecord(view, built_at, fingerprint, raw["data"], key, verified)

    def write_view(
        self,
        view: str,
        data: Any,
        fingerprint: dict[str, str],
        key: str | None = None,
        sign: bool = False,
        built_at: str | None = None,
    ) -> ViewRecord:
        """Write a view; ``sign`` (a view built from checked facts, or checked against its
        schema) adds a MAC under the per-user key so a reader can skip the check. ``built_at``
        keeps the time of a view written again unchanged."""
        record = ViewRecord(view, built_at or utc_now(), fingerprint, data, key)
        path = self._view_path(view)
        path.with_suffix(".mac").unlink(missing_ok=True)  # never a MAC of the previous file
        written = _atomic_write(
            path,
            {
                "format": FORMAT,
                "codeterrain": __version__,
                "view": view,
                "built_at": record.built_at,
                "fingerprint": fingerprint,
                "key": key,
                "data": data,
            },
        )
        mac = self._view_mac(written) if sign else None
        if mac is None:
            return record
        _atomic_write_bytes(path.with_suffix(".mac"), mac.encode("ascii"))
        return ViewRecord(view, record.built_at, fingerprint, data, key, verified=True)


    def served(
        self, record: ViewRecord, kind: str, valid: Callable[[Any], bool]
    ) -> ViewRecord | None:
        """``record`` when it can be answered from as it is: signed and of the format ``kind``,
        or else checked by ``valid``, and then written again signed so the next read skips the
        check (a view written before the static views were signed). None when it fails."""
        if record.signed_as(kind):
            return record
        if not valid(record.data):
            return None
        return self.write_view(
            record.view, record.data, record.fingerprint, record.key, sign=True,
            built_at=record.built_at,
        )


def changed_files(recorded: dict[str, str], current: dict[str, str]) -> int:
    """How many files differ between two fingerprints (changed, added or removed)."""
    return sum(1 for k in recorded.keys() | current.keys() if recorded.get(k) != current.get(k))
