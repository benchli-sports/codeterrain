# v0.2 design: frontend calls and the backend join

Status: the front-end calls and the backend join shipped in 0.2.0; the TypeScript language pack and
the choice of tree-sitter (the last sections) in 0.3.0. This note records the decisions; the README
documents what ships.

## The question

"Which frontend code calls `GET /v1/items/{item_id}`, and which backend route does this frontend call hit?"
asked across repositories, locally, in under a second, with no LLM, like the v0.1 questions.

## What the frontends look like

A read-only survey of the target front ends found three call styles, often side by side:

- an HTTP client instance with a method per verb (`client.get('/v1/...')`), usually gathered in api
  modules;
- a hand-written fetch wrapper with the same shape;
- raw `fetch(path, {method})`;
- in an admin front end, a data-provider framework whose resource hooks build URLs at run time.

In most front ends nearly every path is written at the call site; in the admin front end a large share
goes through the data provider. No front end uses a generated client. Some prefix their calls with a base
URL that already holds the API version, so the call-site path leaves it out. Backend placeholders
(`{item_id}`) and frontend ones (`${itemId}`) never share names.

## Decision: a static scanner in Python

The calls view is a static tier, like the fixture index: a tokenizer for JavaScript and TypeScript written
in Python reads each file, never runs it, and caches per-file facts by content hash.

Rejected: running the TypeScript compiler API under the target's own Node.

- It runs target tooling outside the Python sandbox, where the network guard does not reach.
- Not every front end installs `typescript`; some are plain JSX.
- It would not resolve the one hard case either: data-provider resource names become URLs inside a
  hand-written function, which only evaluating that function resolves.

## What the scanner recognises

- `<receiver>.<verb>(path, ...)` for `get`, `post`, `put`, `patch`, `delete`, `head`, `options`, with
  optional type arguments (`get<List<Item>>(`), across lines. The first argument must look like a path
  (it starts with `/`, or with a `${base}` expression followed by `/`); that filter is what keeps
  `map.get('key')` out.
- `fetch(path, { method: 'POST' })`, GET when there is no `method`.
- An object literal with a `url:` path and an optional `method:` (data-provider custom calls, axios
  configs).
- A path given as an identifier is resolved through a `const NAME = '...'` or template in the same file.
- `typeof x.get` and other type-only mentions are not calls.

Each call records the method, the path as written (`${expr}` becomes `{}`), the file and line, and whether
the path was resolved. Unresolved calls are listed, not hidden, so coverage is visible per repository.

Paths are compared after normalisation: the query string is dropped, every parameter (`{item_id}`, `{}`,
`{slug:path}`) becomes a wildcard segment, and a configured `path_prefix` (for example `/v1`) is put in
front of the frontend path.

## Configuration

Front ends have no `pyproject.toml`. v0.2 reads the same keys from `--config`, and adds a repository-root
`codeterrain.toml` read like `[tool.codeterrain]`. New keys:

- `client_paths` (default: the whole repository, minus the directories never scanned): where to look.
- `path_prefix` (default `""`): prefixed to every call path before matching.

Linking repositories is reading outside the root, so it is user-only, like `pass_env`: `client_repos` lists
front-end roots and is honoured only from `--config` or the user config. An entry can be a table
(`{ path, path_prefix, client_paths }`) whose values win over the front end's own config, because the
front-end checkouts are often read-only and have no `codeterrain.toml`.

## Milestones

1. The tokenizer and call extractor, `terrain calls` in one repository. Tests cover the patterns the survey
   found, including those a regex gets wrong (nested generics, multi-line calls, nested templates, comments,
   type-only mentions).
2. `codeterrain.toml`, `path_prefix`, and same-file constant resolution.
3. The join: `terrain callers [METHOD PATH] [--unmatched]` in the backend, from the routes view and
   `client_repos`, with a warning for any checkout that is behind its remote or has uncommitted changes.
   Showing the matched route from the front-end side (which needs the backend named there) is left for
   later.
4. Measure on the target front ends: calls found, share resolved, share joined to a backend route.

Not in v0.2: data-provider resource hooks (a resource-to-path map is a candidate for later), and backends
that are not FastAPI.

Delivered by the TypeScript language pack (`codeterrain[typescript]`): tree-sitter grammars for JavaScript and TypeScript in place of the hand-written
tokenizer, which reaches its limits at regex/division ambiguity, semicolon insertion, JSX and type
syntax. tree-sitter is a parser, not a type checker: scope and cross-module resolution stay
codeterrain's own work, on a real syntax tree.

### Why tree-sitter, and why the tokenizer stays

The choice was made on two criteria: speed and accuracy.

- **Accuracy.** The pack reads a real syntax tree, so the tokenizer's known blind spots are gone:
  regex against division, automatic semicolon insertion, JSX text, type arguments and assertions.
  Names resolve through scopes, so a `const url` inside one function no longer has to be given up
  because another function binds the same name. A file that does not parse is reported with the
  lines of its syntax errors, and calls there count as unresolved rather than guessed. On the front
  ends it was measured on, both readers find the same calls and resolve the same paths, with no wrong
  path from either; the pack's gain is in the cases those front ends happen not to contain.
- **Speed.** A first version walked every syntax node in Python and took 1.2 to 1.5 times the
  tokenizer's time on a cold read. The reader now asks a tree-sitter query, built once per grammar,
  for only the nodes it needs (scopes, bindings and call sites) and makes one pass over them in
  source order. A cold read takes 0.6 to 0.7 times the tokenizer's time. Warm runs are served from
  the cache either way. tree-sitter loads on the first front-end file read, so the commands that
  never read one (`def`, `refs`, `fixtures` and the rest) pay a few milliseconds for the pack's
  discovery, not the parser's import.

The pack is therefore the reader whenever it is installed. The tokenizer stays as the reader when
the pack is missing: removing it would gain neither speed nor accuracy, since it never runs beside
the pack, and it keeps `terrain calls` working on an install whose only dependency is `pluggy`.
