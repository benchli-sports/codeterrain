from __future__ import annotations

import os
import subprocess

import pytest

from codeterrain.config import (
    DEFAULT_MANIFEST_KEYS,
    USER_CONFIG_ENV,
    ClientRepo,
    ConfigError,
    load_config,
    resolve_root,
    user_config_path,
)


def _write(path, text):
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def test_defaults_without_any_config(tmp_path):
    config = load_config(resolve_root(tmp_path))
    assert config.source == "defaults"
    assert config.apps == ()
    assert config.test_paths == ("tests",)
    assert config.region == "us-east-1"
    assert config.route_manifest is None
    assert config.manifest_keys == DEFAULT_MANIFEST_KEYS


def test_pyproject_table_is_read_and_manifest_is_root_relative(tmp_path):
    root = resolve_root(tmp_path)
    _write(
        root / "pyproject.toml",
        '[tool.codeterrain]\napps = ["pkg.main:app"]\ntest_paths = ["t", "u"]\n'
        'region = "eu-west-1"\nroute_manifest = "docs/r.json"\nplugins = ["x.y"]\n'
        'import_env = { MODE = "test", N = 3 }\n'
        '[tool.codeterrain.manifest_keys]\naccess = "klass"\n',
    )
    config = load_config(root)
    assert config.source == "pyproject.toml [tool.codeterrain]"
    assert config.apps == ("pkg.main:app",)
    assert config.test_paths == ("t", "u")
    assert config.region == "eu-west-1"
    assert config.route_manifest == (root / "docs" / "r.json").resolve()
    assert config.plugins == ("x.y",)
    assert config.import_env == {"MODE": "test", "N": "3"}
    assert config.manifest_keys["access"] == "klass"
    assert config.manifest_keys["method"] == "method"


def test_config_flag_beats_pyproject(tmp_path):
    root = resolve_root(tmp_path)
    _write(root / "pyproject.toml", '[tool.codeterrain]\napps = ["from_pyproject:app"]\n')
    nested = _write(tmp_path / "nested.toml", '[tool.codeterrain]\napps = ["from_nested:app"]\n')
    flat = _write(tmp_path / "flat.toml", 'apps = ["from_flat:app"]\n')
    assert load_config(root, nested).apps == ("from_nested:app",)
    flat_config = load_config(root, flat)
    assert flat_config.apps == ("from_flat:app",)
    assert flat_config.source.startswith("--config ")


def test_pyproject_without_table_falls_back_to_defaults(tmp_path):
    root = resolve_root(tmp_path)
    _write(root / "pyproject.toml", '[project]\nname = "x"\n')
    assert load_config(root).source == "defaults"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('bogus = 1\n', "unknown config key"),
        ('apps = ["no_colon"]\n', "module:attr"),
        ('apps = "pkg:app"\n', "list of strings"),
        ('import_env = ["a"]\n', "import_env"),
        ('region = ""\n', "region"),
        ('route_manifest = 3\n', "route_manifest"),
        ('manifest_keys = { nope = "x" }\n', "unknown key"),
        ('apps = [\n', "invalid TOML"),
        ('apps = [":attr"]\n', "module:attr"),
        ('apps = ["mod:"]\n', "module:attr"),
        ('apps = ["a:b:c"]\n', "module:attr"),
        ('apps = [3]\n', "list of strings"),
        ('plugins = [1]\n', "plugins"),
        ('plugins = "x.y"\n', "plugins"),
        ('test_paths = "tests"\n', "test_paths"),
        ('manifest_keys = { access = 3 }\n', "manifest_keys"),
        ('manifest_keys = ["access"]\n', "manifest_keys"),
        ('pass_env = "TOKEN"\n', "pass_env"),
        ('pass_env = ["TOKEN", 3]\n', "pass_env"),
        ('respect_addopts = "yes"\n', "respect_addopts"),
        ('respect_addopts = 1\n', "respect_addopts"),
        ('client_paths = "web"\n', "client_paths"),
        ('path_prefix = "v1"\n', "path_prefix"),
        ('path_prefix = 1\n', "path_prefix"),
    ],
)
def test_malformed_config_is_refused_with_the_key_named(tmp_path, text, message):
    bad = _write(tmp_path / "bad.toml", text)
    with pytest.raises(ConfigError, match=message):
        load_config(resolve_root(tmp_path), bad)


