from __future__ import annotations

import json
import sys

import pytest

from codeterrain import languages
from codeterrain.cache import Cache
from codeterrain.cli import main
from codeterrain.executables import distrust

#: A pack for an invented language: ``def NAME`` defines, ``use NAME`` references, inside the
#: last ``def``.
PACK = '''
import pathlib

MARK = pathlib.Path(__file__).with_name("imported")
MARK.write_text("yes")

class Language:
    interface = INTERFACE
    suffixes = (".fake",)

    @staticmethod
    def read(source):
        defs, refs, scope = [], [], ""
        for line, text in enumerate(source.splitlines(), 1):
            word, _, name = text.partition(" ")
            if word == "def":
                defs.append({"name": name, "kind": "thing", "line": line, "doc": ""})
                scope = name
            elif word == "use":
                refs.append({"name": name, "line": line, "scope": scope})
            elif word == "bad":
                return {"defs": [{"name": name}], "refs": []}
            elif word == "boom":
                raise ValueError("cannot read this")
        return {"defs": defs, "refs": refs}

language = Language()
'''


def _pack(site, module="fakelang", name="fake", interface=languages.INTERFACE):
    """Install a pack into the folder ``site`` the way an installer would."""
    (site / module).mkdir(parents=True)
    (site / module / "__init__.py").write_text(
        PACK.replace("INTERFACE", str(interface)), encoding="utf-8"
    )
    info = site / f"{module}-0.1.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {module}\nVersion: 0.1\n", encoding="utf-8"
    )
    (info / "entry_points.txt").write_text(
        f"[{languages.GROUP}]\n{name} = {module}:language\n", encoding="utf-8"
    )
    return site / module


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "infra").mkdir(parents=True)
    (root / "infra" / "main.fake").write_text(
        "def bucket.logs\nuse var.region\ndef app\nuse bucket.logs\n", encoding="utf-8"
    )
    (root / "lib.py").write_text("def f():\n    pass\n", encoding="utf-8")
    return root


@pytest.fixture(autouse=True)
def _forget(monkeypatch):
    ours = ("fakelang", "planted", "oldlang", "reviewns")
    for name in [m for m in sys.modules if m.startswith(ours)]:
        monkeypatch.delitem(sys.modules, name)
    yield
    distrust([])


def _run(capsys, root, cache, *argv):
    code = main([*argv, "--root", str(root), "--cache-dir", str(cache)])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_an_installed_pack_answers_def_and_refs(tmp_path, repo, monkeypatch, capsys):
    site = tmp_path / "site"
    _pack(site)
    monkeypatch.syspath_prepend(str(site))
    cache = tmp_path / "cache"
    code, out, _ = _run(capsys, repo, cache, "def", "logs", "--json")
    assert code == 0
    rows = json.loads(out)["rows"]
    assert [(r["name"], r["kind"], r["file"], r["line"], r["language"]) for r in rows] == [
        ("bucket.logs", "thing", "infra/main.fake", 1, "fake"),
    ]
    code, out, _ = _run(capsys, repo, cache, "refs", "bucket.logs")
    assert code == 0
    assert "thing bucket.logs  infra/main.fake:1" in out
    assert "infra/main.fake:4" in out and "app" in out
    # A Python name is answered from the Python index as before.
    code, out, _ = _run(capsys, repo, cache, "def", "f", "--json")
    assert [r["file"] for r in json.loads(out)["rows"]] == ["lib.py"]


def test_a_pack_inside_the_repository_is_refused_before_it_is_imported(
    tmp_path, repo, monkeypatch, capsys
):
    module = _pack(repo / "vendor", module="planted", name="planted")
    monkeypatch.syspath_prepend(str(repo / "vendor"))
    # Through terrain, the repository's folders are off the import path: the pack is not seen.
    code, _, _ = _run(capsys, repo, tmp_path / "cache", "def", "logs")
    assert code == 1
    assert str(repo / "vendor") not in sys.path
    # Named explicitly, its distribution is refused by where it lies.
    found, problems = languages.discover([str(repo / "vendor")])
    assert found == []
    assert "inside the repository; not loaded" in problems[0]
    assert not (module / "imported").exists()
    assert "planted" not in sys.modules


