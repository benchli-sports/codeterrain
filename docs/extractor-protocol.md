# Extractor protocol

codeterrain has two halves:

- the **core** (`codeterrain.cli`, cache, budget, config, fixtures_static, protocol, views): reads JSON,
  caches it, answers questions;
- **extractors**: small programs that run inside the target project's own runtime (its interpreter,
  its installed packages), look at the live objects, and write one JSON document.

The core never imports extractor code in-process. It starts the extractor as a subprocess and reads
the JSON file it leaves behind. That is the whole interface, so a TypeScript or Node extractor, or a
core written in Rust or Go, can replace either half without touching the other.

## How an extractor is run

The core runs the extractor with:

| Environment variable | Meaning |
|---|---|
| `CODETERRAIN_RESULT` | absolute path the extractor must write its JSON document to (UTF-8, LF) |
| `CODETERRAIN_ROOT` | absolute, symlink-resolved path of the repository root; also the working directory |

The extractor's stdout and stderr are not parsed; they are only shown when no document appears.
Exit status is not the verdict: a document at `CODETERRAIN_RESULT` is what counts (pytest, for
example, exits non-zero when some test modules fail to collect, and the document records that).

In the Python reference implementation every extractor subprocess runs in the sandbox
(`codeterrain.sandbox`): an allowlisted environment, dummy AWS credentials, no credential files, EC2
metadata off, proxies removed, and a best-effort loopback-only socket guard that a bootstrap proves
active before the extractor starts. Another runtime's launcher should apply the equivalent guarantees.

## Rules for every document

- A top-level `"schema"` string `codeterrain.<view>/<major>`. The core reads exactly the versions it
  knows and refuses anything else with
  `unsupported extractor schema '<found>': this core reads '<expected>'`; it never guesses.
- Adding an optional field is not a new version. Removing or renaming a field, or changing its meaning, is.
- Paths are repo-relative, POSIX (`/` separators), with symlinks and junctions resolved. A path outside
  the repository (a library, the framework) is absolute POSIX.
- Line numbers are 1-based and point at the first decorator line of a decorated definition (what
  `inspect` and pytest report).
- The core checks the types of every field it reads (strings are strings, integers are integers and not
  booleans, lists are lists, closure indices are in range) and refuses a document that fails with a
  `ProtocolError`, so a malformed extractor can never reach a query.
- A cached document whose schema the core no longer reads is discarded and rebuilt.

## `codeterrain.order/1` (view `order`: fixture setup order)

Produced by `codeterrain._pytest_dump_plugin`, loaded into
`python -m pytest --collect-only -q -p codeterrain._pytest_dump_plugin`.

```json
{
  "schema": "codeterrain.order/1",
  "generator": {"name": "codeterrain-pytest-dump", "pytest": "9.1.1"},
  "closures": [
    [
      {"name": "_env_session", "scope": "session", "file": "tests/conftest.py", "line": 4, "params": false}
    ]
  ],
  "nodes": {"tests/api/test_items.py::test_get_item[a]": 0},
  "errors": 0
}
```

| Field | Type | Meaning |
|---|---|---|
| `closures` | list of list | Each closure is the fixtures one test gets, **in setup order** (dependencies first, higher scopes first). Tests with the same setup share one closure. |
| `closures[][].name` | string | fixture name |
| `closures[][].scope` | string | `session`, `package`, `module`, `class` or `function` |
| `closures[][].file`, `.line` | string, int | where the fixture function is defined |
| `closures[][].params` | bool | the fixture is parametrized |
| `nodes` | object | pytest node id to index into `closures` |
| `errors` | int | number of collection errors in the run (the document still lists what did collect) |

## `codeterrain.routes/1` (view `routes`: web routes)

Produced by `python -m codeterrain._routes_runner --apps module:attr ...`, which imports each app and
asks the `iter_routes` plugin hook for its routes.

```json
{
  "schema": "codeterrain.routes/1",
  "generator": {"name": "codeterrain-routes-runner", "python": "3.12.13"},
  "routes": [
    {
      "app": "sampleapp.main:app",
      "method": "GET",
      "path": "/v1/items/{item_id}",
      "handler": "sampleapp.main.get_item",
      "file": "sampleapp/main.py",
      "line": 32,
      "dependencies": ["sampleapp.main.require_token"],
      "response_model": "Item"
    }
  ],
  "errors": [{"app": "pkg.gone:app", "error": "<traceback tail>", "chain": ["ModuleNotFoundError: ..."]}],
  "notes": []
}
```

