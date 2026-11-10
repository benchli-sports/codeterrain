# Changelog

## Unreleased

- `codeterrain classes NAME`: a class's bases, its method resolution order within the repository,
  and every subclass across modules with its depth, through imports, aliases and re-exports
  (#1).
- `codeterrain env NAME`: an environment variable end to end (#2). The Terraform pack reports where
  a variable is set (`env.NAME`, kind `env`: Lambda and App Service maps, ECS `environment` lists,
  Kubernetes `env` blocks), never its value: the `name` of a record names a variable, while in an
  assigned map of variables `name` is a key like any other, so its value is not kept. A settings
  prefix or an alias that is not a literal (a name, a `**spread`, an `AliasPath`) leaves its fields
  unmatched instead of reading as empty; each name of an `AliasChoices` is read, an alias inside
  `Annotated` counts, and configuration merges as pydantic does, a later base winning.
  pydantic-settings fields map to the variable through a
  literal `env_prefix` or alias, with each field's readers; direct `os.environ` and `os.getenv`
  reads, sets and tests are new facts of the code index (format `code-23`), through the names the
  file imports them under (`from os import getenv as read_env`) and with the name given by
  keyword (`key="NAME"`). A settings class also reads the
  fields of a base that is a plain model (`class Settings(Common, BaseSettings)`).
- `codeterrain raises NAME`: the exceptions a function raises, the routes that reach it through
  callers and FastAPI dependencies (`Depends`, `Security`, and each route's own dependency
  callables), and the status each route answers with, from the app's registered exception handlers
  (#3). The routes document gains an optional `exception_handlers` list, read in the sandbox with
  the routes; a view built before it says to run `terrain refresh routes`. When several functions
  share the name, each one's exceptions are listed on the routes that reach that one; and an app's
  handler for a class imported under a re-exported name (`fastapi.HTTPException`) is matched to
  that class before a handler of its base. In a nested project, where the app names a module by
  a tail of its path (`routes` for `apps/web/routes.py`), handlers, dependencies and handled
  classes are joined to the index's names. `--exception ValueError` names a built-in class as a
  raise of it is read. A dependency that is a callable instance is followed into its `__call__`,
  and a handler registered for a status code answers only for an `HTTPException`, except the one
  for 500, which takes what nothing else handles. Between two handled bases the method
  resolution order decides.
- A change of the code index's format now drops its `bases` table with the others. It was kept,
  so after an upgrade its rows could point at the file ids the rebuild reused.
- `codeterrain fixture NAME` names, for each dependent of a fixture with several definitions, the
  definition it gets (`gets FILE:LINE`, and `gets` in `--json`): a test under a nested conftest that
  overrides the fixture gets that override, not the root one the list used to sit under, so the
  conftest to edit for it is no longer a guess. Resolution is `impact`'s: class bases, module,
  conftest folders.
- The fixture view reads the `conftest.py` of each folder above a test path, up to the root, as
  pytest loads it. A root `conftest.py` beside `tests/` was not read, so its fixtures were missing
  and an override below it showed `overrides: -`.
- `codeterrain fixture NAME` lists the dependents `impact` already counted: every test an autouse
  definition runs for (`via autouse`; an autouse fixture used to show `dependents (0)`), and the test
  methods a class inherits from a helper module (`inherited from FILE:LINE`). Both commands now
  build the tests pytest collects in one place, so they cannot disagree.

## 0.3.2 (2026-11-02)

- The README's links to the docs, SECURITY.md and the CHANGELOG are absolute GitHub links. PyPI
  resolves a relative link against its own project page, so on the 0.3.1 page every one of them
  led nowhere.

## 0.3.1 (2026-11-02)

- The README's diagram of how it works is an image, as the charts are: PyPI does not render
  Mermaid, so the 0.3.0 page showed its source.
- `codeterrain` is the command the docs use; `terrain` stays installed as a shorter name. Another
  tool on PyPI installs a `terrain` command, and with both installed the last one wins.
- `codeterrain-terraform` 0.2.1 and `codeterrain-typescript` 0.1.1: their READMEs, which are their
  PyPI pages, install from PyPI instead of saying the packs are not published. No code change.
- The README leads with `uv tool install codeterrain` from PyPI, and has a starting point for an
  agent's instructions. Its claims are narrowed to what is measured: no "exact answers" headline;
  `impact` lists the tests that statically reach a name, not the tests that would catch a change;
  character counts measure an answer, not a session; the import tier's safeguards stop accidents,
  not hostile code, so it is for code you trust. The field figures cover all 160 logged lookups,
  with their period and how they were judged, two marked wrong among them.
- SECURITY.md and the rationale no longer describe the repository as private or a public release
  as future work. SECURITY.md's third guarantee says what it covers: pytest and its known plugins
  do not write into the repository by default; target code that opens a file by absolute path still
  can.
- A prior-art page, `docs/prior-art.md`: what twelve comparable tools document answering, read from
  their READMEs, with a chart. None documents pytest fixture resolution, front-end calls matched to
  routes, or routes read from the application. It is a reading of their docs, not a benchmark.

## 0.3.0 (2026-11-02)

- `terrain refs` following an alias counts a read after an assignment on the same line
  (`ALIAS = VALUE; print(ALIAS)`), and still not one inside the assignment's right-hand side.
  A binding takes effect where Python binds it, by line and column: a `for` target once
  its iterable is evaluated (`for A in [A]` reads the old `A`), a `:=` target after its value,
  and of two assignments on one line the later one is the last. The code index stores columns
  for calls, uses and definitions (format `code-22`, so the index is rebuilt once), and
  `refs --json` rows carry `col`.
- Version 0.3.0. `codeterrain[terraform]` needs the Terraform pack 0.2 (modules and providers), and
  both packs need codeterrain 0.3. The package carries PyPI classifiers and keywords. The docs are
  brought up to date with what 0.3.0 ships: the Terraform pack in the command reference, limits
  and README, signed static views in SECURITY.md and how-it-works, `refresh languages`.
- Warm answers are faster: one walk of the checkout now serves the code index and every language
  pack, a virtual environment is recognised from the folder listing already read, a signed view
  of the current format is not re-checked row by row, `impact` looks up a file's tests in one
  query, and a table cut to its budget no longer formats the rows it cannot show. On a large
  backend, `def` takes about 0.85 times and `impact` about 0.8 times as long as before.
- The static views (fixtures, front-end calls, language packs) are now written signed, so the
  skip above takes effect; a view written unsigned by an earlier build is checked once and signed
  again with its build time kept. `impact` builds an autouse fixture's reach once per name and
  checks a fixture chain once per test class, not once per test: about 0.93 times as long again.
- `terrain def --kind KIND` keeps only definitions of one kind, and without a name lists every one
  of them: `--kind class` for every class, `--kind module` for a Terraform checkout's module calls.
  An unknown kind lists the kinds the index holds.
- `codeterrain-terraform` 0.2.0: module calls carry their `source` and `version` as their doc;
  `required_providers` entries and `provider` blocks are definitions (`provider.aws`, and
  `provider.aws.east` for an alias); `provider = aws.east` and a module's `providers` map are
  references to them. `.tf.json` is left out: none of the Terraform checkouts surveyed has one.
- `terrain routes TEXT` that matches no route path or handler now lists the routes whose handler
  reads or calls a name containing the text (`routes legal` finds the handler that reads
  `LEGAL_PRIVACY_VERSION`), from the code index. A cut route listing's footer says `--budget 0`
  shows every row, so a listing piped into a filter is not mistaken for the whole.
- `codeterrain-typescript` 0.1.0, a language pack (`codeterrain[typescript]`) that reads the
  front-end files of `terrain calls` and `terrain callers` with tree-sitter in place of the built-in
  tokenizer. Names resolve through scopes: each function's own `const url` is read, and a parameter
  hides a module constant only inside its function. A file with a syntax error is reported with its
  lines and its calls there are unresolved. Across six front-end checkouts it finds the same calls
  and resolves as many as the tokenizer, with no wrong path, and a cold read takes about 0.6 to 0.7
  times the tokenizer's time (a tree-sitter query finds the scopes, bindings and call sites, so only
  those are visited in Python); warm reads are cached either way. tree-sitter loads on the first
  front-end file read, so discovering the pack costs other commands about 9 ms. The tokenizer stays
  as the reader when the pack is missing; why, in `docs/design-v0.2.md`.
  tree-sitter and both grammars are pinned exactly; the facts are cached per pack version.
- Language packs may offer `call_suffixes` and `read_calls(source, suffix)` for `calls` and
  `callers`, alongside or instead of `suffixes` and `read` for `def` and `refs`. The core checks every
  call a pack returns, and a pack that raises or returns anything malformed gives that file an error.
- `terrain importers MODULE` lists every import of a Python module or of a name from it, and
  `terrain imports MODULE` what a module imports, from the code index. `def` and `refs`
  on a module name point at `importers`. A star import (`from m import *`) is now kept in the
  index as an import of `m.*`; the index format moves to code-21, so indexes rebuild once.
- `terrain callers NAME` with a Python name instead of `METHOD PATH` answers with `terrain refs
  NAME`, with a note, instead of an error: the most frequent misuse in agents' feedback.
- The code index's cache of decoded definitions is bounded by count and by the stored size of
  what it holds: receivers with very large signatures no longer stay decoded for the
  whole query.
- `terrain impact` looks a fixture request's definition up by scope (class, module, conftest
  folder, plugin module) instead of scanning every definition of the name per request:
  many classes overriding and requesting one fixture no longer cost the square of their number.
- The code index counts the module path each definition and binding is stored under toward a
  file's byte budget: a small file under a very long path is reported as not indexed
  instead of multiplying the index size.
- `fixtures`, `fixture` and `doctor` name a top-level test tree outside `test_paths` (a directory
  holding a `conftest.py` or test modules) that the fixture view does not read: a
  same-named fixture from `tests` was otherwise taken for that tree's own. `--json` answers
  carry the list as `unlisted_test_paths`.
- The code index looks up a file's nested project root once per file, not once per binding it
  resolves: a file far below a project marker no longer pays for its depth on every row.
- `terrain refs` no longer lists, through a followed alias, the module code and class bodies
  of its file that run before the alias's assignment, while the variable still holds
  something else; reads inside functions still count.
- `terrain impact` gives an inherited test the marks pytest runs it with: the method's
  own, the collected class's and its bases' (pytest merges class marks along the MRO), its
  enclosing classes' and its own module's `pytestmark`, not the base module's. A subclass that
  parametrizes a fixture name no longer reaches the fixture through its copy; a usefixtures
  mark on an uncollected base's method (`class Contract`) now runs the fixture in each
  subclass; a usefixtures mark on a plain base class applies to a test class's own methods.
  The fixture view keeps each module's, class's and test method's own marks for this.
- `terrain impact` with a constructor as the target (`lib.Foo.__init__`) reaches the tests that
  call its class, and the subclasses that inherit that constructor; it found none before.
  Constructors follow `@dataclass`: a dataclass's generated `__init__` replaces an inherited one,
  and `__post_init__` is followed only where a generated `__init__` calls it. The index records
  which classes get a generated `__init__`; its format moves to code-20, so indexes rebuild once.
- Worktrees: when a repository has more than one checkout, every answer ends with a line
  on stderr naming the checkout it was built from (`answered from <root> (a worktree); the
  repository has 3 other checkouts: run terrain from the one you are editing`), the JSON envelope
  carries `root`, `terrain doctor` lists the other checkouts, and `--root` pointing at another
  checkout than the current directory's is flagged. The worktrees are read from git's own files,
  without running git. One cache directory shared by several worktrees is tested to keep each
  checkout's answers apart, and the docs now say so.
- `terrain refs` finds a name where it is read, not only where it is passed to a call: assigned,
  combined into another value, returned, compared, a member of a set, list, tuple or dict
  literal, a default, or read anywhere else (an `if` test, an operand, a loop's iterable). The
  summary says "used as a value" and each row says how. A module or class variable assigned straight from the name (`TABLE_NAME =
  settings.TABLE`, as its last assignment) is followed one level, up to 20, its sites marked `via TABLE_NAME`. A variable made by its
  class (`settings = Settings()`, or annotated `settings: Settings = ...`) is an instance of it, so
  `settings.save()` and `settings.TABLE` resolve to the class's members instead of by name only.
  The index format moves to code-19: indexes rebuild once.
- Docs: the README is now a front page with a quick start, a table of the questions each command
  answers, the results as charts (adding warm `def`, `refs` and `impact` times, `refs` accuracy
  against grep, and the outcomes of lookups logged by coding agents at work), an inferred token-saving
  estimate with its method, and a diagram of the two tiers. The reference sections moved unchanged to
  `docs/commands.md`, `docs/configuration.md`, `docs/how-it-works.md`, `docs/sandbox.md` and
  `docs/limits.md`. AGENTS.md and SECURITY.md now point at `docs/sandbox.md` for the guard's gaps.
- Output that was cut no longer reads as complete through a pipe: when a cut text answer goes
  to a pipe, its `+N more` line is repeated on stderr (`terrain routes | grep x` dropped it), and
  a cut route listing counts what it left out per app (`+581 more: api.main:app 390, ...`).
- Field-log fixes: `terrain callers NAME` with a Python name says to run `terrain refs NAME`;
  a route substring or path that Git Bash rewrote (`/me` arrives as `C:/Program Files/Git/me`)
  is named when nothing matches, with `MSYS_NO_PATHCONV=1` or `//me` as the fix; an app that
  fails to import with `ModuleNotFoundError` under codeterrain's own interpreter says to pass
  `--python` or set `CODETERRAIN_PYTHON`.
- Code index: a `.py` path that is not valid UTF-8 (a POSIX file name byte) is reported as an
  unreadable file instead of stopping the build, and a file's parse error is kept to 500
  characters, so many symlink aliases of one file cannot each store a large copy. The index
  format moves to code-18: indexes rebuild once.
- `codeterrain-terraform`: a directive nested inside a template `for`'s collection no longer
  starts the loop's binding early. Installing from a checkout with the pack is
  `uv tool install "<checkout>[terraform]"`.
- `codeterrain-terraform` 0.1.0, the first language pack (`pip install "codeterrain[terraform]"`),
  kept in this repository under `packages/` as a uv workspace member: `terrain def` and `terrain refs`
  for `.tf` files. Definitions are named the way Terraform refers to them (`aws_s3_bucket.logs`,
  `data.aws_ami.base`, `module.vpc`, `var.region`, `local.tags`, `output.url`); references are found
  in attribute values and inside string and heredoc templates, with the block they sit in, skipping
  comments, literal text, escapes, built-in objects (`each`, `count`, `self`, `path`, `terraform`) and
  `dynamic` block iterators. A tokenizer, not an evaluator: no module source is followed and no
  `count`, `for_each` or `.tfvars` is expanded. No dependency besides `codeterrain`.
- Language packs: an installed distribution that registers an entry point in the
  `codeterrain.languages` group adds another language's definitions and references to `terrain def`
  and `terrain refs`. A pack supplies the file suffixes it reads and a pure `read(text)` function; the
  core walks the repository, caches the facts of each file by content (per pack version), checks every
  field a pack returns and turns a pack's failure on one file into a file error. A name Python defines
  is still answered from the Python index; `refs` uses a pack for a name only a pack defines.
  `terrain doctor` lists the installed packs and any refused, and `terrain refresh languages`
  rebuilds them. Security: a pack runs in terrain's own process, so before any pack is looked up
  every import-path entry inside the repository being read (and an empty or relative one) is
  removed, and a pack whose distribution or module still lies inside it is refused before it is
  imported. Only what terrain itself runs from inside the checkout (its virtual environment, the
  folder of its own code) is exempt; a checkout inside terrain's environment is not.
- `terrain impact NAME [--depth N]`: the tests that reach a Python name, joining the code index to the
  fixture index. It walks out through the name's callers (three levels by default) to test functions,
  and through fixtures to every test that requests them, directly or through other fixtures, resolved
  for each test the way pytest does (class, base class, module, conftest and plugin fixtures,
  overrides, direct parametrization); autouse fixtures, setup and teardown hooks (xunit style and
  unittest) and test methods inherited from a base class are followed too; each test comes with the
  chain that reaches the name. Tests that patch the name are listed apart, and what a
  static walk cannot follow (module-level calls, the name passed as a value, calls by name only,
  callers past the depth) is counted. Bounded: 2,000 callers, 2,000 fixtures and the rows `refs` reads.
  `references` takes a shared row budget for it. The code index records each class's bases (index
  format bumped: the first query after upgrading rebuilds it).
- Code index: an import written in a folder with its own `pyproject.toml`, `setup.py` or `setup.cfg`
  (an app in a monorepo) that the index does not know is looked up in that folder and its `src/`, so
  `from src.repositories import factory` in `apps/api` resolves to
  `apps/api/src/repositories/factory.py`, through re-exports and string patch targets too. Module
  names still start at the repository root, so two apps' modules never collide and a path from the
  root still resolves. Adding or removing such a folder needs no rebuild.
- `terrain def NAME` and `terrain refs NAME [--tests|--code] [--by-name]`: a code index of every Python
  file, application code and tests, read with `ast` only. `refs` lists the calls that resolve to the
  definition (through imports, package re-exports, `self`/`cls` and a constructor receiver) with each
  call's shape and what does not fit the signature, the places the name is passed as a value, and the
  places it is patched (`mock.patch`, `patch.object`, `monkeypatch.setattr`). Calls of the same
  attribute on a receiver of unknown type are counted, and listed with `--by-name`. A call through an
  import under another name (`import create as renamed`, also through a renaming re-export) counts; a
  parameter or local that hides the imported name does not. Folders that packages were installed into
  (a `*.dist-info`, or an `*.egg-info` that records installed files or has no source package beside
  it) are not indexed; an editable install's egg-info keeps `src/`, its package found by the
  distribution's name or its `top_level.txt` (the two need not match). That metadata is read only
  from a regular file reached through no link, so a `top_level.txt` linked out of the checkout or to
  a device is never opened. Changing `test_paths`
  reclassifies the indexed files.
  An import inside a function binds only in that function and the functions nested in it. The
  signature check keeps positional-only parameters (`def f(a, /, **kw)`) and matches arguments to
  parameters one by one, so `f(1, a=2)` reports both the repeated `a` and the missing `b`. A
  parameter of a nested function, lambda or comprehension no longer hides an imported name in the
  enclosing function. A member of a re-exported module (`from . import impl as api`, then
  `api.Service.create()`) resolves, and a patch of a renamed export counts as a patch site. A
  comprehension target or a `match` capture hides an imported name of the same name, and a lambda's
  defaults are read outside its parameters. Parameter and return annotations are read, so
  `Annotated[str, Depends(get_user)]` counts as a use of `get_user`, and a method called on a
  module-level variable (`svc = make(); svc.save()`) is a by-name candidate, as inside a function.
  A name defined twice in a file means its last definition, so a function's implementation wins
  over its `@overload` stubs. An import in an inner function wins over an outer function's parameter
  of the same name, and a walrus target inside a comprehension binds in the enclosing function. At
  module level the later of an import and a definition of the same name wins, as at run time, and
  re-exports follow it. A file is parsed from its bytes, so a UTF-8 BOM or a coding line is honoured.
  In a class body a call of a name the class defines means that definition (its methods still see
  the module's names), and `patch.object(lib, ...)` through a parameter or local named `lib` is not
  a patch of the imported module. A name declared `global` means the module's binding, whatever the
  enclosing functions bind, and `self.save()` of a method the class does not define (an inherited
  one) is a by-name candidate rather than a call to a name that does not exist. A value passed under
  a `global` declaration resolves the same way, and a comprehension runs in its own scope (shown as
  `C.<listcomp>`), which skips a class body's names as Python does; so does a lambda's body
  (`C.<lambda>`). A class body's own definition counts only after it is defined (the body runs top to
  bottom), and `patch.object` on a receiver declared `global` resolves through the module. In a class
  body the latest binding before the call wins, a definition or an import, and `Child().save()` of a
  method only a base class defines is a by-name candidate, as `self.save()` already was.
  A module-level assignment that unpacks (`a, (b, *c) = ...`) binds every name in it, so it
  replaces an earlier import of the same name.
  A function that both imports a name and assigns it, or imports it from two places, leaves the
  name unresolved, since which binding ran last is not known statically.
  Code at module level, and in a top-level class body, means the binding of its own moment, so
  `create = create(1)` calls the import and `create = cache(create)` passes it; a module-level
  `for` or `with ... as` target binds like an assignment.
  A module-level call of the file's own function before a later import of the name means that
  function, and `cls.save(instance, ...)` in a classmethod passes the instance explicitly. A
  module-level `patch.object` resolves its receiver at the patch's own line, and a module-level list,
  set or dict comprehension runs at its own moment (a generator expression later).
  A module-level walrus (`if (create := ...):`) binds like an assignment, and a call through
  another module's variable (`state.svc.save()`) is a by-name candidate.
  A patch whose target is passed by keyword (`patch(target="lib.create")`,
  `patch.object(target=lib, attribute="create")`, `monkeypatch.setattr(lib, name="create", ...)`)
  counts, and so does a renamed import of `patch` (`from unittest.mock import patch as mock_patch`,
  then `mock_patch(...)` or `mock_patch.object(...)`) where it is the import that binds the name
  there: a parameter, a local or another scope's import of that name hides it, and a class body's
  import counts only in that body. The same holds for `patch` under its own name: a parameter
  named `patch`, or `patch` imported from another module, is not a patch. `Child.build()` of a member only a base class defines is a
  by-name candidate, as `self.build()` and `Child().build()` already were.
- The index is a SQLite database in a folder of its own in the cache directory (SQLite creates its
  journal there), created `0600` on POSIX before anything is
  written to it (regression test), and updated only for files whose content changed. A row it does
  not recognise stops the query with a message to run `terrain refresh code` (regression test); an
  index of another format is rebuilt. SQLite follows links and a rebuild drops tables, so the index is
  not opened when it or its folder is a link or it has a hard link (on Windows too), or, on POSIX,
  when its folder is not the user's alone or it or a folder above it
  belongs to another user or is writable by others without the sticky bit, checked again after the
  index file is created so that a link winning the race to the name is refused (regression tests). A file
  is hashed in chunks and read only up to the 2 MB cap, so a huge file in the checkout costs neither
  memory nor a parse, and collecting a function's local names visits each node once, so deeply
  nested lambdas cost linear work (regression tests). `refs` decodes the matching rows one at a time
  and keeps at most 10,000 sites of each kind, counting the rest, so a name called hundreds of
  thousands of times does not exhaust memory; `--json` carries `counts` and `truncated`
  (regression test). A bare or qualified name lists at most 100 definitions, and at most five million
  bytes of them; when more match, `def`
  and `refs` say how many and ask for a qualified name (regression test). Whether a value use sits on
  a patched line is looked up in the index for that line, so no set of every patch site is kept,
  and a function-scoped import is found with one lookup per enclosing scope, so neither costs work
  proportional to the product of two counts (regression tests). Renamed imports of a queried name
  are collected up to 1,000 and every lookup reads them in batches, so a checkout with a huge number
  of aliases costs a bounded number of queries (regression test). One query resolves at most 100,000
  candidate rows. The database's identity is checked again right after SQLite opens it, and on
  Windows the index goes in the per-user cache under the profile when the cache directory is outside
  it (regression tests). A file of more than 600,000 tokens, or one yielding more than 50,000 facts
  of any kind (definitions, imports, calls, values passed, patches), is reported and not indexed, so
  a dense file cannot make the parse take memory out of proportion to its size; facts count before
  they are kept, so no statement shape (a chain of assignments, a call of many arguments) gets past
  the limit, and reading a signature or checking a call against it stays linear in the number of
  parameters. Resolving a name reads only that name's bindings in the enclosing scopes, through
  an index, into a bounded cache, so `refs` keeps no file's other bindings, and a function's
  parameters count toward the per-file fact limit. `refs` builds each signature's shape once a
  query and checks a call in time linear in the call, and a diagnostic names at most ten names
  before saying how many more; the shapes one query keeps share a budget of parameters, and a
  line's patches are read once a query and only for the queried name. A kept shape is never
  dropped, and a query builds at most two million parameters of shapes in all; past that, calls
  are listed without a shape check and `refs` says how many. The strings a file's facts repeat
  (scope, class, qualified name) count toward a per-file limit of ten million bytes, and the sites
  one `refs` query keeps share a limit of ten million bytes of text; past it they are counted, not
  kept (regression tests).

## 0.2.0 (2026-10-27)

- Warm answers: the cache's file walk keeps paths as strings (about five times faster on a large
  backend), the slow-tier views are signed with the per-user key and a signed view is not checked
  against the protocol again on every read (a view whose signature does not hold is checked as
  before, and a per-user key that cannot be read or created leaves views unsigned, so a read-only
  user cache dir still works; regression tests), and the callers join splits each route path once per join and each call path once.
  Security: no split is kept after the join, so a front-end checkout
  cannot fill a process-wide cache with call paths (regression test). Warm `routes`,
  `setup-order` and `callers` take about 35 to 40% less time, with the same output.
