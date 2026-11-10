# Commands

Every `codeterrain` command, its options and what each one reads.

## Install

From a checkout (not published yet):

```
uv tool install <path-to-this-checkout>
uv tool install "<path-to-this-checkout>[terraform]"   # with the Terraform language pack
uv tool install "<path-to-this-checkout>[typescript]"  # front-end calls read with tree-sitter
```

Python 3.12 or newer. The only runtime dependency is `pluggy`. Language packs, in
[packages/](../packages/), are extras: Terraform adds `.tf` files to `codeterrain def` and `codeterrain refs`;
TypeScript reads the front-end files of `codeterrain calls` and `codeterrain callers` with tree-sitter (its
only native dependency, with prebuilt wheels) in place of the built-in tokenizer.

`codeterrain` runs the import and pytest tier in the **target's** interpreter, not its own. Point
`--python` (or `CODETERRAIN_PYTHON`) at the target's virtualenv interpreter, for example
`--python .venv/bin/python` (`.venv\Scripts\python.exe` on Windows). That environment needs `pytest`
for `setup-order` and `pluggy` for `routes` (the route walker imports it; it is not copied into the
sandbox because it would shadow the pluggy that pytest itself uses). `codeterrain doctor` prints a hint
when `--python` is codeterrain's own interpreter and the target has a `.venv`.

## Commands

```
codeterrain fixtures [substring] [--autouse] [--scope SCOPE]
codeterrain fixture NAME
codeterrain doctor
codeterrain setup-order NODEID
codeterrain routes [substring] [--app APP] [--access ACCESS]
codeterrain route METHOD PATH
codeterrain raises NAME [--exception E] [--status N] [--access ACCESS] [--depth N]
codeterrain calls [substring] [--method METHOD] [--unresolved]
codeterrain callers [METHOD PATH] [--unmatched]
codeterrain def [NAME] [--kind KIND]
codeterrain refs NAME [--tests|--code] [--by-name]
codeterrain importers MODULE [--tests|--code]
codeterrain imports MODULE
codeterrain classes NAME
codeterrain env NAME [--tests|--code]
codeterrain impact NAME [--depth N]
codeterrain refresh [fixtures|calls|order|routes|code|languages]
codeterrain --version
```