def test_missing_config_file_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(resolve_root(tmp_path), tmp_path / "absent.toml")


def test_moto_mock_defaults_off_and_must_be_a_boolean(tmp_path):
    root = resolve_root(tmp_path)
    assert load_config(root).moto_mock is False
    on = _write(tmp_path / "on.toml", "moto_mock = true\n")
    assert load_config(root, on).moto_mock is True
    bad = _write(tmp_path / "bad.toml", 'moto_mock = "yes"\n')
    with pytest.raises(ConfigError, match="moto_mock"):
        load_config(root, bad)


def test_pytest_args_are_a_string_list_defaulting_to_empty(tmp_path):
    root = resolve_root(tmp_path)
    assert load_config(root).pytest_args == ()
    ok = _write(tmp_path / "ok.toml", 'pytest_args = ["-o", "addopts="]\n')
    assert load_config(root, ok).pytest_args == ("-o", "addopts=")
    bad = _write(tmp_path / "bad.toml", "pytest_args = 3\n")
    with pytest.raises(ConfigError, match="pytest_args"):
        load_config(root, bad)


def test_a_codeterrain_entry_in_pyproject_that_is_not_a_table_is_refused(tmp_path):
    root = resolve_root(tmp_path)
    _write(root / "pyproject.toml", "[tool]\ncodeterrain = 3\n")
    with pytest.raises(ConfigError, match="must be a table"):
        load_config(root)


def test_pass_env_and_respect_addopts_default_off_and_round_trip(tmp_path):
    root = resolve_root(tmp_path)
    plain = load_config(root)
    assert plain.pass_env == () and plain.respect_addopts is False
    on = _write(tmp_path / "on.toml", 'pass_env = ["TOKEN", "REGION"]\nrespect_addopts = true\n')
    config = load_config(root, on)
    assert config.pass_env == ("TOKEN", "REGION") and config.respect_addopts is True
    assert config.as_dict()["pass_env"] == ["TOKEN", "REGION"]
    assert config.as_dict()["respect_addopts"] is True


def test_user_only_keys_in_the_repository_pyproject_are_ignored_and_named(tmp_path):
    # Security review P2-2 / P3-6: the target's pyproject is not the user's opt-in.
    root = resolve_root(tmp_path)
    _write(
        root / "pyproject.toml",
        '[tool.codeterrain]\napps = ["pkg:app"]\npass_env = ["GITHUB_TOKEN"]\n'
        'pytest_args = ["-o", "log_file=x.log"]\nrespect_addopts = true\n'
        'client_repos = ["../../home"]\n',
    )
    config = load_config(root)
    assert config.apps == ("pkg:app",)
    assert config.pass_env == () and config.pytest_args == () and config.respect_addopts is False
    assert config.client_repos == ()
    assert config.ignored == ("pass_env", "pytest_args", "respect_addopts", "client_repos")


def test_the_user_config_supplies_user_only_keys_and_config_overrides_it(tmp_path, monkeypatch):
    (tmp_path / "repo").mkdir()
    root = resolve_root(tmp_path / "repo")
    user = _write(tmp_path / "user.toml", 'pass_env = ["DATABASE_URL"]\nrespect_addopts = true\n')
    monkeypatch.setenv(USER_CONFIG_ENV, str(user))
    _write(root / "pyproject.toml", '[tool.codeterrain]\npass_env = ["GITHUB_TOKEN"]\n')
    from_user = load_config(root)
    assert from_user.pass_env == ("DATABASE_URL",) and from_user.respect_addopts is True
    assert from_user.ignored == ("pass_env",) and from_user.user_config == user.as_posix()
    explicit = _write(tmp_path / "explicit.toml", 'pass_env = ["OTHER"]\n')
    both = load_config(root, explicit)
    assert both.pass_env == ("OTHER",) and both.respect_addopts is True and both.ignored == ()