- The version is part of every view's key, so 0.2.0 rebuilds each cached view once. The setup-order
  view costs one `pytest --collect-only` of the suite.

- `terrain calls`: HTTP calls in JavaScript and TypeScript (client verbs with type arguments, `fetch`,
  object literals with `url:`), from a tokenizer written in Python that never runs the target. Paths
  from strings, templates, `+` and same-file constants; the rest listed as unresolved. Test, story and
  declaration files are skipped. Cached per file by content hash like the fixture index.
- Config: `client_paths` (confined to the repository like `test_paths`) and `path_prefix`.
- Config: a repository-root `codeterrain.toml`, read before `pyproject.toml`, for front ends without
  one. User-only keys in it are ignored, and a `codeterrain.toml` that links out of the root is refused.
- `terrain callers [METHOD PATH] [--unmatched]`: the calls of the front ends in `client_repos` joined to
  the routes view, segment by segment, most literal match first. `client_repos` is user only (it reads
  other checkouts); an entry can be a table that sets that front end's `path_prefix` and `client_paths`,
  so a read-only checkout needs no config file. A missing or misconfigured front end is reported and
  skipped.
- Calls view: minified files, msw `mocks/` and build output (`out/`, `coverage/`) are skipped, and an
  absolute URL through a client is not a call.