`--scope` takes `session`, `package`, `module`, `class` or `function`, or the letter `S`, `P`, `M`, `C`
or `F`. `--app` keeps the apps whose `module:attr` contains the text; `--access` keeps one access class
from the route manifest. `codeterrain routes TEXT` keeps the routes whose path or handler contains the
text; when none does, it lists the routes whose handler reads or calls, in its own body, a name
containing it (`codeterrain routes legal` finds the handler that reads `LEGAL_PRIVACY_VERSION`), from the
code index; `--json` lists them as `handlers_reading`, with `handlers_reading_truncated` when the search
stopped early. `NODEID` is a pytest node id; a function, class or file matches its tests.
`codeterrain fixture NAME` prints each definition of the fixture and every test and fixture pytest
gives it: by argument, `usefixtures` or `getfixturevalue`, and (`via autouse`) every test an autouse
definition runs for. A test method a class inherits is listed in that class, with `inherited from
FILE:LINE` naming the method's own place; a test that parametrizes the name directly is not listed. When there are several definitions, each dependent ends with `gets FILE:LINE`: the one
pytest gives it, and so the one to edit for it (a test gets the nearest up from its class and its
bases, its module and its conftest folders; an override requesting its own name gets the one it
overrides; another fixture shows `per test`, since pytest resolves that request where each test
sits; `-` when none is visible). `--json` carries these as `gets` and `inherited` on each
dependent. The answer reads the code index, built on first use, as `impact` does.
`codeterrain calls` lists the HTTP calls in `.ts`, `.tsx`, `.js`, `.jsx`, `.mjs`, `.cjs`, `.mts` and `.cts`
files: `<client>.get(path)` and the other verbs, `fetch(path, {method})` and object literals with a
`url:` path (`useCustom({url, method})`, axios configs). A path written as a string, a template (`${id}`
shows as `{}`), a `+` concatenation or a same-file `const` is resolved; a call whose path is only known at
run time is listed with `?` and the expression, and `--unresolved` lists only those. Test, story,
declaration and minified files, `__tests__`, `__mocks__` and `mocks` directories and build output (`out`,
`coverage`, besides the `build` and `dist` every view skips) are left out. With the TypeScript pack
installed the files are parsed, not tokenized: names resolve through scopes (a `const url` inside a
function is that function's), and a file with a syntax error is reported with its lines, its calls
there unresolved. `refresh` with no argument rebuilds every view and the code index; `refresh
languages` rebuilds the views of the installed language packs.

`codeterrain def NAME` and `codeterrain refs NAME` read the code index: every `.py` file in the repository,
application code and tests alike, except folders that packages were installed into (a folder holding a
`*.dist-info`, or an `*.egg-info` that records installed files or has no source package beside it,
such as a vendored deployment bundle; an editable install's egg-info beside its package, matched by
name or by its `top_level.txt`, is not one; that metadata is read only from a regular file reached
through no link). `NAME` is a full dotted name
(`app.services.orders.create`), a qualified name (`Service.place`) or a bare name; a name re-exported by
a package resolves to where it is defined, and an unknown name lists similar ones. `--kind KIND`
keeps only definitions of one kind (`class`, `function`, `method`, or a language pack's: `module`,
`provider`, `required_provider`); without a name it lists every one of them, so
`codeterrain def --kind module` is a Terraform checkout's module tree, each call with its file and
`source`. An unknown kind lists the kinds there are. `src/` is dropped
from module names unless it has its own `__init__.py`. Names start at the repository root, so two
apps' modules never collide; code in a folder with its own `pyproject.toml`, `setup.py` or
`setup.cfg` (an app in a monorepo) imports from that folder or its `src/`, and an import there the
index does not know is looked up in that folder. `refs` lists:

- the calls that resolve to the definition through the file's imports, `self`/`cls` or a constructor
  receiver (`Service(repo).place(...)`) or a module or class variable made by its class
  (`settings = Settings()`, `settings: Settings = ...`), each with its shape (`2 positional; kw user`) and what does not
  fit the signature: too many positional arguments, an unknown keyword, a positional-only argument
  passed by keyword, two values for one parameter, a missing keyword-only or positional argument. A
  call with `*args` or `**kwargs` is never flagged;
- the places it is used as a value, each saying how: passed to a call (`Depends(get_settings)`, a
  callback), assigned, combined into another value (`TOTAL += VALUE`), returned, compared, a default,
  a member of a set, list, tuple or dict literal, or read anywhere else (an `if` test, an operand, a
  loop's iterable, an f-string, an annotation); a local variable of the same name is not. A module or class variable assigned straight from it (`TABLE_NAME = settings.TABLE`,
  as its last assignment) is followed one level, up to 20 of them: its sites are listed too, marked `via TABLE_NAME`,
  leaving out the module code and class bodies of its file that run before that assignment;
- the places it is patched (`mock.patch("...")`, `patch.object`, `monkeypatch.setattr`), where the real
  one does not run;
- with `--by-name`, the calls of the same attribute on a receiver whose type is not known statically
  (`svc.place(...)` after `svc = make()`), which may or may not be this definition. Without it, only
  their count is shown.

`--tests` and `--code` keep only test files or only the rest. A test file is a `test_*.py` or
`*_test.py`, or a file under `test_paths`.

`codeterrain importers MODULE` lists every import of a Python module, or of a name from it, in other
files: `import app.orders`, `from app.orders import create`, `from app import orders`, a
relative import, a star import (`app.orders.*`), or one inside a function, each with its file:line, scope, what it imports and
the local name (`--tests` and `--code` as for `refs`). `MODULE` is a full dotted name or its last
parts (`orders`); a namespace package (no `__init__.py`) counts too. A plain `import app.orders` binds `app`, so it is listed for the package
`app`. `codeterrain imports MODULE` lists what the module imports, resolved to full names. `def`
and `refs` on a module name point here.

`codeterrain classes NAME` lists a class's bases (each with where the repository defines it, or
`outside`), its method resolution order as far as the repository places it, and every class below
it across modules, nearest first, with its depth and the base it names on the way. Bases resolve
through imports, aliases and re-exports as `def` resolves names, so an unrelated class of the same
name is not counted. At most 2,000 subclasses are listed; the answer says when it was cut.

`codeterrain env NAME` follows an environment variable end to end: where infrastructure sets it (the
Terraform pack reports each `env.NAME` it finds: a key of an `environment`/`variables`/`env` map, or the
`name` of an `environment = [{ name, value }]` entry or an `env` block, with the resource it is set in;
its value is never read into the cache or the answer), the pydantic-settings fields that read it (a
class whose bases reach `BaseSettings`, its literal `env_prefix` and the field name in any case, or a
literal `alias`/`validation_alias`, inherited along the bases) with each field's readers as `refs`
finds them, and every direct read through `os` (`os.environ[...]`, `.get`, `os.getenv`, `in
os.environ`, sets and deletes, under any import name). Only a name written as a string literal is
seen.

`codeterrain raises NAME` answers "this function raises: which routes reach it, and with what status?".
It lists the exceptions the function's own body raises (a nested function's raises are its own),
with the status the raise site states (`status_code=404`, `HTTPException(404)`,
`status.HTTP_404_NOT_FOUND`) or the exception's class does (`status_code = 410` in its body, or
`super().__init__(status_code=404)`); `--exception E` (and `--status N`) asks about one exception raised
below the function instead. It walks the function's callers up to the configured apps' route handlers,
through FastAPI dependencies too: a function in `Depends(...)` or `Security(...)` in another
function's signature counts as called by it, and a route's own dependency callables as called by the
route. Each route and exception gets the handler the app registered (from the routes view, which
imports the apps: use it on code you trust): the one for the raise site's status code, else for the
nearest class above the exception, else a catch-all `Exception` handler, else none, which answers 500.
A handler in the repository answers with the literal `status_code` it responds with; FastAPI's own
answers with the exception's status (422 for a validation error); `?` when it cannot be read.
`--access` keeps one access class from the route manifest, `--depth` (default 6, at most 12) bounds the
walk, which follows at most 2,000 functions.

