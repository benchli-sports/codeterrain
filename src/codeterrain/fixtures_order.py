"""The ``order`` view: fixture setup order per test, extracted by pytest inside the sandbox.

The core runs ``python -m pytest --collect-only`` with the dump plugin in the sandbox and reads the
``codeterrain.order/1`` document it leaves behind (docs/extractor-protocol.md). It never imports
pytest or the plugin itself. The project's ``addopts`` are switched off (``-o addopts=``) so a
reporting option cannot write into the target; ``respect_addopts = true`` opts back in. An ini
``log_file`` is always blanked (``-o log_file=``): it would write into the target on any run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from codeterrain import protocol
from codeterrain.config import Config
from codeterrain.sandbox import redact_document, redactor, run_python

VIEW = "order"
SCOPE_LETTER = {"session": "S", "package": "P", "module": "M", "class": "C", "function": "F"}


class ExtractError(RuntimeError):
    """The extractor ran but produced no usable document."""


def extract(config: Config, cache_dir: Path, python: str) -> dict[str, Any]:
    args = [
        "-m",
        "pytest",
        "--collect-only",
        "-q",
        "-p",
        "codeterrain._pytest_dump_plugin",
        "-p",
        "no:cacheprovider",
        "-o",
        "log_cli=false",
        "-o",
        "log_file=",
        *([] if config.respect_addopts else ["-o", "addopts="]),
        *config.pytest_args,
        *config.test_paths,
    ]
    result = run_python(python, args, config, cache_dir)
    if result.result is None:
        detail = (result.stderr.strip() or result.stdout.strip())[-1200:]
        raise ExtractError(f"pytest left no order document (exit {result.returncode}): {detail}")
    # Node ids, fixture fields and any extra field are the child's text (a parametrize id can be
    # built from an environment value): redact them before they are cached. A redacted id still
    # matches a prefix query.
    return protocol.check(redact_document(result.result, redactor(config)), protocol.ORDER_SCHEMA)


def match_nodes(doc: dict[str, Any], query: str) -> list[str]:
    """Node ids for ``query``: exact, else every id below it (parametrized, class, file)."""
    wanted = query.replace("\\", "/")
    nodes = doc["nodes"]
    if wanted in nodes:
        return [wanted]
    prefixes = (f"{wanted}[", f"{wanted}::", f"{wanted}/")
    return sorted(n for n in nodes if n.startswith(prefixes))


def groups(doc: dict[str, Any], matched: list[str]) -> list[dict[str, Any]]:
    """Matched node ids grouped by shared closure, each with its ordered fixtures."""
    by_closure: dict[int, list[str]] = {}
    for node in matched:
        by_closure.setdefault(doc["nodes"][node], []).append(node)
    return [
        {"nodes": nodes, "fixtures": doc["closures"][index]}
        for index, nodes in sorted(by_closure.items(), key=lambda kv: kv[1][0])
    ]


def scope_letter(scope: str) -> str:
    return SCOPE_LETTER.get(scope, "?")