- Calls view: a constant declared more than once with different values is left
  unresolved instead of taking the last value; type arguments may hold object and array types
  (`get<{id: number}>(...)`); `fetch(path, {...})` without a `method` is GET unless the options spread
  another object.
- Calls view: only `const` bindings are resolved (a `let` or `var` can be
  reassigned before the request); a field an options object spreads over after it is unknown; the
  shorthand `{ url, method }` is a call site.
- Calls view: an object call (`axios({ ...opts, url })`) whose spread could supply
  the method has an unknown method instead of GET.
- Routes: a GET record carries `also_head` when the route answers HEAD too (a Starlette `Route`,
  not a FastAPI `APIRoute`), an optional field of `codeterrain.routes/1`; the callers join matches a
  HEAD call to such a route.
- Calls view: type arguments may hold parentheses and function types
  (`get<(User | null)[]>(...)`, `{ done: () => void }`); the view records the facts format and is
  rebuilt when it changes, so a scanner fix reaches unchanged files. An apostrophe inside a word
  (JSX text such as `Don't`) no longer opens a string that hides the rest of the line. A `const`
  with a type annotation (`const BASE: string = '/v1'`) is resolved; before, `${BASE}/items` read as
  `/items`. Calls inside a template's `${...}` are found (the template's text is still not
  scanned). A `const` with a trailing type assertion (`'/v1' as const`, `satisfies string`) is
  resolved (one with more expression after the type is left unresolved), and so is every
  declarator of `const A = '/v1', B = 5`. A name bound to a string the scanner cannot
  read (a `let`, a conflicting redeclaration, `cond ? '/a' : '/b'`) leaves a path built on it
  unresolved; before, it was dropped like a base URL and the shortened path read as resolved.
  An initializer that continues on the next line (a line starting with `.`, `(`, `+` and the like,
  or a line ending in an operator) is read whole.
  Escapes in strings and templates are decoded (`\u0069`, `\x69`, `\u{69}`, `\n`), not just
  stripped of their backslash. A regex literal inside a template's `${...}` (`/}/`) no longer
  closes it early. Constants are resolved after every declaration is read, so one may use a
  constant declared further down (a cycle stays unresolved). A computed key in request options
  counts as its string (`['method']: 'POST'`), and one the scanner cannot read (`[key]: v`) hides
  the fields before it, like a spread. A `let` or `var` with the same name as a constant hides
  the constant whatever its value, and calls inside a template's `${...}` keep their source line.
  A type assertion followed by more
  expression of any kind (`'/v1' as string ? '/a' : '/b'`) leaves the value unresolved.
  The calls facts format is `calls-16`, so facts cached by an earlier build are re-read.