`codeterrain impact NAME` joins the code index to the fixture index: which tests run a name. It walks
out from the definition through its callers, three levels by default (`--depth`, at most 10). A call
inside a test function reaches that test, and the copies of a test method that pytest collects in
the test classes inheriting it, each with the marks pytest runs it with there (the method's own,
the collected class's and its bases', its enclosing classes' and its module's). A call inside a fixture reaches every test that gets that very
definition, directly or through other fixtures, the way pytest picks it for that test (the class's
own or a base class's, an enclosing class's, the module's, the nearest conftest's, then a plugin
module's when only one offers the name, since which of several is loaded is not read statically;
never one the test parametrizes directly), and every test an autouse fixture runs for. A call inside
a setup or teardown hook (`setup_method`, `setup_class`, `setUp` and the like on a class, and its
subclasses; `setup_module`, `setup_function`, `setUpModule` in a module) reaches the tests it runs
for, and naming a fixture or a hook as `NAME` lists its tests directly. A call inside any other
function, code or test helper, makes that function the next name to follow, and following a
constructor (`__init__`, or a dataclass's `__post_init__` where `__init__` is generated) follows the
calls of its class too, and of the subclasses that inherit it unchanged; naming a constructor as
`NAME` starts there. Each test is listed with its depth and the chain that reaches the name (`app.service.run
<- app.service.Service.place <- app.core.check`). A test that patches the name, in its body or with
a decorator on it or its class, is listed apart, since the real one does not run there. What a
static walk cannot follow is counted instead: callers deeper than `--depth`, calls that run at
import time (module level, a class body), the name used as a value (`Depends(...)`, a callback; a
test that reaches a route through an HTTP client is not seen) and calls by name only.

With the Terraform pack installed, `def` and `refs` also read `.tf` files, under Terraform's own
names: `aws_s3_bucket.logs`, `data.aws_ami.base`, `module.vpc`, `var.region`, `local.tags`,
`output.url`, and `provider.aws` (`provider.aws.east` for an alias). A module call's doc is its
`source` and `version`; `refs provider.aws.east` lists the resources and module calls that select
it. Each reference is shown with the block it sits in. See
[the pack's README](../packages/codeterrain-terraform/README.md).

`codeterrain callers`, run in the backend, joins the calls of the front ends listed in `client_repos` to the
routes view: with no argument it lists every call with the route it hits and a one-line summary
(`40 calls in 2 front ends: 34 hit a route, 2 hit none, 4 have no static path`); with `METHOD PATH`
only the calls of that route; `--unmatched` only calls with a static path that hit no route, which is
where a front end calls an endpoint the backend does not have. A single argument that is not an HTTP
method is taken for a Python name and answered by `codeterrain refs`, with a note saying so. Paths are compared segment by segment
with the query string dropped: a route parameter matches any segment (`{x:path}` the rest), a `{}` in a
call matches any text in its segment, the most literal match wins and then registration order. Each
front end's path gets its `path_prefix` first. Because the join reads working copies, `codeterrain callers`
prints a warning for the backend or any front end whose checkout is behind its upstream branch or the
remote's default branch (`origin/HEAD`, else `origin/main` or `origin/master`), as of its last fetch, or
has uncommitted changes, and `--json` reports `backend_state` and each client's `state`. It runs `git status` there with no optional locks, so nothing is written to the checkout, and
with `core.fsmonitor` switched off, so a command configured in that checkout's `.git/config` is not run.
A front-end file that could not be parsed is reported too (a warning with the count; `clients[].errors`
in `--json`), since its calls are missing from the join.

Run it from the repository root, or pass `--root PATH`. Every command takes:

| Flag | Meaning |
|---|---|
| `--root PATH` | the repository root (default: the current directory) |
| `--config FILE` | TOML file with a `[tool.codeterrain]` table (or the keys at top level) |
| `--fresh` | rebuild the views before answering |
| `--budget N` | cut text output at about N tokens (default 2000, 4 characters per token; 0 = no cut) |
| `--json` | the full data as JSON instead of a table |
| `--cache-dir DIR` | cache location (also `CODETERRAIN_CACHE_DIR`) |
| `--python EXE` | the target's interpreter for the import and pytest tier (also `CODETERRAIN_PYTHON`; default: codeterrain's own) |

A cut table ends with `+N more; narrow with <filter>`. Paths are repo-relative and POSIX.
