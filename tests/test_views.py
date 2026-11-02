from __future__ import annotations

import dataclasses
import sys

import pytest

from codeterrain import views
from codeterrain.cache import Cache
from codeterrain.config import ClientRepo
from codeterrain.protocol import ROUTES_SCHEMA, ProtocolError


def _doc(**extra):
    return {"schema": ROUTES_SCHEMA, "routes": [], "errors": [], **extra}


@pytest.fixture
def served(sample_copy, cache_dir, sample_config):
    calls = []

    def build():
        calls.append(1)
        return _doc(generation=len(calls))

    def serve(fresh=False, notes=None, config=None, python=sys.executable, build_fn=None):
        return views.serve_slow(
            Cache(sample_copy, cache_dir),
            config or sample_config,
            "routes",
            ROUTES_SCHEMA,
            build_fn or build,
            python,
            fresh=fresh,
            notify=(notes.append if notes is not None else (lambda message: None)),
        )

    serve.calls = calls
    return serve


def test_first_use_builds_and_announces_it_then_the_cache_answers(served):
    notes: list[str] = []
    first = served(notes=notes)
    assert first.built_now and first.stale == 0 and len(notes) == 1
    again = served()
    assert not again.built_now and again.stale == 0
    assert again.record.data["generation"] == 1 and len(served.calls) == 1


def test_edits_are_counted_as_stale_but_never_trigger_a_rebuild(served, sample_copy):
    served()
    target = sample_copy / "sampleapp" / "main.py"
    target.write_text(target.read_text(encoding="utf-8") + "\n# edit\n", encoding="utf-8")
    (sample_copy / "sampleapp" / "added.py").write_text("x = 1\n", encoding="utf-8")
    result = served()
    assert result.stale == 2 and not result.built_now and len(served.calls) == 1


def test_changing_the_manifest_or_pyproject_counts_too(served, sample_copy):
    served()
    manifest = sample_copy / "docs" / "route-access.json"
    manifest.write_text(manifest.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert served().stale == 1


def test_fresh_rebuilds_and_clears_the_stale_count(served, sample_copy):
    served()
    (sample_copy / "sampleapp" / "added.py").write_text("x = 1\n", encoding="utf-8")
    assert served().stale == 1
    rebuilt = served(fresh=True)
    assert rebuilt.built_now and rebuilt.record.data["generation"] == 2
    assert served().stale == 0


def test_a_cached_document_with_an_unknown_schema_is_rebuilt_not_served(
    served, sample_copy, cache_dir
):
    Cache(sample_copy, cache_dir).write_view(
        "routes", {"schema": "codeterrain.routes/2", "routes": [], "errors": []}, {}
    )
    result = served()
    assert result.built_now and result.record.data["schema"] == ROUTES_SCHEMA


def test_a_fresh_build_that_violates_the_protocol_is_refused_and_not_cached(
    sample_copy, cache_dir, sample_config
):
    def build():
        raise ProtocolError("unsupported extractor schema")

    with pytest.raises(ProtocolError):
        views.serve_slow(Cache(sample_copy, cache_dir), sample_config, "routes",
                         ROUTES_SCHEMA, build, sys.executable)
    assert Cache(sample_copy, cache_dir).read_view("routes") is None


@pytest.mark.parametrize(
    "name",
    ["pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini", "tests/conftest.py"],
)
def test_editing_any_project_config_file_or_a_conftest_makes_the_view_stale(
    served, sample_copy, name
):
    served()
    target = sample_copy / name
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    target.write_text(existing + "\n# edit\n", encoding="utf-8")
    assert served().stale == 1


def test_a_pyproject_only_edit_is_stale_and_changes_nothing_else(served, sample_copy):
    served()
    pyproject = sample_copy / "pyproject.toml"
    pyproject.write_text(pyproject.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    result = served()
    assert result.stale == 1 and not result.built_now


def test_the_fingerprint_is_taken_before_the_build_so_an_edit_during_it_is_stale(
    served, sample_copy
):
    target = sample_copy / "sampleapp" / "other.py"

    def edits_while_building():
        target.write_text(target.read_text(encoding="utf-8") + "\n# during\n", encoding="utf-8")
        return _doc()

    built = served(build_fn=edits_while_building)
    assert built.built_now and built.stale == 0
    assert served().stale == 1


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda c: {"python": "/somewhere/else/python"}, id="python"),
        pytest.param(lambda c: {"config": dataclasses.replace(c, pytest_args=("-x",))},
                     id="pytest_args"),
        pytest.param(lambda c: {"config": dataclasses.replace(c, import_env={"MODE": "x"})},
                     id="import_env"),
        pytest.param(lambda c: {"config": dataclasses.replace(c, pass_env=("TOKEN",))},
                     id="pass_env"),
        pytest.param(lambda c: {"config": dataclasses.replace(c, apps=("sampleapp.other:other",))},
                     id="apps"),
        pytest.param(lambda c: {"config": dataclasses.replace(c, respect_addopts=True)},
                     id="respect_addopts"),
        pytest.param(lambda c: {"config": dataclasses.replace(c, plugins=("x.y",))},
                     id="plugins"),
        pytest.param(lambda c: {"config": dataclasses.replace(c, moto_mock=True)},
                     id="moto_mock"),
        pytest.param(lambda c: {"config": dataclasses.replace(c, region="eu-west-1")},
                     id="region"),
        pytest.param(lambda c: {"config": dataclasses.replace(c, test_paths=("tests", "more"))},
                     id="test_paths"),
    ],
)
def test_a_changed_config_or_interpreter_rebuilds_instead_of_serving_the_other_runs_rows(
    served, sample_config, change
):
    first = served()
    assert first.record.data["generation"] == 1
    assert served().record.data["generation"] == 1
    other = served(**change(sample_config))
    assert other.built_now and other.record.data["generation"] == 2
    assert other.record.key != first.record.key