- Callers: a front-end file that could not be parsed is reported (a warning naming the count, and
  `clients[].errors` in `--json`) instead of leaving a partial join looking whole.
- Callers join: a call never hits a WebSocket route, even when its method is
  unknown; an empty segment inside a call path (`/v1//items`) is kept, so it no longer matches
  `/v1/items`, and an empty segment does not fill a route parameter (`/files//download` is
  not `/files/{name}/download`). A call segment with `{}` and a route segment with a parameter inside it match
  when some text fits both (`report-{}.json` and `{name}.json`).
- Calls view: a constant whose name the file also binds as a parameter
  (`function load(url)`, `url => ...`, `catch (url)`), a destructured name or a declaration without a
  value is left unresolved: scopes are not tracked, so the outer value could be the wrong path.
- Security: wildcards inside a path segment (`{}` in a call, `{name}.json` in
  a route) are matched without regular expressions, in time linear in the segment, and paths of more
  than 2048 characters match nothing; before, a call with many `${...}` in one segment could hold
  `terrain callers` in regex backtracking. A string expression of more than 64 pieces or 2048
  characters is not read as a path, so `const b = a + a` chains no longer double until memory runs
  out.
- Security: reading one front-end file's calls is bounded by its size
  (`WORK_PER_TOKEN` steps per token); a file of thousands of calls nested in one another is
  reported as a scan error instead of holding the CLI for seconds, and an object call no longer
  copies the rest of the file. Every lookahead and lookbehind counts against the bound too: type
  arguments after a `<`, a receiver chain, a type annotation, a declaration's value.
