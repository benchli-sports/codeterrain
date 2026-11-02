from __future__ import annotations

import json
import os
import shutil
import subprocess
import time

import pytest

from codeterrain import __version__
from codeterrain import cache as cache_module
from codeterrain.cache import (
    CACHE_ENV,
    Cache,
    CacheBusy,
    Checkouts,
    age_text,
    changed_files,
    checkouts,
    find_cache_dir,
)
from codeterrain.config import resolve_root


def _git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env=cache_module.git_env(),
    )


def test_override_then_env_then_git_common_dir(tmp_path, monkeypatch):
    repo = resolve_root(tmp_path / "repo")
    repo.mkdir()
    _git(repo, "init", "-q")
    assert find_cache_dir(repo) == repo / ".git" / "codeterrain"
    monkeypatch.setenv(CACHE_ENV, str(tmp_path / "from-env"))
    assert find_cache_dir(repo) == resolve_root(tmp_path / "from-env")
    assert find_cache_dir(repo, tmp_path / "flag") == resolve_root(tmp_path / "flag")


def test_linked_worktrees_share_one_cache_dir(tmp_path):
    main = resolve_root(tmp_path / "main")
    main.mkdir()
    _git(main, "init", "-q")
    (main / "a.txt").write_text("x", encoding="utf-8")
    _git(main, "add", "a.txt")
    _git(main, "commit", "-q", "-m", "init")
    linked = resolve_root(tmp_path / "linked")
    _git(main, "worktree", "add", "-q", str(linked), "-b", "other")
    assert find_cache_dir(linked) == find_cache_dir(main) == main / ".git" / "codeterrain"
    assert Cache(linked).root_id != Cache(main).root_id


def _worktrees(tmp_path):
    main = resolve_root(tmp_path / "main")
    main.mkdir()
    _git(main, "init", "-q")
    (main / "a.txt").write_text("x", encoding="utf-8")
    _git(main, "add", "a.txt")
    _git(main, "commit", "-q", "-m", "init")
    linked = resolve_root(tmp_path / "linked")
    _git(main, "worktree", "add", "-q", str(linked), "-b", "other")
    return main, linked


def test_checkouts_names_the_checkout_and_the_repository_s_others(tmp_path):
    main, linked = _worktrees(tmp_path)
    (main / "sub").mkdir()
    assert checkouts(main / "sub") == Checkouts(main, False, (linked,))
    assert checkouts(linked) == Checkouts(linked, True, (main,))
    plain = resolve_root(tmp_path / "plain")
    plain.mkdir()
    _git(plain, "init", "-q")
    assert checkouts(plain) == Checkouts(plain, False, ())


def test_checkouts_with_a_separate_git_dir(tmp_path):
    # `git init --separate-git-dir`: the main checkout has a `.git` file, as a worktree does.
    meta, main = tmp_path / "meta", resolve_root(tmp_path / "main")
    _git(tmp_path, "init", "-q", "--separate-git-dir", str(meta), str(main))
    (main / "a.txt").write_text("x", encoding="utf-8")
    _git(main, "add", "a.txt")
    _git(main, "commit", "-q", "-m", "init")
    linked, second = resolve_root(tmp_path / "linked"), resolve_root(tmp_path / "second")
    _git(main, "worktree", "add", "-q", str(linked), "-b", "one")
    _git(main, "worktree", "add", "-q", str(second), "-b", "two")
    assert checkouts(main) == Checkouts(main, False, (linked, second))
    # From a linked worktree the main checkout cannot be found (git lists the git dir instead).
    assert checkouts(linked) == Checkouts(linked, True, (second,))


def test_checkouts_skips_a_removed_worktree_and_unreadable_files(tmp_path):
    main, linked = _worktrees(tmp_path)
    shutil.rmtree(linked)  # removed by hand: git still lists it until `git worktree prune`
    assert checkouts(main) == Checkouts(main, False, ())
    odd = tmp_path / "odd"
    odd.mkdir()
    (odd / ".git").write_text("not a pointer\n", encoding="utf-8")
    assert checkouts(odd) is None


