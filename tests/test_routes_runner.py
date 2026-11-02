from __future__ import annotations

import importlib.util
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from codeterrain import _routes_runner
from codeterrain.config import Config, resolve_root
from codeterrain.routes_view import extract


def _repo(tmp_path, files):
    root = resolve_root(tmp_path / "app")
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text), encoding="utf-8", newline="\n")
    return root


GOOD = """
    from fastapi import FastAPI
    app = FastAPI()

    @app.get("/ok")
    def ok():
        return 1
"""


def test_one_failing_app_is_recorded_and_the_others_still_load(tmp_path):
    root = _repo(
        tmp_path,
        {"good.py": GOOD, "broken.py": "raise RuntimeError('boom at import')\n"},
    )
    config = Config(root=root, apps=("broken:app", "good:app", "good:missing", "good:app"))
    doc = extract(config, tmp_path / "cache", sys.executable)
    assert [(r["app"], r["path"]) for r in doc["routes"]].count(("good:app", "/ok")) == 2
    failed = {e["app"]: e["error"] for e in doc["errors"]}
    assert "boom at import" in failed["broken:app"]
    assert "good:missing" in failed and "AttributeError" in failed["good:missing"]


def test_an_app_importing_boto3_cannot_reach_a_non_loopback_address(tmp_path):
    pytest.importorskip("boto3")
    root = _repo(
        tmp_path,
        {
            "netapp.py": """
                import boto3
                from fastapi import FastAPI

                boto3.client("sts", region_name="us-east-1").get_caller_identity()
                app = FastAPI()
            """
        },
    )
    doc = extract(Config(root=root, apps=("netapp:app",)), tmp_path / "cache", sys.executable)
    assert doc["routes"] == []
    guard = "codeterrain sandbox: non-loopback network access is blocked"
    assert any(guard in link for link in doc["errors"][0]["chain"])


def test_moto_mock_wraps_the_import_when_moto_is_importable(tmp_path):
    root = _repo(tmp_path, {"good.py": GOOD})
    doc = extract(
        Config(root=root, apps=("good:app",), moto_mock=True), tmp_path / "cache", sys.executable
    )
    if importlib.util.find_spec("moto"):
        assert doc["notes"] == ["imports ran inside moto.mock_aws()"]
    else:
        assert doc["notes"] == ["moto_mock is set but moto is not importable in the target"]
    assert "/ok" in {r["path"] for r in doc["routes"]}


def test_plugins_from_config_are_registered_in_the_target(tmp_path):
    root = _repo(
        tmp_path,
        {
            "myplugin.py": """
                from codeterrain.hookspecs import hookimpl

                class Thing:
                    pass

                @hookimpl
                def iter_routes(app):
                    if not isinstance(app, Thing):
                        return None
                    return [{"method": "GET", "path": "/thing", "handler": "myplugin.Thing",
                             "file": __file__, "line": 3, "dependencies": [],
                             "response_model": None}]
            """,
            "thingapp.py": "from myplugin import Thing\napp = Thing()\n",
        },
    )
    config = Config(root=root, apps=("thingapp:app",), plugins=("myplugin",))
    doc = extract(config, tmp_path / "cache", sys.executable)
    assert [(r["path"], r["file"]) for r in doc["routes"]] == [("/thing", "myplugin.py")]


def test_an_app_no_plugin_recognises_is_an_error_not_a_crash(tmp_path):
    root = _repo(tmp_path, {"plain.py": "app = object()\n"})
    doc = extract(Config(root=root, apps=("plain:app",)), tmp_path / "cache", sys.executable)
    assert "no plugin recognises object" in doc["errors"][0]["error"]


def test_relative_file_resolves_symlinks_and_keeps_outside_paths_absolute(tmp_path):
    root = resolve_root(tmp_path / "real")
    root.mkdir()
    (root / "m.py").write_text("x\n", encoding="utf-8")
    link = tmp_path / "link"
    try:
        link.symlink_to(root, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            raise
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(root)], check=True,
                       capture_output=True)
    assert _routes_runner.relative_file(str(link / "m.py"), root) == "m.py"
    outside = Path(__file__).resolve()
    assert _routes_runner.relative_file(str(outside), root) == outside.as_posix()
    assert _routes_runner.relative_file("", root) == ""