- Security: matching a call against a route costs at most segments of the
  call times segments of the route, and paths of more than 256 segments match nothing; before, a
  route of many `{x:path}` parameters tried every split and could hold `terrain callers` for
  minutes.
- Security: `terrain callers` refuses a program on `PATH` inside any
  checkout it reads, the backend and every `client_repos` entry, whichever checkout it is asking
  about. Before, a `git` planted in one front end could run for another checkout's `git status`.
- Callers join: a route registered for any method (a class-based endpoint) matches
  calls of every method, and a parameter inside a segment (`/reports/{name}.json`) matches the text
  there instead of being compared literally. The segments a route requires after a `{rest:path}`
  parameter must match too.
- Callers: the backend's and every front end's `git status` run side by side while the calls
  views load, and a route without a `{x:path}` parameter is skipped at once for a call with a
  different number of segments; a warm `terrain callers` takes about a third less time, with the
  same output.
- Docs: charts of the measured gains (characters an agent reads, accuracy, warm speed) in light and
  dark, drawn by `docs/charts/make_charts.py` from ratios only.
- Docs: the speed of the repositories beyond the backend, measured again on an idle machine (warm
  answers 0.22 to 0.31 s); the earlier figures were taken under heavy load.
- `terrain callers` warns when the backend or a front-end checkout is behind its upstream or the
  remote's default branch (as of its last fetch) or has uncommitted changes, since the join reads
  working copies. A feature branch level with its own upstream is still flagged when the default branch
  has moved on. A stale front-end checkout had
  produced a false gap. Security: that `git status` runs with `--no-optional-locks` and
  `core.fsmonitor=false`, so it writes nothing to the checkout and does not run a command configured in
  the checkout's own `.git/config` (regression test).