def test_a_new_codeterrain_version_rebuilds(served, monkeypatch):
    served()
    monkeypatch.setattr(views, "__version__", "99.0.0")
    assert served().built_now


def test_the_two_slow_views_never_share_a_key(sample_copy, cache_dir, sample_config):
    cache = Cache(sample_copy, cache_dir)
    a = views.view_key(cache, sample_config, sys.executable, "routes")
    b = views.view_key(cache, sample_config, sys.executable, "order")
    assert a != b


def test_the_key_tracks_installed_distributions_beside_the_interpreter(
    tmp_path, sample_copy, cache_dir, sample_config
):
    venv = tmp_path / "venv"
    site = venv / "Lib" / "site-packages"
    site.mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")
    exe = venv / "Scripts" / "python.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    cache = Cache(sample_copy, cache_dir)
    before = views.view_key(cache, sample_config, str(exe), "routes")
    (site / "pytest-8.0.0.dist-info").mkdir()
    after = views.view_key(cache, sample_config, str(exe), "routes")
    assert before != after
    assert views.interpreter_fingerprint(str(exe))["dist_info"] == ["pytest-8.0.0.dist-info"]
    (venv / "pyvenv.cfg").write_text("home = y\n", encoding="utf-8")
    assert views.view_key(cache, sample_config, str(exe), "routes") != after


def test_a_changed_pass_env_value_rebuilds_but_the_key_never_holds_the_value(
    served, sample_config, monkeypatch
):
    config = dataclasses.replace(sample_config, pass_env=("CT_FLAG",))
    monkeypatch.setenv("CT_FLAG", "first-secret-value")
    first = served(config=config)
    assert served(config=config).record.data["generation"] == 1
    monkeypatch.setenv("CT_FLAG", "second-secret-value")
    flipped = served(config=config)
    assert flipped.built_now and flipped.record.data["generation"] == 2
    assert flipped.record.key != first.record.key
    monkeypatch.delenv("CT_FLAG")
    unset = served(config=config)
    assert unset.built_now and unset.record.key not in (first.record.key, flipped.record.key)
    for key in (first.record.key, flipped.record.key):
        assert "secret" not in key