def test_scan_hashes_once_then_trusts_mtime_and_size(tmp_path, monkeypatch):
    root = resolve_root(tmp_path / "r")
    (root / "pkg").mkdir(parents=True)
    target = root / "pkg" / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")
    cache = Cache(root, tmp_path / "c")
    first = cache.scan(None)
    cache.flush()
    assert list(first) == ["pkg/a.py"]

    def boom(path):
        raise AssertionError("memoised file was re-hashed")

    monkeypatch.setattr(cache_module, "_sha256_file", boom)
    assert Cache(root, tmp_path / "c").scan(None) == first

    monkeypatch.undo()
    target.write_text("x = 22\n", encoding="utf-8")
    changed = Cache(root, tmp_path / "c").scan(None)
    assert changed["pkg/a.py"] != first["pkg/a.py"]


def test_scan_skips_vendored_dirs_and_forgets_deleted_files(tmp_path):
    root = resolve_root(tmp_path / "r")
    for rel in ("src/a.py", ".venv/lib/b.py", "node_modules/c.py", "src/__pycache__/d.py", "e.txt"):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n", encoding="utf-8")
    cache = Cache(root, tmp_path / "c")
    assert list(cache.scan(None)) == ["src/a.py"]
    (root / "src" / "a.py").unlink()
    assert cache.scan(None) == {}
    cache.flush()
    assert Cache(root, tmp_path / "c").scan(None) == {}


def test_one_walk_serves_each_suffix_once_and_a_repeat_scan_walks_again(tmp_path, monkeypatch):
    root = resolve_root(tmp_path / "r")
    for rel in ("a.py", "infra/main.tf", "venv/pyvenv.cfg", "venv/lib/x.py"):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n", encoding="utf-8")
    cache = Cache(root, tmp_path / "c")
    walks = []
    real = os.scandir
    monkeypatch.setattr(os, "scandir", lambda p: walks.append(p) or real(p))
    assert list(cache.scan(None)) == ["a.py"]  # a virtual environment is skipped whole
    first = len(walks)
    assert list(cache.scan(None, (".tf",))) == ["infra/main.tf"]
    assert len(walks) == first  # another suffix: the same listing
    (root / "b.py").write_text("x\n", encoding="utf-8")
    assert sorted(cache.scan(None)) == ["a.py", "b.py"]  # .py again: walked again
    assert len(walks) == 2 * first


def test_a_checked_unsigned_view_is_signed_again_with_its_build_time(tmp_path):
    cache = Cache(resolve_root(tmp_path / "r"), tmp_path / "c")
    cache.write_view("v", {"format": "f1", "rows": [1]}, {"a.py": "h"})  # unsigned
    record = cache.read_view("v")
    assert not record.verified
    checks = []
    served = cache.served(record, "f1", lambda data: checks.append(data) or True)
    assert served.verified and served.built_at == record.built_at and len(checks) == 1
    again = cache.read_view("v")
    assert again.verified and cache.served(again, "f1", lambda data: checks.append(data)) is again
    assert len(checks) == 1  # signed now: not checked again
    assert cache.served(again, "f2", lambda data: False) is None  # another format: checked


def test_scan_of_subpaths_leaves_other_memo_entries_alone(tmp_path):
    root = resolve_root(tmp_path / "r")
    for rel in ("tests/t.py", "src/s.py"):
        path = root / rel
        path.parent.mkdir(parents=True)
        path.write_text("x\n", encoding="utf-8")
    cache = Cache(root, tmp_path / "c")
    assert sorted(cache.scan(None)) == ["src/s.py", "tests/t.py"]
    assert list(cache.scan(["tests"])) == ["tests/t.py"]
    assert sorted(cache._memo()) == ["src/s.py", "tests/t.py"]


def test_view_round_trip_is_lf_json_and_replaces_atomically(tmp_path):
    cache = Cache(resolve_root(tmp_path), tmp_path / "c")
    assert cache.read_view("fixtures") is None
    cache.write_view("fixtures", {"rows": [1]}, {"a.py": "h1"})
    record = cache.write_view("fixtures", {"rows": [2]}, {"a.py": "h2"})
    loaded = cache.read_view("fixtures")
    assert loaded == record
    assert loaded.data == {"rows": [2]}
    raw = (tmp_path / "c" / "views" / f"fixtures-{cache.root_id}.json").read_bytes()
    assert b"\r" not in raw
    assert not list((tmp_path / "c").rglob("*.tmp"))


def test_corrupt_view_reads_as_missing(tmp_path):
    cache = Cache(resolve_root(tmp_path), tmp_path / "c")
    cache.write_view("routes", {}, {})
    (tmp_path / "c" / "views" / f"routes-{cache.root_id}.json").write_bytes(b"{not json")
    assert cache.read_view("routes") is None