def test_a_module_the_repository_shadows_cannot_replace_the_pack(tmp_path, repo, monkeypatch):
    site = tmp_path / "site"
    _pack(site)
    shadow = repo / "shadow"
    (shadow / "fakelang").mkdir(parents=True)
    (shadow / "fakelang" / "__init__.py").write_text(
        "import pathlib\npathlib.Path(__file__).with_name('imported').write_text('yes')\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(site))
    monkeypatch.syspath_prepend(str(shadow))
    distrust([repo])
    found, problems = languages.discover([str(site)])
    assert [lang.name for lang in found] == ["fake"] and problems == []
    assert sys.modules["fakelang"].__file__ == str(site / "fakelang" / "__init__.py")
    assert not (shadow / "fakelang" / "imported").exists()


def test_a_namespace_package_cannot_reach_into_the_repository(tmp_path, repo, monkeypatch):
    site = tmp_path / "site"
    (site / "reviewns").mkdir(parents=True)  # a namespace package: no __init__.py
    info = site / "reviewns-0.1.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: reviewns\nVersion: 0.1\n", encoding="utf-8"
    )
    (info / "entry_points.txt").write_text(
        f"[{languages.GROUP}]\nreviewns = reviewns.pack:language\n", encoding="utf-8"
    )
    (repo / "reviewns").mkdir()
    (repo / "reviewns" / "pack.py").write_text(
        "import pathlib\npathlib.Path(__file__).with_name('imported').write_text('yes')\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(repo))
    monkeypatch.syspath_prepend(str(site))
    distrust([repo])
    found, problems = languages.discover([str(site)])
    assert found == [] and problems
    assert not (repo / "reviewns" / "imported").exists()
    assert str(repo) not in sys.path


def test_a_pack_for_another_interface_is_refused(tmp_path, monkeypatch):
    site = tmp_path / "site"
    _pack(site, module="oldlang", name="old", interface=languages.INTERFACE + 1)
    monkeypatch.syspath_prepend(str(site))
    found, problems = languages.discover([str(site)])
    assert found == []
    assert f"this codeterrain reads {languages.INTERFACE}" in problems[0]


def test_malformed_facts_and_a_failing_read_become_file_errors(tmp_path, monkeypatch):
    site = tmp_path / "site"
    _pack(site)
    monkeypatch.syspath_prepend(str(site))
    (language,), _ = languages.discover([str(site)])
    assert languages.read_file(language, b"bad x\n") == {
        "defs": [], "refs": [], "error": "language pack fake returned malformed facts",
    }
    assert languages.read_file(language, b"boom\n")["error"] == "ValueError: cannot read this"
    too_large = b"x" * (languages.MAX_FILE_BYTES + 1)
    assert "not indexed" in languages.read_file(language, too_large)["error"]


def test_doctor_and_refresh_list_the_installed_packs(tmp_path, repo, monkeypatch, capsys):
    site = tmp_path / "site"
    _pack(site)
    monkeypatch.syspath_prepend(str(site))
    cache = tmp_path / "cache"
    code, out, _ = _run(capsys, repo, cache, "refresh", "languages")
    assert code == 0
    assert "languages: fake 2 definitions, 2 references" in out
    code, out, _ = _run(capsys, repo, cache, "doctor")
    assert "language packs: fake 0.1" in out


def test_a_signed_language_view_is_not_checked_again(tmp_path, repo, monkeypatch):
    site = tmp_path / "site"
    _pack(site)
    monkeypatch.syspath_prepend(str(site))
    (language,), _ = languages.discover([str(site)])
    assert languages.build(Cache(repo, tmp_path / "cache"), language).verified
    monkeypatch.setattr(languages, "_valid_view", lambda data, lang: pytest.fail("checked"))
    assert languages.build(Cache(repo, tmp_path / "cache"), language).verified


def test_an_oversized_file_is_read_only_up_to_the_cap(tmp_path, repo, monkeypatch):
    site = tmp_path / "site"
    _pack(site)
    monkeypatch.syspath_prepend(str(site))
    (repo / "large.fake").write_bytes(b"def big\n" * (languages.MAX_FILE_BYTES // 4))
    (language,), _ = languages.discover([str(site)])
    seen = []
    real = languages.read_file
    monkeypatch.setattr(
        languages, "read_file", lambda lang, data: seen.append(len(data)) or real(lang, data)
    )
    data = languages.build(Cache(repo, tmp_path / "cache"), language).data
    assert max(seen) == languages.MAX_FILE_BYTES + 1
    errors = {e["file"]: e["error"] for e in data["errors"]}
    assert "not indexed" in errors["large.fake"]


def test_a_pack_may_offer_only_calls_and_a_half_pair_is_refused(tmp_path):
    good = type("P", (), {"interface": languages.INTERFACE, "call_suffixes": (".ts",),
                          "read_calls": staticmethod(lambda s, x: {"calls": [], "error": None})})
    found = languages._language("calls", "1", good())
    assert found.read is None and found.suffixes == () and found.call_suffixes == (".ts",)
    half = type("H", (), {"interface": languages.INTERFACE, "call_suffixes": (".ts",)})
    with pytest.raises(ValueError, match="has call_suffixes but no read_calls function"):
        languages._language("half", "1", half())
    with pytest.raises(ValueError, match="has no read function"):
        languages._language("none", "1", type("N", (), {"interface": languages.INTERFACE})())