- Known limits: the calls scanner is a tokenizer, not a parser. It resolves same-file `const`
  strings only, tracks no scopes (a name bound twice in a file leaves its paths unresolved) and does
  not model the whole grammar, so unusual syntax can still mislead it; the README lists the cases.
  A planned TypeScript pack replaces it with tree-sitter grammars.

## 0.1.0 (not released on its own; part of 0.2.0)

- Config: `--config`, `[tool.codeterrain]`, defaults.
- Cache: content-hash keyed, memoised on (mtime_ns, size), one per repository across worktrees.
- Static fixture index: `terrain fixtures`, `terrain fixture`, `terrain doctor`, `terrain refresh`.
- Setup order (`terrain setup-order`) from a sandboxed `pytest --collect-only` with a dump plugin.
- Routes (`terrain routes`, `terrain route`): FastAPI plugin, nested routers and mounts, manifest join for the access class.
- Versioned extractor protocol (`codeterrain.order/1`, `codeterrain.routes/1`) in docs/extractor-protocol.md.
- Sandbox for every import or pytest subprocess: dummy AWS credentials, loopback-only sockets.

### Fixes before release

- Sandbox environment is an allowlist, not the parent's environment; new `pass_env` (opt-in names),
  proxies and `PYTEST_ADDOPTS`/`PYTEST_PLUGINS` always dropped, `NO_PROXY=*`.
- Network guard covers connect, `connect_ex`, `sendto`, `sendmsg`, name lookups and `create_connection`,
  AF_UNIX, a guarded `_socket.socket`, and the Windows selector loop; every extractor starts through a
  bootstrap that proves the guard is active (exit 3, one line) before running.
- Never writes into the target: outside git the cache is per-user; extraction runs with `-o addopts=`
  (`respect_addopts` opts back in and refuses report options); git subprocesses run without `GIT_DIR` and
  friends.