| Field | Type | Meaning |
|---|---|---|
| `routes[].app` | string | the `module:attr` given on the command line |
| `routes[].method` | string | upper-case HTTP method; `WEBSOCKET` or `ANY` for the non-HTTP cases. One record per method. |
| `routes[].path` | string | full path including every router prefix and mount |
| `routes[].handler` | string | `module.qualname` of the handler with decorators and wrappers looked through |
| `routes[].file`, `.line` | string, int | where the handler is defined |
| `routes[].dependencies` | list of string | `module.qualname` of the dependency callables declared for the route (route, router and include-time) |
| `routes[].response_model` | string or null | response model class name |
| `routes[].also_head` | bool (optional) | on a `GET` record, the route answers `HEAD` too (folded into the `GET` record, not listed on its own); absent means false |
| `errors[]` | list | apps that failed to import: `error` is a traceback tail, `chain` (optional) lists `Type: message` from the outermost exception to its root cause. One failing app never stops the others. |
| `notes` | list of string (optional) | informational lines, for example that imports ran inside `moto.mock_aws()`; lines starting with `warning:` (a `Host` route that was not walked) are shown by `terrain doctor` |

## Language packs (in-process, static)

A language that needs no running code (Terraform, TypeScript) is added by a language pack rather
than an extractor: an installed distribution with one entry point in the `codeterrain.languages`
group, named after the language (lowercase letters, digits and `_`), pointing at an object with
`interface` and one pair of attributes or both:

| Attribute | Meaning |
|---|---|
| `interface` | the version of this contract it implements; this core reads `1` |
| `suffixes` | for `def` and `refs`: the file suffixes it reads, such as `(".tf",)` |
| `read(source)` | the facts of one file's text: `{"defs": [...], "refs": [...]}` |
| `call_suffixes` | for `calls` and `callers`: the front-end suffixes it reads, such as `(".ts",)` |
| `read_calls(source, suffix)` | the HTTP calls of one file: `{"calls": [...], "error": str or None}` |

Each definition is `{"name", "kind", "line", "doc"}` and each reference `{"name", "line", "scope"}`,
`scope` naming the definition the reference sits in, or `""`. Names are the language's own full names
(`aws_s3_bucket.logs`, `var.region`); `terrain def logs` also finds a definition whose last dotted part
is `logs`. `read` is a pure function of the text: it opens no file and runs nothing.

The core walks the repository for the pack's suffixes, skipping what every view skips, caches the
facts of each file by content and pack version, and checks every field: exact keys, strings of at most
1,000 characters, positive integer lines, at most 50,000 facts and 2,000,000 bytes per file. A pack
that raises on a file, or returns anything else, gives that file an error instead of facts.

Each call `read_calls` returns has the keys of the built-in reader's: `method` (`GET` to `OPTIONS`,
or `?`), `path` (`""` when not static), `line`, `style` (`client`, `fetch` or `object`), `resolved`
(whether `path` is set) and `raw` (the expression when unresolved, at most 120 characters); at most
20,000 calls per file. A file whose suffix a pack reads is read by that pack, the first by name when
several offer it, and any other by the built-in tokenizer; the facts are cached under the packs and
versions that read them, so installing, upgrading or removing a pack re-reads the files.

A pack runs in terrain's process. Before any pack is looked up, every import-path entry inside the
repository being read, and an empty or relative one, is removed, so a pack, the parts of a namespace
package and anything a pack imports later all resolve outside it; a pack whose distribution or module
still lies inside is refused before it is imported. Only what terrain itself runs from inside the
checkout (its virtual environment, the folder of its own code) is exempt. `terrain doctor` lists the
packs loaded and the ones refused.

## Plugin seams (Python reference extractor)

`codeterrain.hookspecs` defines two pluggy hooks that run **inside the target runtime**:

- `iter_routes(app)` (first result wins): return the route dicts above (without `app`; the runner adds
  it) or `None` when the plugin does not recognise the app. FastAPI/Starlette is the built-in plugin;
  more are listed under `plugins` in the config and registered in the same process.
- `tests_for(file)`: a seam only in v0.1; nothing calls it.

## Joining a manifest (core side)

The access class is not part of any extractor document. The core joins routes to the repository's own
manifest (`route_manifest`, with `manifest_keys` naming the fields) on every read, matching on method
and path, and on app when a manifest row lists the apps it applies to.