def test_pass_env_digests_hmac_name_and_value_and_mark_a_missing_name(monkeypatch):
    import hashlib
    import hmac

    monkeypatch.setenv("CT_PRESENT", "v")
    monkeypatch.delenv("CT_ABSENT", raising=False)
    found = views.pass_env_digests(("CT_PRESENT", "CT_ABSENT"), b"k" * 32)
    assert found == {
        "CT_PRESENT": hmac.new(b"k" * 32, b"CT_PRESENT=v", hashlib.sha256).hexdigest(),
        "CT_ABSENT": None,
    }


def _user_dirs(monkeypatch, base):
    monkeypatch.setenv("LOCALAPPDATA", str(base))
    monkeypatch.setenv("XDG_CACHE_HOME", str(base))


def test_the_digest_key_is_random_per_user_and_changes_the_digest(monkeypatch, tmp_path):
    # Security review P3-8: an unsalted sha256 of a low-entropy value could be guessed offline.
    monkeypatch.setenv("CT_PRESENT", "v")
    _user_dirs(monkeypatch, tmp_path / "u1")
    first = Cache(tmp_path, tmp_path / "c1").secret_key()
    again = Cache(tmp_path, tmp_path / "c2").secret_key()
    _user_dirs(monkeypatch, tmp_path / "u2")
    other = Cache(tmp_path, tmp_path / "c1").secret_key()
    assert len(first) == 32 and first == again and first != other
    digests = [views.pass_env_digests(("CT_PRESENT",), k) for k in (first, other)]
    assert digests[0] != digests[1]
    (tmp_path / "u2" / "codeterrain" / "key").write_bytes(b"short")
    assert len(Cache(tmp_path, tmp_path / "c1").secret_key()) == 32


def test_the_digest_key_is_never_kept_beside_the_views(monkeypatch, tmp_path):
    # Security review r2 P3-8: the key sat in .git/codeterrain next to the views it protects, so
    # a zipped checkout carried both and the offline guess worked again.
    _user_dirs(monkeypatch, tmp_path / "user")
    cache = Cache(tmp_path / "repo", tmp_path / "repo" / ".git" / "codeterrain")
    cache.secret_key()
    assert (tmp_path / "user" / "codeterrain" / "key").is_file()
    assert not list((tmp_path / "repo").rglob("key"))


CONFIG_ALTERNATIVES = {
    "apps": ("sampleapp.other:other",),
    "import_env": {"MODE": "x"},
    "test_paths": ("other_tests",),
    "region": "eu-west-1",
    "plugins": ("x.y",),
    "moto_mock": True,
    "pytest_args": ("-x",),
    "pass_env": ("TOKEN",),
    "respect_addopts": True,
}
CONFIG_EXEMPT = {
    # the cache is per checkout, so the root is the same for every key it ever sees
    "root": "/elsewhere",
    # where the values came from, not the values themselves
    "source": "--config other.toml",
    # read at query time by the access join; the extractors never see the manifest
    "route_manifest": "other-manifest.json",
    # how the join reads that manifest, again query time only
    "manifest_keys": {"rows": "other"},
    # which repository keys were dropped: reporting only, the dropped values never reach the child
    "ignored": ("pass_env",),
    # where the user-only values came from; the values themselves are keyed above
    "user_config": "/home/u/.config/codeterrain/config.toml",
    # the frontend calls view; its fingerprint is the files it scanned
    "client_paths": ("web",),
    # applied when frontend calls are matched to routes, at query time
    "path_prefix": "/v1",
    # front ends joined to the routes at query time; the route walk never sees them
    "client_repos": ("/elsewhere/web",),  # made a ClientRepo below
}


def test_the_view_key_changes_with_every_config_field_that_changes_an_extraction(
    sample_copy, cache_dir, sample_config
):
    from pathlib import Path

    fields = {f.name for f in dataclasses.fields(type(sample_config))}
    assert fields == set(CONFIG_ALTERNATIVES) | set(CONFIG_EXEMPT), (
        "a new Config field needs an alternative value here or an exemption with a reason"
    )
    assert not set(CONFIG_ALTERNATIVES) & set(CONFIG_EXEMPT)
    assert views.KEY_EXEMPT == set(CONFIG_EXEMPT)
    cache = Cache(sample_copy, cache_dir)
    base = views.view_key(cache, sample_config, sys.executable, "order")
    for name, value in CONFIG_ALTERNATIVES.items():
        other = dataclasses.replace(sample_config, **{name: value})
        assert views.view_key(cache, other, sys.executable, "order") != base, name
    for name, value in CONFIG_EXEMPT.items():
        value = Path(value) if name in ("root", "route_manifest") else value
        value = tuple(ClientRepo(Path(v)) for v in value) if name == "client_repos" else value
        other = dataclasses.replace(sample_config, **{name: value})
        assert views.view_key(cache, other, sys.executable, "order") == base, name