- Cache: format and version stamps on every file; slow views carry a key (config, interpreter, versions)
  and a mismatch rebuilds; `pytest.ini`, `setup.cfg`, `tox.ini` join the staleness fingerprint; malformed
  or old-shape files rebuild instead of raising.
- FastAPI routes: websockets and sub-apps mounted inside included routers (0.137+), plain Starlette
  routes, class endpoints with their class location, `list[Item]` / `Optional[Item]` response models.
- Protocol: types, ranges and records validated on read. Manifest: root shape, exact `apps` matching
  (an empty list matches no app), first match wins.
- CLI: collection errors named when a node is not found, `refresh` continues past a failing view and
  exits 1, a parse-error warning, one-line errors, `indirect=[...]` handled per name, doctor hints.
- CI: actions pinned by commit SHA, a Windows job.
- The Windows asyncio proactor is guarded at `IocpProactor.connect` and `sendto`, whatever loop a target
  selects; the bootstrap checks it. `socket.SocketType` and `_socket.SocketType` are the guarded class.
- Never writes, part two: `-o log_file=` on every run; `respect_addopts` also refuses `--log-file`,
  `--debug` (before pytest opens its file), `--report-log`, `--json-report` and `--alluredir`. The README
  lists every pytest file writer and how each is handled.
- View keys cover the values of `pass_env` variables (hashed, never stored) and no longer include fields
  the extractors do not read (`root`, `source`, `route_manifest`, `manifest_keys`). The interpreter probe
  runs once per interpreter file and reports the installed distributions too.
- The Windows environment allowlist adds `USERNAME`, `PROGRAMDATA`, `PROGRAMFILES`, `PROGRAMFILES(X86)`.
- Cached per-file facts and views are type-checked, not only key-checked. The cache directory is taken
  from git only when the repository it reports contains the root. The manifest loader refuses `apps` that
  is not a string or a list of strings and `access` that is not a string.
- `refresh` continues past every codeterrain error type.
- The interpreter probe is cached per interpreter file, and the installed distributions of its site
  directories are listed again on every query, so an install behind a launcher or shim is noticed.
- Setup order resolves a fixture override across the whole chain: a fixture that requests its own name
  gets the next definition up, as pytest does, however deep the chain is.
- A `parametrize` mark on a class, or in `pytestmark` at class or module level (a single mark or a
  list or tuple), supplies its argument to the tests below it. A fixture of that name is no longer
  listed as a dependency of those tests, unless the mark is `indirect`.
- A `parametrize` mark that passes `argnames` as a keyword (`argnames="x"`, or a list or tuple) is read
  like the positional form, including with `indirect`, on tests, classes and `pytestmark`. Before, such a
  test was listed as a dependent of a same-named fixture.
- A positional `indirect` argument (`parametrize("x", [2], True)`, or a third positional list of names)
  sends the name through the fixture, as pytest does; such a test is listed as a dependent again.
- Each repository field in plaintext output has its newlines and tabs escaped too (`field_safe`), so a
  newline in a fixture name or route path can no longer start a forged row.
- The extractors run on a Python 3.9 target: two 3.10+ calls in the child modules (`isinstance` with
  `X | Y`, `write_text(newline=...)`) failed every extraction there; a test keeps the child modules
  3.9-safe.
- A bare program name whose `PATH` match lies inside the target repository (an absolute entry such as
  `<checkout>/bin`) is refused for `git` and the interpreter.
- Config and failure messages are printed as one escaped field: an unknown TOML key with an escaped
  newline forged a stderr line.
- The FastAPI plugin no longer reads `types.UnionType` on a Python 3.9 target, where the error dropped
  every route of an app with a `list[Item]` response model.
- A name that a `usefixtures` mark lists but direct parametrization supplies is no longer a fixture
  dependency, from the test's own marks or inherited class and module marks: pytest passes the
  parameter and never runs the fixture.
- `setup-order` shows the scope pytest runs an indirectly parametrized fixture at: with
  `parametrize(..., indirect=True, scope="module")` a function-scoped fixture runs once per module, but
  the document said `function` (and `--setup-plan` prints the declared scope too). The README notes the
  difference.
- Every string in an extractor's document, keys and optional or plugin fields included, is redacted
  before it is checked and cached; a plugin field carried a `pass_env` value into `--json` and the
  cache.
- The POSIX process check in the sandbox tests tolerates a process reaped mid-check; it failed a Linux
  CI job.
- Tests and fixtures inside nested test classes (`TestOuter::TestInner`) are in the static index, with
  the outer classes' marks inherited; they were skipped, so `terrain fixture` missed their
  dependents.

### Security reviews

- Plain text output escapes terminal controls: C0 controls other than tab and newline, DEL, C1 controls
  and the Unicode bidi overrides and isolates are shown as `\xNN` or `\uNNNN` on stdout and stderr. A
  fixture name, path or error message from the scanned repository could otherwise clear the screen or
  forge rows. `--json` was already safe.
- New `SECURITY.md`: the trust model (running the import tier runs the repository's code, as its tests
  would; the parent never does), what is guaranteed and what is best-effort, and how to report.
- Repository config is not the user's opt-in: `pass_env`, `pytest_args` and `respect_addopts` are
  honoured only from `--config` or the new user config (`CODETERRAIN_USER_CONFIG`, else
  `%APPDATA%\codeterrain\config.toml` or `$XDG_CONFIG_HOME/codeterrain/config.toml`), and ignored with a
  warning when the repository's `pyproject.toml` sets them. `doctor` shows the user config.
- Options that make pytest write files are refused from `pytest_args` too, and an ini `log_file` set by a
  later `-o` is refused, in both `respect_addopts` modes.