def test_the_user_config_takes_only_user_only_keys(tmp_path, monkeypatch):
    user = _write(tmp_path / "user.toml", 'apps = ["pkg:app"]\n')
    monkeypatch.setenv(USER_CONFIG_ENV, str(user))
    with pytest.raises(
        ConfigError, match="takes only pass_env, pytest_args, respect_addopts, client_repos"
    ):
        load_config(resolve_root(tmp_path))


def test_the_user_config_lives_in_the_platform_config_dir(monkeypatch, tmp_path):
    monkeypatch.delenv(USER_CONFIG_ENV, raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    base = tmp_path / ("roaming" if os.name == "nt" else "xdg")
    assert user_config_path() == base / "codeterrain" / "config.toml"


def _dir_link(link, target):
    """A directory symlink, or a junction where Windows refuses unprivileged symlinks."""
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True,
                       capture_output=True)


@pytest.mark.parametrize("key", ["test_paths", "route_manifest", "client_paths"])
@pytest.mark.parametrize(
    "value",
    ["../outside", "sub/../../outside", "ABSOLUTE"],
    ids=["dotdot", "nested-dotdot", "absolute"],
)
def test_repository_paths_that_leave_the_root_are_refused(tmp_path, key, value):
    # Security review P3-5: test_paths = ["../x"] made the parent read files outside the root.
    (tmp_path / "repo").mkdir()
    root = resolve_root(tmp_path / "repo")
    if value == "ABSOLUTE":
        value = (tmp_path / "outside").as_posix()
    text = f"{key} = {value!r}\n" if key == "route_manifest" else f"{key} = [{value!r}]\n"
    _write(root / "pyproject.toml", "[tool.codeterrain]\n" + text)
    with pytest.raises(ConfigError, match=key):
        load_config(root)


def test_a_link_inside_the_root_that_points_out_of_it_is_refused(tmp_path):
    (tmp_path / "repo").mkdir()
    (tmp_path / "outside").mkdir()
    root = resolve_root(tmp_path / "repo")
    _dir_link(root / "tests", tmp_path / "outside")
    _write(root / "pyproject.toml", '[tool.codeterrain]\ntest_paths = ["tests"]\n')
    with pytest.raises(ConfigError, match="resolves outside the repository root"):
        load_config(root)
    _write(root / "pyproject.toml", '[tool.codeterrain]\nroute_manifest = "tests/m.json"\n')
    with pytest.raises(ConfigError, match="route_manifest"):
        load_config(root)


def test_the_default_tests_dir_as_a_link_out_of_the_root_is_refused(tmp_path):
    # Security review r2 P3-4: only configured test_paths were confined; with no config at all a
    # `tests` junction (or a git-cloned symlink) was walked and printed from outside the checkout.
    (tmp_path / "repo").mkdir()
    (tmp_path / "outside").mkdir()
    root = resolve_root(tmp_path / "repo")
    _dir_link(root / "tests", tmp_path / "outside")
    with pytest.raises(ConfigError, match="test_paths: 'tests' resolves outside"):
        load_config(root)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"tool = 1\n", "must be a table"),
        (b'[tool.codeterrain]\nregion = "' + bytes([0xFF]) + b'"\n', "invalid TOML"),
    ],
    ids=["tool-not-a-table", "not-utf8"],
)
def test_a_malformed_repository_pyproject_is_a_config_error(tmp_path, data, message):
    # Security review r2 P3-7: both raised a raw traceback from every command, static tier too.
    root = resolve_root(tmp_path)
    (root / "pyproject.toml").write_bytes(data)
    with pytest.raises(ConfigError, match=message):
        load_config(root)