@pytest.fixture
def fake_interpreter(tmp_path):
    exe = tmp_path / "bare" / "python"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    return exe


def _site(tmp_path, name, *dists):
    site = tmp_path / name / "site-packages"
    for dist in dists:
        (site / f"{dist}.dist-info").mkdir(parents=True)
    site.mkdir(parents=True, exist_ok=True)
    return site


@pytest.fixture
def probes(monkeypatch, tmp_path):
    seen = []
    site = _site(tmp_path, "launched", "a-1")
    state = {
        "site": site,
        "answer": {
            "python": "3.12.0", "pytest": "8.0", "fastapi": None,
            "prefix": str(site.parent), "sites": [str(site)], "dists": ["a-1.dist-info"],
        },
    }

    def probe(python, config, cache_dir):
        seen.append(str(python))
        return dict(state["answer"])

    monkeypatch.setattr(views, "probe_versions", probe)
    probe.seen = seen
    probe.state = state
    return probe


def test_two_serve_slow_calls_spawn_the_version_probe_once(served, fake_interpreter, probes):
    served(python=str(fake_interpreter))
    served(python=str(fake_interpreter))
    served(python=str(fake_interpreter), fresh=True)
    assert len(probes.seen) == 1


def test_the_probe_runs_again_when_the_interpreter_file_changes(
    sample_copy, cache_dir, sample_config, fake_interpreter, probes
):
    cache = Cache(sample_copy, cache_dir)
    before = views.view_key(cache, sample_config, str(fake_interpreter), "order")
    probes.state["answer"] = {**probes.state["answer"], "python": "3.13.0"}
    assert views.view_key(cache, sample_config, str(fake_interpreter), "order") == before
    fake_interpreter.write_bytes(b"upgraded")
    after = views.view_key(cache, sample_config, str(fake_interpreter), "order")
    assert len(probes.seen) == 2 and after != before


def test_a_probe_that_finds_other_site_dirs_with_other_distributions_changes_the_key(
    sample_copy, cache_dir, sample_config, tmp_path, probes
):
    cache = Cache(sample_copy, cache_dir)
    keys = []
    for index, dists in enumerate((("a-1",), ("a-1", "b-1"))):
        exe = tmp_path / f"bare{index}" / "python"
        exe.parent.mkdir()
        exe.write_bytes(b"")
        site = _site(tmp_path, f"env{index}", *dists)
        probes.state["answer"] = {**probes.state["answer"], "sites": [str(site)]}
        info = views.interpreter_fingerprint(str(exe), sample_config, cache)
        assert info["dist_info"] == [f"{d}.dist-info" for d in dists]
        assert "dists" not in info["probe"]
        keys.append(views.view_key(cache, sample_config, str(exe), "order"))
    assert keys[0] != keys[1]


def test_a_pip_install_behind_a_launcher_changes_the_key_without_another_probe(
    served, sample_copy, cache_dir, sample_config, fake_interpreter, probes
):
    cache = Cache(sample_copy, cache_dir)
    first = views.view_key(cache, sample_config, str(fake_interpreter), "order")
    cache.flush()
    assert views.view_key(cache, sample_config, str(fake_interpreter), "order") == first
    (probes.state["site"] / "zz_fake-1.0.dist-info").mkdir()
    installed = views.view_key(cache, sample_config, str(fake_interpreter), "order")
    assert installed != first
    assert "zz_fake-1.0.dist-info" in views.interpreter_fingerprint(
        str(fake_interpreter), sample_config, cache
    )["dist_info"]
    stale = served(python=str(fake_interpreter))
    assert stale.built_now
    assert not served(python=str(fake_interpreter)).built_now
    (probes.state["site"] / "zz_other-2.0.dist-info").mkdir()
    assert served(python=str(fake_interpreter)).built_now
    assert len(probes.seen) == 1


