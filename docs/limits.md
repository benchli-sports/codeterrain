# Limits

What codeterrain does not see or bounds on purpose.

- `setup-order` matches `pytest --setup-plan` in order and scope, with one deliberate difference: a
  fixture reached by `parametrize(..., indirect=True, scope="module")` is shown at the scope pytest
  runs it at (module), where the plan prints its declared scope. That scope is read from a private
  pytest attribute (`callspec._arg2scope`); without it the declared scope is shown.
- Fixture overrides and dependents are resolved by name from static facts; fixtures contributed by
  plugins or `pytest_plugins` are not seen statically (they do appear in `setup-order`).
- FastAPI route walking uses two private helpers of FastAPI and pytest, each isolated in one function with a
  version-guarded fallback and a test.
- `Host` routes are not walked (`codeterrain doctor` warns when it meets one). FastAPI before 0.137 drops
  mounts inside included routers itself, so they cannot be listed there.
- The view key covers the target interpreter's own site-packages; editable sibling packages and the user
  site-packages are not keyed, so run `codeterrain refresh` after changing them.
- The network guard does not see non-Python subprocesses, isolated-mode Python children, uvloop, aiodns
  or network access from C extensions (see [the sandbox](sandbox.md)).
- On POSIX a grandchild that starts its own session can survive the run; on Windows a process escapes
  only when the job object cannot be created, and a warning says so.
- The home redirect hides credential files from SDK discovery; code that reads the real home by
  absolute path or asks an OS credential store is not stopped (see [SECURITY.md](../SECURITY.md)).
- The target sees an empty home directory, so a project that relies on packages in the user
  site-packages, or on config under the real home, needs them passed through with user-level `pass_env`.
- Two applications defined in one module are not told apart by the route manifest join.
- The socket guard is a best-effort one: Python's `getfqdn` swallows the guard error from its reverse
  lookup, and the original C socket class (`socket.socket.__mro__[1]`) cannot be patched.
- The static object store only grows with edits; old content hashes are not pruned yet.
- With the TypeScript pack installed, `codeterrain calls` reads JavaScript and TypeScript with
  tree-sitter. It still reads one file at a time: a path built in another module, by a wrapper
  function or from a resource name that a data provider turns into a URL is listed as unresolved, and
  a call inside a syntax error is unresolved.
- Without the pack, `codeterrain calls` reads JavaScript and TypeScript with a tokenizer written for this
  purpose, not a parser. It knows strings, templates, comments, regex literals and brackets, not the
  grammar, so:
  - It reads one file at a time. A path built in another module, by a wrapper function or from a
    resource name that a data provider turns into a URL is listed as unresolved.
  - It does not track scopes. Only a `const` holding a string is resolved, and only when the file
    binds that name nowhere else (as a parameter, a `let` or `var`, a destructured name, or a second
    `const` with a different value); otherwise paths built on it are unresolved. A correct path can
    be lost this way, but an inner binding does not lend its value to an outer call.
  - The text of a template literal is not scanned for calls, only its `${...}` expressions.
  - Syntax it does not model can still mislead it: a `/` it reads as division when it is a regex (or
    the reverse), automatic semicolon insertion, JSX text with quotes, unusual type syntax.
    Its rules lean towards "unresolved" rather than a wrong path, but that is not guaranteed. Edge
    cases of this kind are known; none changed a result on the front ends it was measured on, but a
    wrong path is possible.
  - A file whose calls are nested beyond its work bound is reported as a scan error, not scanned.

  The TypeScript pack (`codeterrain[typescript]`) removes these: it parses the full syntax and
  resolves names through scopes, and it is faster on a cold read. See
  [design-v0.2.md](design-v0.2.md#why-tree-sitter-and-why-the-tokenizer-stays) for the measurements.
- The Terraform pack reads `.tf` files with a tokenizer, not an evaluator: it follows no module
  `source` (a module's contents are the `.tf` files in its own folder), expands no `count` or
  `for_each`, evaluates no expression and reads no `.tfvars`. `.tf.json` files are not read. A
  resource that selects no provider uses its default one implicitly, which is not listed as a
  reference.
- A fetch of a static file the front end serves itself (`/config.json`) is listed as a call and hits
  no route.
- `codeterrain refs` resolves names statically, file by file. A call on a value whose type is only known
  at run time (a parameter, a function's return value, a dependency-injected object) is a by-name
  candidate, not a reference; `getattr`, string dispatch and names built at run time are not seen; code
  inside a string (a script run in a subprocess) is not code. A module-level variable is a definition;
  what it holds is followed only when its class made it (`settings = Settings()`) or its annotation
  names one, read at its last assignment in its file, and an alias of a name is followed one level.
- `codeterrain impact` follows the calls `codeterrain refs` resolves, so it inherits those limits, and it does
  not follow a name used as a value: a FastAPI dependency (`Depends(get_user)`) reaches the routes
  that declare it, and the tests that call those routes over HTTP, only at run time. A call inside a
  nested function counts as a call from the function that holds it. Fixture visibility follows
  pytest's own order, but a fixture supplied by `pytest_generate_tests`, a plugin's hooks or a
  parametrized indirect value is not seen. It follows at most 2,000 callers, expands at most 2,000
  fixtures and checks at most 1,000,000 fixture requests.
- `codeterrain env` sees a variable named by a string literal only: `os.environ[name]` with a computed
  name, `dotenv` files, Docker Compose and Kubernetes YAML are not read. A settings class's prefix or
  alias counts only as a literal, and a settings class built by a factory is not seen. Terraform is
  read by shape, not by provider schema: a map under one of its `env` keys counts wherever it sits,
  and per-environment values in `.tfvars` or workspaces are not expanded.
- `codeterrain callers` joins FastAPI routes only; an API served by anything else has no routes view to
  join to.
- `codeterrain callers` reads the working copies of the backend and the front ends. A checkout that is behind
  its remote, or holds uncommitted work, can show a gap that the deployed code does not have, or hide one
  that it does. It warns about both, but "behind" is only as fresh as that checkout's last fetch:
  nothing is fetched.

The CHANGELOG lists the same gaps, plus the test-coverage ones.