def test_a_deeply_nested_cache_file_reads_as_missing(tmp_path):
    # Security review P3-2: json.loads raised RecursionError, which no caller caught.
    cache = Cache(resolve_root(tmp_path), tmp_path / "c")
    cache.write_view("routes", {}, {})
    path = tmp_path / "c" / "views" / f"routes-{cache.root_id}.json"
    path.write_bytes(b"[" * 100000 + b"]" * 100000)
    assert cache.read_view("routes") is None


def test_changed_files_counts_edits_additions_and_removals():
    assert changed_files({"a": "1", "b": "2"}, {"a": "1", "b": "3", "c": "4"}) == 2
    assert changed_files({"a": "1", "gone": "2"}, {"a": "1"}) == 1
    assert changed_files({"a": "1"}, {"a": "1"}) == 0


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(5, "5s"), (125, "2m"), (7300, "2h"), (3 * 86400 + 5, "3d")],
)
def test_age_text(seconds, expected):
    now = time.time()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - seconds))
    assert age_text(stamp, now=now) == expected


def test_scan_skips_hidden_dirs_and_virtualenvs_with_any_name(tmp_path):
    root = resolve_root(tmp_path / "r")
    for rel in ("src/a.py", ".claude/worktrees/w/tests/t.py", "myenv/lib/b.py", "keep/c.py"):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n", encoding="utf-8")
    (root / "myenv" / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")
    assert sorted(Cache(root, tmp_path / "c").scan(None)) == ["keep/c.py", "src/a.py"]


def _fallback_env(monkeypatch, tmp_path):
    base = tmp_path / "userdata"
    monkeypatch.setenv("LOCALAPPDATA", str(base / "local"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(base / "xdg"))
    return base


@pytest.mark.parametrize("windows", [True, False])
def test_the_fallback_is_a_per_user_cache_dir_never_inside_the_root(tmp_path, monkeypatch, windows):
    base = _fallback_env(monkeypatch, tmp_path)
    monkeypatch.setattr(cache_module, "ON_WINDOWS", windows)
    monkeypatch.setattr(
        cache_module.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no git"))
    )
    root = resolve_root(tmp_path / "repo")
    root.mkdir()
    found = find_cache_dir(root)
    expected_base = base / ("local" if windows else "xdg")
    assert found == expected_base / "codeterrain" / cache_module.root_id(root)
    assert root not in found.parents
    assert not (root / ".codeterrain").exists()


def test_a_real_directory_outside_any_repository_gets_the_user_cache(tmp_path, monkeypatch):
    base = _fallback_env(monkeypatch, tmp_path)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    root = resolve_root(tmp_path / "plain")
    root.mkdir()
    found = find_cache_dir(root)
    assert base in found.parents and root not in found.parents


def test_a_git_failure_such_as_exit_128_falls_back_too(tmp_path, monkeypatch):
    base = _fallback_env(monkeypatch, tmp_path)

    def refuse(*args, **kwargs):
        raise subprocess.CalledProcessError(128, "git", stderr="fatal: detected dubious ownership")

    monkeypatch.setattr(cache_module.subprocess, "run", refuse)
    assert base in find_cache_dir(resolve_root(tmp_path)).parents


def test_explicit_cache_locations_still_win_over_the_fallback(tmp_path, monkeypatch):
    _fallback_env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        cache_module.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no git"))
    )
    root = resolve_root(tmp_path)
    assert find_cache_dir(root, tmp_path / "flag") == resolve_root(tmp_path / "flag")
    monkeypatch.setenv(CACHE_ENV, str(tmp_path / "env"))
    assert find_cache_dir(root) == resolve_root(tmp_path / "env")


def test_git_is_asked_without_variables_that_redirect_the_repository(tmp_path, monkeypatch):
    repo = resolve_root(tmp_path / "repo")
    repo.mkdir()
    _git(repo, "init", "-q")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    for name in cache_module.GIT_ENV_VARS:
        monkeypatch.setenv(name, str(elsewhere))
    assert find_cache_dir(repo) == repo / ".git" / "codeterrain"
    assert not set(cache_module.git_env()) & cache_module.GIT_ENV_VARS


@pytest.mark.parametrize(("windows", "same"), [(True, True), (False, False)])
def test_root_id_folds_case_only_on_windows(monkeypatch, tmp_path, windows, same):
    monkeypatch.setattr(cache_module, "ON_WINDOWS", windows)
    upper, lower = tmp_path / "Repo", tmp_path / "repo"
    assert (cache_module.root_id(upper) == cache_module.root_id(lower)) is same


def _view_file(cache, view="routes"):
    return cache.dir / "views" / f"{view}-{cache.root_id}.json"


def _rewrite_view(cache, **changes):
    path = _view_file(cache)
    raw = json.loads(path.read_bytes())
    raw.update(changes)
    path.write_bytes(json.dumps(raw).encode("utf-8"))


def test_views_carry_the_format_the_version_and_the_key(tmp_path):
    cache = Cache(resolve_root(tmp_path), tmp_path / "c")
    cache.write_view("routes", {"x": 1}, {"a.py": "h"}, key="k1")
    raw = json.loads(_view_file(cache).read_bytes())
    assert raw["format"] == cache_module.FORMAT and raw["codeterrain"] == __version__
    assert cache.read_view("routes").key == "k1"


@pytest.mark.parametrize(
    "damage",
    [
        {"format": 0},
        {"format": "1"},
        {"codeterrain": "0.0.1"},
        {"built_at": 5},
        {"built_at": "yesterday"},
        {"fingerprint": []},
        {"key": 3},
    ],
)
def test_a_view_of_another_format_version_or_shape_reads_as_absent(tmp_path, damage):
    cache = Cache(resolve_root(tmp_path), tmp_path / "c")
    cache.write_view("routes", {"x": 1}, {}, key="k")
    _rewrite_view(cache, **damage)
    assert cache.read_view("routes") is None


@pytest.mark.parametrize("missing", ["format", "codeterrain", "built_at", "fingerprint", "data"])
def test_a_view_missing_any_field_reads_as_absent_never_a_key_error(tmp_path, missing):
    cache = Cache(resolve_root(tmp_path), tmp_path / "c")
    cache.write_view("routes", {"x": 1}, {}, key="k")
    path = _view_file(cache)
    raw = json.loads(path.read_bytes())
    del raw[missing]
    path.write_bytes(json.dumps(raw).encode("utf-8"))
    assert cache.read_view("routes") is None


def test_objects_are_stamped_and_an_old_shape_or_other_version_file_reads_empty(tmp_path):
    cache = Cache(resolve_root(tmp_path), tmp_path / "c")
    cache.save_objects("static", {"sha": {"fixtures": []}})
    assert cache.load_objects("static") == {"sha": {"fixtures": []}}
    path = tmp_path / "c" / "objects-static.json"
    raw = json.loads(path.read_bytes())
    assert raw["format"] == cache_module.FORMAT and raw["codeterrain"] == __version__
    path.write_bytes(json.dumps({"sha": {"fixtures": []}}).encode("utf-8"))
    assert cache.load_objects("static") == {}
    path.write_bytes(json.dumps({**raw, "codeterrain": "0.0.1"}).encode("utf-8"))
    assert cache.load_objects("static") == {}
    path.write_bytes(b"[1, 2")
    assert cache.load_objects("static") == {}


def test_a_junk_stat_memo_is_tolerated_and_rebuilt(tmp_path):
    root = resolve_root(tmp_path / "r")
    root.mkdir()
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    first = Cache(root, tmp_path / "c")
    first.scan(None)
    first.flush()
    junk = {"a.py": ["not", "ints"], "b.py": 5, "c.py": [1], "d.py": None}
    first._stat_path.write_bytes(json.dumps(junk).encode("utf-8"))
    assert list(Cache(root, tmp_path / "c").scan(None)) == ["a.py"]


def test_a_file_changed_to_the_same_size_with_a_later_mtime_gets_a_new_sha(tmp_path):
    root = resolve_root(tmp_path / "r")
    root.mkdir()
    target = root / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")
    before = Cache(root, tmp_path / "c")
    sha = before.scan(None)["a.py"]
    before.flush()
    stat = target.stat()
    target.write_text("x = 2\n", encoding="utf-8")
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    assert target.stat().st_size == stat.st_size
    assert Cache(root, tmp_path / "c").scan(None)["a.py"] != sha


def test_a_deleted_file_leaves_the_memo_on_flush(tmp_path):
    root = resolve_root(tmp_path / "r")
    root.mkdir()
    for name in ("a.py", "b.py"):
        (root / name).write_text("x = 1\n", encoding="utf-8")
    cache = Cache(root, tmp_path / "c")
    cache.scan(None)
    cache.flush()
    (root / "b.py").unlink()
    again = Cache(root, tmp_path / "c")
    assert list(again.scan(None)) == ["a.py"]
    again.flush()
    memo = json.loads(again._stat_path.read_bytes())
    assert list(memo) == ["a.py"]


def test_pruning_one_subpath_keeps_memo_entries_of_a_directory_sharing_its_prefix(tmp_path):
    root = resolve_root(tmp_path / "r")
    for rel in ("tests/t.py", "tests_extra/e.py"):
        path = root / rel
        path.parent.mkdir(parents=True)
        path.write_text("x\n", encoding="utf-8")
    cache = Cache(root, tmp_path / "c")
    cache.scan(None)
    (root / "tests" / "t.py").unlink()
    assert cache.scan(["tests"]) == {}
    assert sorted(cache._memo()) == ["tests_extra/e.py"]


def test_only_real_vendored_dirs_are_skipped_not_packages_called_env_or_venv(tmp_path):
    root = resolve_root(tmp_path / "r")
    wanted = ("env/settings.py", "venv/helpers.py", "src/a.py")
    skipped = ("build/b.py", "dist/d.py", "node_modules/n.py", "pkg.egg-info/e.py")
    for rel in (*wanted, *skipped):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n", encoding="utf-8")
    assert sorted(Cache(root, tmp_path / "c").scan(None)) == sorted(wanted)


def test_replace_retries_a_locked_file_then_succeeds(tmp_path, monkeypatch):
    calls = []
    real = os.replace

    def flaky(src, dst):
        calls.append(src)
        if len(calls) < 3:
            raise PermissionError("locked")
        return real(src, dst)

    monkeypatch.setattr(cache_module.os, "replace", flaky)
    monkeypatch.setattr(cache_module.time, "sleep", lambda s: None)
    cache = Cache(resolve_root(tmp_path), tmp_path / "c")
    cache.save_objects("static", {"a": 1})
    assert len(calls) == 3
    assert cache.load_objects("static") == {"a": 1}


def test_a_file_that_stays_locked_raises_cache_busy_after_five_tries_and_leaves_no_tmp(
    tmp_path, monkeypatch
):
    tries = []

    def locked(src, dst):
        tries.append(1)
        raise PermissionError("locked")

    monkeypatch.setattr(cache_module.os, "replace", locked)
    monkeypatch.setattr(cache_module.time, "sleep", lambda s: None)
    cache = Cache(resolve_root(tmp_path), tmp_path / "c")
    with pytest.raises(CacheBusy, match="locked by another process"):
        cache.save_objects("static", {"a": 1})
    assert len(tries) == 5
    assert not list((tmp_path / "c").glob("*.tmp"))


def _fake_git(common, top):
    from types import SimpleNamespace

    def run(cmd, **kwargs):
        return SimpleNamespace(stdout=(common if cmd[-1] == "--git-common-dir" else top) + "\n")

    return run


def test_a_repository_that_does_not_contain_the_root_is_not_trusted_with_the_cache(
    tmp_path, monkeypatch
):
    base = _fallback_env(monkeypatch, tmp_path)
    root = resolve_root(tmp_path / "checkout")
    root.mkdir()
    elsewhere = resolve_root(tmp_path / "other-repo")
    monkeypatch.setattr(
        cache_module.subprocess, "run", _fake_git(str(elsewhere / ".git"), str(elsewhere))
    )
    found = find_cache_dir(root)
    assert base in found.parents and elsewhere not in found.parents


def test_a_repository_that_contains_the_root_places_the_cache_in_its_git_dir(
    tmp_path, monkeypatch
):
    repo = resolve_root(tmp_path / "repo")
    root = repo / "services" / "api"
    root.mkdir(parents=True)
    monkeypatch.setattr(cache_module.subprocess, "run", _fake_git(str(repo / ".git"), str(repo)))
    assert find_cache_dir(root) == repo / ".git" / "codeterrain"


def test_a_subdirectory_of_a_real_repository_shares_that_repositorys_cache(tmp_path):
    repo = resolve_root(tmp_path / "repo")
    (repo / "svc").mkdir(parents=True)
    _git(repo, "init", "-q")
    assert find_cache_dir(repo / "svc") == repo / ".git" / "codeterrain"


def test_a_file_outside_the_root_is_never_hashed(tmp_path):
    # Security review r2 P3-4: the fingerprint hashed files through links out of the root. A
    # route manifest outside the root is refused by the config since the security fold.
    root = resolve_root(tmp_path / "repo")
    root.mkdir()
    inside = root / "inside.json"
    inside.write_text("{}", encoding="utf-8")
    outside = resolve_root(tmp_path / "shared") / "manifest.json"
    outside.parent.mkdir()
    outside.write_text('{"routes": []}', encoding="utf-8")
    found = Cache(root, tmp_path / "cache").hash_files([inside, outside, root / "missing.json"])
    assert set(found) == {"inside.json"}


@pytest.mark.parametrize(
    "name", ["GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY"]
)
def test_each_git_variable_that_redirects_a_repository_is_stripped(monkeypatch, name):
    monkeypatch.setenv(name, "/elsewhere")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "kept")
    env = cache_module.git_env()
    assert name not in env and env["GIT_AUTHOR_NAME"] == "kept"
    assert name in cache_module.GIT_ENV_VARS