def test_paths_inside_the_root_still_resolve(tmp_path):
    root = resolve_root(tmp_path)
    _write(root / "pyproject.toml",
           '[tool.codeterrain]\ntest_paths = ["a/../tests"]\nroute_manifest = "./docs/m.json"\n')
    config = load_config(root)
    assert config.route_manifest == root / "docs" / "m.json"


def test_a_repository_codeterrain_toml_is_read_before_pyproject(tmp_path):
    # v0.2: front ends have no pyproject.toml; codeterrain.toml takes the same keys, nested or at
    # top level, and user-only keys in it are dropped like in pyproject.
    root = resolve_root(tmp_path)
    _write(root / "pyproject.toml", '[tool.codeterrain]\ntest_paths = ["from_pyproject"]\n')
    _write(
        root / "codeterrain.toml",
        'client_paths = ["src"]\npath_prefix = "/v1/"\npass_env = ["X"]\n',
    )
    config = load_config(root)
    assert config.source == "codeterrain.toml"
    assert config.client_paths == ("src",) and config.path_prefix == "/v1"
    assert config.test_paths == ("tests",) and config.ignored == ("pass_env",)
    assert config.pass_env == ()
    _write(root / "codeterrain.toml", '[tool.codeterrain]\nclient_paths = ["web"]\n')
    assert load_config(root).client_paths == ("web",)
    assert load_config(root, _write(tmp_path / "other.toml", "")).client_paths == ()


def test_a_codeterrain_toml_that_links_out_of_the_root_is_refused(tmp_path):
    (tmp_path / "repo").mkdir()
    (tmp_path / "outside").mkdir()
    root = resolve_root(tmp_path / "repo")
    _write(tmp_path / "outside" / "c.toml", 'client_paths = ["src"]\n')
    try:
        os.symlink(tmp_path / "outside" / "c.toml", root / "codeterrain.toml")
    except OSError:
        pytest.skip("file symlinks need privileges here")
    with pytest.raises(ConfigError, match="codeterrain.toml"):
        load_config(root)


def test_client_repos_are_read_from_the_root_and_kept_absolute(tmp_path):
    (tmp_path / "api").mkdir()
    (tmp_path / "web").mkdir()
    root = resolve_root(tmp_path / "api")
    chosen = _write(
        tmp_path / "join.toml", f'client_repos = ["../web", {(tmp_path / "web").as_posix()!r}]\n'
    )
    config = load_config(root, chosen)
    web = resolve_root(tmp_path / "web")
    assert config.client_repos == (ClientRepo(web), ClientRepo(web))
    assert config.as_dict()["client_repos"][0] == {
        "path": web.as_posix(), "path_prefix": None, "client_paths": None
    }
    tables = _write(
        tmp_path / "tables.toml",
        'client_repos = [{ path = "../web", path_prefix = "/v1/", client_paths = ["src"] }]\n',
    )
    assert load_config(root, tables).client_repos == (ClientRepo(web, "/v1", ("src",)),)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ('"../web"', "expected a list"),
        ("[3]", "a path or a table"),
        ('[{ path_prefix = "/v1" }]', "a path or a table"),
        ('[{ path = "../web", bogus = 1 }]', "unknown key"),
        ('[{ path = "../web", path_prefix = "v1" }]', "client_repos.path_prefix"),
        ('[{ path = "../web", client_paths = "src" }]', "client_paths"),
    ],
)
def test_malformed_client_repos_are_refused(tmp_path, value, message):
    bad = _write(tmp_path / "bad.toml", f"client_repos = {value}\n")
    with pytest.raises(ConfigError, match=message):
        load_config(resolve_root(tmp_path), bad)