- Every program codeterrain starts is resolved to an absolute path from the absolute `PATH` entries, so
  a `git.exe` or `python3.exe` in the repository is never run (on Windows the current directory was
  searched first).
- A timeout kills the whole process tree and returns: child output goes to files, not pipes, and the tree
  is killed with `taskkill /T /F` (absolute path) on Windows and `killpg` on POSIX. The run directory is a
  private temp directory, never under the cache; output is read back as a capped tail and the result
  document is capped at 256 MB.
- The child gets empty home and per-user data directories (`HOME`, `USERPROFILE`, `APPDATA`,
  `LOCALAPPDATA`, `XDG_*`), so credential stores under the user's home are not visible.
- `import_env` and `pass_env` values (6 characters or more) and the dummy credential are redacted from
  child errors, tracebacks and output before they are cached or printed.
- A file that exhausts the parser (`RecursionError`, `MemoryError`) is a recorded parse error, and a deep
  JSON document from the child or the cache is a clean error.
- `test_paths` and `route_manifest` must resolve inside the repository root; the walk does not follow
  file symlinks out of it.
- POSIX cache directories `0700` and files `0600` whatever the umask, temp files opened exclusively and
  never through a symlink; `pass_env` digests are an HMAC under a random per-cache key.
- `uv.lock` is committed and CI installs from it; `actions/checkout` no longer keeps credentials.
- README: "no network" is scoped to codeterrain's own process, and the guard's gaps are listed in full
  (non-Python subprocesses, isolated-mode children, a reloaded `_socket`, undoing the patch). The doctor
  hint says the `.venv` interpreter belongs to the target repository.
- The sandboxed child's git reads no system or global config and has an empty credential helper
  (`GIT_CONFIG_NOSYSTEM`, `GIT_CONFIG_GLOBAL`, `GIT_CONFIG_COUNT`), so Git Credential Manager, which Git
  for Windows sets in the system config, no longer hands it your token.
- On Windows only `.exe` and `.com` files are run: a `git.cmd` first on `PATH` made `cmd.exe` read `&` in
  the checkout's directory name as commands, in the parent, and a `python.bat` shim broke the import
  tier. An explicit `.bat` or `.cmd` is refused. A driveless `PATH` entry (`\tools`) is skipped.
- On Windows the child runs in a job object, so what it leaves behind is killed after a normal exit as
  well as on timeout; the run directory, which holds the raw child output, is removed with retries, and
  a failed kill or removal is a warning.
- The redactor also replaces a long value's leading and trailing fragments (12 characters or more),
  which pydantic prints around `...`, and it now covers the order view (node ids, fixture fields) and
  every route field.
- The default `tests` path is confined to the root like a configured one: a `tests` junction or a
  git-cloned symlink was walked outside the checkout. The scan and the fingerprint hash refuse a start
  or a file that leads out of the root on their own as well.
- The `pass_env` digest key lives in the per-user cache directory, not beside the views, so a copied
  `.git` does not carry it.
- Output escapes the bidi marks (LRM, RLM, ALM), the Unicode line and paragraph separators and lone
  surrogates, and stdout and stderr write with `backslashreplace`: an undecodable file name crashed
  every run.
- A repository `pyproject.toml` whose `[tool]` is not a table, or that is not UTF-8, is a config error
  (exit 2), not a traceback.
- README and SECURITY.md: the home redirect hides credential files from SDK discovery but not from code
  that reads the real home or an OS credential store; a user-config `pass_env` applies to every
  repository; the child can work out the cache location; on Windows an in-checkout cache inherits the
  checkout's permissions; the writer refusal is a list of known options.
- The parser-exhaustion tests use 100,000 terms: 5,000 parsed on Linux, whose C recursion limit is
  higher, and CI failed there.

### Documentation

- README: a "Why this exists" section, every configuration key with its default, the exact `--config`
  precedence, the `--root`, `--fresh` and `--version` options, and a Development section.
- New `docs/why-codeterrain.md`: the problem, the proof, how it was tried on other projects, why we did
  not adopt a knowledge-graph tool, and what is not proven yet.
- `docs/why-codeterrain.md` rewritten around the pain, the four gains and how each is measured, the
  target repositories by shape with their v0.1 status, and a per-repository results table for the
  Python repositories beyond the backend. It says
  where the token gain is small and that the speed figures from those runs were taken under load.
- README: "Why this exists" covers the goal, the target repositories and the measured accuracy, speed
  and token figures.

### Known gaps (deliberately not in v0.1)

- Prune old content hashes from the static object store (it only grows with edits).
- On POSIX a grandchild that starts its own session can survive a run. On Windows a process escapes only
  when the job object cannot be created (a warning says so).
- The home redirect hides credential files from SDK discovery only: hostile code can read the real home
  by absolute path or ask an OS credential store.
- A user-config `pass_env` applies to every repository; scoping it per repository in the user config is
  not built (use `--config`).
- The child can work out where the cache is and rewrite it.
- Redaction catches exact values and long leading or trailing fragments, not other transformations
  (encoding, case changes) of a secret.
- The target sees an empty home, so user site-packages and config under the real home need user-level
  `pass_env`.
- The network guard does not cover non-Python subprocesses, isolated-mode Python children, a reloaded
  `_socket` or target code that undoes the patch (README).
- Budget and footer literal pins (token-budget boundary tests) are not exhaustive.
- Memo pruning is not tested against directory junctions on non-Windows platforms.
- Two applications defined in one module are not told apart by the manifest join.
- Editable sibling packages and the user site-packages are not part of the interpreter key; after
  changing them run `terrain refresh`.
- The original C socket class (`socket.socket.__mro__[1]`) is immutable and stays reachable to code
  that deliberately goes round the guard; it is a best-effort guard (README).
- Python's `getfqdn` swallows the guard error from its reverse lookup and returns the name.