def test_the_walk_skips_file_links_that_leave_the_root(tmp_path):
    # Security review P3-5: tests/x.py -> ~/secret was read and hashed.
    root = resolve_root(tmp_path / "repo")
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_in.py").write_text("x = 1\n", encoding="utf-8")
    secret = tmp_path / "secret.py"
    secret.write_text("TOKEN = 'CANARY'\n", encoding="utf-8")
    try:
        (root / "tests" / "test_out.py").symlink_to(secret)
        (root / "tests" / "test_alias.py").symlink_to(root / "tests" / "test_in.py")
    except OSError:
        pytest.skip("file symlinks need privileges on this Windows host; covered on Linux CI")
    found = Cache(root, tmp_path / "cache").scan(["tests"])
    assert sorted(found) == ["tests/test_alias.py", "tests/test_in.py"]


def test_a_scan_start_or_hashed_file_that_leads_out_of_the_root_is_skipped(tmp_path):
    # Security review r2 P3-4: the scan followed a `tests` junction or symlink out of the root;
    # config now refuses it, and the scan and the fingerprint hash refuse it on their own too.
    (tmp_path / "repo").mkdir()
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "test_x.py").write_text("import pytest\n", encoding="utf-8")
    (tmp_path / "outside" / "pyproject.toml").write_text("[x]\n", encoding="utf-8")
    root = resolve_root(tmp_path / "repo")
    try:
        (root / "tests").symlink_to(tmp_path / "outside", target_is_directory=True)
    except OSError:
        subprocess.run(["cmd", "/c", "mklink", "/J", str(root / "tests"),
                        str(tmp_path / "outside")], check=True, capture_output=True)
    cache = Cache(root, tmp_path / "cache")
    assert cache.scan(["tests"]) == {}
    assert cache.scan(["tests/test_x.py"]) == {}
    assert cache.hash_files([root / "tests" / "pyproject.toml"]) == {}


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes; Windows relies on the per-user ACL")
def test_cache_dirs_and_files_are_private_whatever_the_umask(tmp_path, monkeypatch):
    # Security review P3-7: mkdir/write_bytes followed the umask (0755/0644).
    old = os.umask(0o022)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "user"))
    try:
        cache = Cache(resolve_root(tmp_path), tmp_path / "c")
        cache.write_view("routes", {}, {})
        cache.secret_key()
    finally:
        os.umask(old)
    for directory in (tmp_path / "c", tmp_path / "c" / "views", tmp_path / "user" / "codeterrain"):
        assert directory.stat().st_mode & 0o777 == 0o700
    key = tmp_path / "user" / "codeterrain" / "key"
    for path in (key, *(tmp_path / "c" / "views").iterdir()):
        assert path.stat().st_mode & 0o777 == 0o600


def test_a_stale_tmp_file_from_an_earlier_process_is_replaced(tmp_path):
    cache = Cache(resolve_root(tmp_path), tmp_path / "c")
    cache.write_view("routes", {}, {})
    view = tmp_path / "c" / "views" / f"routes-{cache.root_id}.json"
    stale = view.with_name(f"{view.name}.{os.getpid()}.tmp")
    stale.write_text("old", encoding="utf-8")
    cache.write_view("routes", {"n": 1}, {})
    assert cache.read_view("routes").data == {"n": 1} and not stale.exists()