def test_a_failing_probe_is_remembered_as_nothing_and_tried_again(
    sample_copy, cache_dir, sample_config, fake_interpreter, monkeypatch
):
    from codeterrain.sandbox import SandboxError

    calls = []

    def failing(python, config, cache_dir):
        calls.append(1)
        raise SandboxError("no")

    monkeypatch.setattr(views, "probe_versions", failing)
    cache = Cache(sample_copy, cache_dir)
    for _ in range(2):
        info = views.interpreter_fingerprint(str(fake_interpreter), sample_config, cache)
        assert info["probe"] is None
    assert len(calls) == 2


def test_both_site_packages_and_dist_packages_are_fingerprinted(tmp_path):
    venv = tmp_path / "venv"
    for kind, name in (("site-packages", "a-1.dist-info"), ("dist-packages", "b-1.dist-info")):
        (venv / "lib" / "python3.12" / kind / name).mkdir(parents=True)
    exe = venv / "bin" / "python"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    info = views.interpreter_fingerprint(str(exe))
    assert info["dist_info"] == ["a-1.dist-info", "b-1.dist-info"]
    assert "probe" not in info


def test_the_real_probe_reports_prefix_site_dirs_and_distributions(tmp_path):
    from codeterrain.config import Config
    from codeterrain.sandbox import probe_versions

    root = tmp_path / "root"
    root.mkdir()
    found = probe_versions(sys.executable, Config(root=root), tmp_path / "cache")
    assert found["prefix"] == sys.prefix
    assert found["sites"]
    assert all(s.endswith(("site-packages", "dist-packages")) for s in found["sites"])
    assert any(d.startswith("pytest-") for d in found["dists"])


def _counting_checks(monkeypatch):
    checked = []
    real = views.protocol.check

    def check(doc, schema):
        checked.append(schema)
        return real(doc, schema)

    monkeypatch.setattr(views.protocol, "check", check)
    return checked


def test_a_signed_view_is_served_without_checking_it_again(served, monkeypatch, tmp_path):
    # Checking the whole cached document on every read was most of a warm `setup-order`.
    _user_dirs(monkeypatch, tmp_path / "user")
    served()
    checked = _counting_checks(monkeypatch)
    result = served()
    assert not result.built_now and result.record.verified and checked == []


@pytest.mark.parametrize("damage", ["edited", "unsigned", "other key"])
def test_a_view_whose_signature_does_not_hold_is_checked_as_before(
    served, monkeypatch, tmp_path, sample_copy, cache_dir, damage
):
    _user_dirs(monkeypatch, tmp_path / "user")
    served()
    cache = Cache(sample_copy, cache_dir)
    path = cache._view_path("routes")
    if damage == "edited":
        # Still valid JSON and stamped, but the routes are not a list: the check refuses it.
        path.write_bytes(path.read_bytes().replace(b'"routes":[]', b'"routes":"x"'))
    elif damage == "unsigned":
        path.with_suffix(".mac").unlink()
    else:
        _user_dirs(monkeypatch, tmp_path / "another-user")
    checked = _counting_checks(monkeypatch)
    result = served()
    assert checked
    assert result.record.data["routes"] == []
    assert result.built_now == (damage == "edited")


def test_views_work_unsigned_when_the_per_user_key_cannot_be_reached(
    served, monkeypatch, sample_copy, cache_dir
):
    # Signing every view made static commands fail when the user cache
    # dir was read-only, even with a writable --cache-dir. Only slow views are signed, and a key
    # that cannot be read or created leaves them unsigned and checked.
    def unavailable(self):
        raise OSError("read-only user cache dir")

    monkeypatch.setattr(Cache, "secret_key", unavailable)
    cache = Cache(sample_copy, cache_dir)
    static = cache.write_view("fixtures", {"rows": []}, {})
    assert not static.verified and not cache._view_path("fixtures").with_suffix(".mac").exists()
    first = served()
    assert first.built_now and not first.record.verified
    checked = _counting_checks(monkeypatch)
    again = served()
    assert not again.built_now and checked
