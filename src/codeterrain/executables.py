"""Resolve a program name to an absolute path without ever looking in the current directory.

On Windows, ``CreateProcess`` with a bare name searches the current directory before ``PATH``
(unless ``NoDefaultCurrentDirectoryInExePath`` is set), and ``shutil.which`` puts ``.`` first. The
documented way to run codeterrain is from the target repository's root, so a bare ``git`` or
``--python python3`` would run a program the repository ships. Every subprocess codeterrain starts
gets an absolute path from here instead.

Only ``.exe`` and ``.com`` files are run on Windows, as ``CreateProcess`` itself does: a ``.bat``
or ``.cmd`` file runs through ``cmd.exe``, which would read ``&`` or ``%VAR%`` in an argument (a
checkout path, a configured app name) as commands.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable
from pathlib import Path

WINDOWS = os.name == "nt"
#: The program types run directly on Windows, in ``PATHEXT`` order.
RUNNABLE = (".COM", ".EXE")
BATCH = (".BAT", ".CMD")

#: Checkouts every lookup refuses programs from, besides the one it names: the target and every
#: other checkout the command reads (``client_repos``). Set once per command by ``distrust``.
_distrusted: tuple[Path, ...] = ()


def distrust(roots: Iterable[Path]) -> None:
    """Refuse programs inside ``roots`` in every later lookup, replacing the previous set: a
    command that reads several checkouts must not run a program one of them ships while it asks
    about another."""
    global _distrusted
    _distrusted = tuple(Path(root) for root in roots)


def _has_separator(name: str) -> bool:
    return any(sep and sep in name for sep in (os.sep, os.altsep, "/"))


def _candidates(name: str) -> list[str]:
    if not WINDOWS:
        return [name]
    if os.path.splitext(name)[1].upper() in RUNNABLE:
        return [name]
    return [name + ext for ext in RUNNABLE]


def _absolute_entry(entry: str) -> bool:
    """An absolute ``PATH`` entry; on Windows it needs a drive or a UNC share, since a rooted
    ``\\tools`` resolves against the current drive (the checkout's)."""
    return os.path.isabs(entry) and (not WINDOWS or bool(Path(entry).drive))


def _inside(root: Path, path: Path) -> bool:
    try:
        Path(os.path.realpath(path)).relative_to(os.path.realpath(root))
    except ValueError:
        return False
    return True


#: The folder terrain's own package is imported from (``src`` of a checkout, or site-packages).
_OWN_CODE = Path(__file__).resolve().parent.parent


def _runs_terrain(root: Path) -> list[Path]:
    """What terrain itself runs from inside ``root``: its environment (a virtual environment in
    the checkout) and the folder its own code is imported from, when either lies inside it. A
    checkout inside the environment gains nothing from this."""
    return [place for place in (Path(sys.prefix), _OWN_CODE) if _inside(root, place)]


def distrusted(path: Path) -> bool:
    """Whether ``path`` lies inside a checkout given to ``distrust`` and outside what terrain
    itself runs from there, which is trusted with terrain already."""
    return any(
        _inside(root, path) and not any(_inside(place, path) for place in _runs_terrain(root))
        for root in _distrusted
    )


def drop_distrusted_imports() -> None:
    """Remove from ``sys.path`` every entry a checkout given to ``distrust`` could control: one
    inside it, and an empty or relative entry (the current directory). Every later import, a
    namespace package's parts and a pack's own lazy imports included, then resolves outside it."""
    sys.path[:] = [
        entry for entry in sys.path
        if entry and os.path.isabs(entry) and not distrusted(Path(entry))
    ]
    sys.path_importer_cache.clear()


def resolve_executable(name: str | os.PathLike[str], untrusted: Path | None = None) -> str:
    """An absolute path for ``name``.

    A name with a path separator is the caller's explicit choice and is only made absolute. A bare
    name is looked up in the ``PATH`` entries that are absolute (empty, ``.``, relative and, on
    Windows, driveless entries are skipped), trying ``.com`` and ``.exe`` on Windows. Raises
    ``FileNotFoundError`` when nothing matches, for a ``.bat`` or ``.cmd`` file, and when the
    program a bare name finds lies inside ``untrusted`` (the target repository) or any checkout
    given to ``distrust``: an absolute ``PATH`` entry such as ``<checkout>/bin`` would otherwise
    run a program a repository ships.
    """
    text = os.fspath(name)
    if WINDOWS and os.path.splitext(text)[1].upper() in BATCH:
        raise FileNotFoundError(
            f"{text}: a .bat or .cmd file runs through cmd.exe, which would interpret its "
            "arguments; pass the .exe it starts"
        )
    if _has_separator(text):
        return os.path.abspath(text)
    roots = [*_distrusted, *([Path(untrusted)] if untrusted is not None else [])]
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        entry = entry.strip('"')
        if not entry or not _absolute_entry(entry):
            continue
        for candidate in _candidates(text):
            path = Path(entry) / candidate
            if path.is_file() and (WINDOWS or os.access(path, os.X_OK)):
                if any(_inside(root, path) for root in roots):
                    raise FileNotFoundError(
                        f"{text} on PATH is {path}, inside a target repository, which could "
                        "have planted it; put a trusted directory first on PATH, or give the "
                        "full path if you trust it"
                    )
                return str(path)
    raise FileNotFoundError(f"{text} not found on PATH")
