# codeterrain-typescript

TypeScript and JavaScript for [codeterrain](https://github.com/benchli-sports/codeterrain): the HTTP
calls of a front end, read with [tree-sitter](https://tree-sitter.github.io/), in `terrain calls` and
`terrain callers`.

```
uv tool install "<path-to-the-codeterrain-checkout>[typescript]"
terrain calls orders                # GET /v1/orders  src/api/orders.ts:12  client
terrain callers POST /v1/orders     # the front-end calls that reach a route
```

Not published yet: install from a checkout of the codeterrain repository, where this pack lives under
`packages/`. Once published, `pip install "codeterrain[typescript]"` will do the same.
`terrain doctor` lists the installed packs (`language packs: typescript 0.1.0`). Without the pack,
`terrain calls` reads the same files with the core's own tokenizer.

Why install it: it is both more accurate and faster than the tokenizer. It parses the full syntax
and resolves names through scopes, and a cold read takes about 0.6 to 0.7 times the tokenizer's time,
since a tree-sitter query hands the reader only the nodes it needs. tree-sitter loads on the first
front-end file read, so commands that read none barely notice the pack.

The facts are the core's: `<receiver>.<verb>(path, ...)`, `fetch(path, {method})` and object literals
with a `url:` path. The syntax tree makes strings, templates, comments, type arguments and assertions,
spreads and computed keys exact, and names resolve through scopes: a name is read from the declaration
it refers to, so a `const url` inside a function is that function's, and a parameter hides a module
constant of the same name only inside its function. A file tree-sitter cannot parse whole is reported
with the lines of its syntax errors, and a call inside one counts as unresolved.

It depends on `tree-sitter` and the TypeScript and JavaScript grammars, which ship prebuilt wheels
for Linux, macOS and Windows; the versions are pinned exactly, since grammars rename nodes between
releases. The pack only parses the text it is given: it reads no other file and runs nothing.
