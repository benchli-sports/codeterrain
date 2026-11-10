# Configuration

Where codeterrain reads its settings, every key with its default, and non-default layouts.

## Configuration

Precedence, exactly as the loader implements it, first match wins and nothing is merged:

1. `--config FILE` (a relative path is read from the current directory; a missing file is an error). The
   file's `[tool.codeterrain]` table is used, or, if it has none, its top-level keys.
2. `codeterrain.toml` in the repository root, read like a `--config` file. It is meant for repositories
   without a `pyproject.toml`, such as front ends; when it exists, `pyproject.toml` is not read. A
   `codeterrain.toml` that is a link out of the root is an error.
3. `[tool.codeterrain]` in the repository root's `pyproject.toml`. A `pyproject.toml` without that table
   is ignored.
4. Defaults.

**User-only keys.** `pass_env`, `pytest_args` and `respect_addopts` widen what the sandboxed code gets
(your environment variables, extra pytest options), and `client_repos` makes `codeterrain` read other
checkouts, so they are honoured only from `--config` or your user config, never from the repository you
are reading. When a repository's `pyproject.toml` or
`codeterrain.toml` sets them, they are ignored and `codeterrain` prints one warning line naming them. The
user config is a TOML file with only those keys at top level:

- `CODETERRAIN_USER_CONFIG`, if set;
- otherwise `%APPDATA%\codeterrain\config.toml` on Windows and `$XDG_CONFIG_HOME/codeterrain/config.toml`
  elsewhere (default `~/.config/codeterrain/config.toml`).

It fills in user-only keys that `--config` does not set, for every repository you run on, including
ones you do not trust: a `pass_env` there hands those values to every checkout you inspect. To give a
value to one repository only, put it in a `--config` file for that run. `codeterrain doctor` prints which user
config was used.

An unknown key or a value of the wrong type is an error (exit 2) naming the key, and so is a
`pyproject.toml` that is not valid TOML or UTF-8. `test_paths` (the default `tests` included),
`client_paths` and `route_manifest` are relative to the repository root, not to the config file, and must stay inside it: an
absolute path, a `..` that leaves the root, or a symlink or junction pointing out of it is an error
(exit 2). `codeterrain doctor` prints which source was used. Every key is optional. The example shows a value
for each; the default is in the comment. The keys marked "user only" take effect in a `--config` file or
the user config, not in the repository's own `pyproject.toml`.

```toml
[tool.codeterrain]
apps = ["myservice.main:app"]          # default []; "module:attr" entries the route walker imports
test_paths = ["tests"]                  # default ["tests"]
client_paths = ["src"]                  # default []: the whole repository; where `codeterrain calls` looks
path_prefix = "/v1"                     # default ""; put before every front-end call path when matching routes
region = "us-east-1"                    # default "us-east-1"; the AWS region inside the sandbox
import_env = { APP_ENV = "test" }       # default {}; extra environment for the import and pytest subprocesses
pass_env = ["DATABASE_URL"]             # default []; user only: parent variables to copy through (see sandbox.md)
route_manifest = "docs/routes.json"     # default none; joined to routes for the access class
plugins = ["my_tools.terrain_plugin"]   # default []; extra pluggy plugins (see hookspecs)
pytest_args = ["-m", "not slow"]        # default []; user only: extra pytest arguments for the extraction run
respect_addopts = false                 # default false; user only: the project's addopts are switched off (-o addopts=)
client_repos = ["../web", { path = "../admin", path_prefix = "/v1", client_paths = ["src"] }]
                                        # default []; user only: front ends for `codeterrain callers`, relative to the root;
                                        # a table's path_prefix and client_paths win over that checkout's own config
moto_mock = false                       # default false: true wraps app imports in moto.mock_aws() when moto is importable

[tool.codeterrain.manifest_keys]        # where the manifest keeps its fields (defaults shown)
rows = "routes"
method = "method"
path = "path"
access = "access"
apps = "apps"
```

## Monorepos and non-default layouts

- `test_paths` is relative to the root; point it at `apps/api/tests` if that is where the tests live
  (`codeterrain doctor` marks a missing path). Node ids are the ones pytest prints, relative to its rootdir.
- List every test tree: a second one beside `tests` (a live suite with its own `conftest.py` chain)
  is not read otherwise. A top-level directory outside `test_paths` that holds a `conftest.py` or test
  modules is named in a note on `fixtures` and `fixture` answers (`unlisted_test_paths` with
  `--json`) and in `codeterrain doctor`.
- The `conftest.py` of each folder above a test path, up to the root (a root `conftest.py`, or
  `apps/api/conftest.py` above `apps/api/tests`), is read with it: pytest loads those for the tests
  below them. A sibling folder's `conftest.py` is not on that way up and is not read.
- Put source roots on the target's path with `import_env = { PYTHONPATH = "apps/api" }`.
- The project's `addopts` are switched off during extraction (see [the sandbox](sandbox.md)), so `--cov` and report
  options cannot write into the repository.
- Never scanned: hidden directories, `.git`, `.venv`, `node_modules`, `__pycache__`, `.tox`, `.nox`,
  `build`, `dist`, `site-packages`, `*.egg-info`, tool caches, junctions, and any directory that holds a
  `pyvenv.cfg` (a virtualenv, whatever its name). A package called `env` or `venv` is scanned. A
  `test_paths` entry that is itself a link out of the root, the default `tests` included, is an error.
