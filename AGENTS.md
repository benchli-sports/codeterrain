# codeterrain: instructions for coding agents and contributors

codeterrain is a local, LLM-free, network-free command line tool (`codeterrain`, also installed as
`terrain`) that answers questions about a repository's code from a cache, in under a second. Python
first: pytest fixture order, FastAPI routes, definitions and references; plus the HTTP calls of a
JavaScript or TypeScript front end. See
[README.md](README.md) for what it does and [docs/extractor-protocol.md](docs/extractor-protocol.md) for
the extractor protocol.

## Code review rules

These rules set severity for every review of this repository, by people or by automated reviewers
(the GitHub Codex code and security reviews included). codeterrain is a local developer tool: it has
no server, no network access and no stored credentials, and [SECURITY.md](SECURITY.md) states what it
guarantees and what it does not.

### Severity

- **P1** only for a break of a guarantee in [SECURITY.md](SECURITY.md) (the static tier runs target
  code, the child gets something the user did not opt into, the child writes into the repository, a
  program is resolved from inside the target, a secret reaches output or the cache), or a write
  outside the cache directory.
- **P2** for a wrong answer on ordinary code: a definition, reference, fixture order or route that
  codeterrain reports wrongly for code a real project writes, or a crash on such code.
- **P3 at most** for:
  - resource use (time, memory, disk) driven by a file crafted for it, when a limit already bounds
    that kind of work: say which input defeats which limit, by how much;
  - a wrong or missing answer for Python whose meaning depends on run-time state (rebinding in
    branches, `exec`, monkeypatching, dynamic attributes): static resolution is an approximation, and
    leaving a name unresolved is the intended answer when it cannot be known;
  - a missing regression lock.
- No style, naming, lint or formatting findings: CI owns them.

### Scope

- Review the changed lines and the code they call or are called by. A defect in untouched code is a
  finding only if the diff newly exposes it; name what in the diff exposes it.
- One finding per root cause, with every affected call site in that one finding.

### Settled threads

- A thread resolved with a reply that links a commit or an issue ("deferred to #NNN"), or that
  rebuts the premise, is settled. Do not raise it or a variant of it again unless a later commit
  changes that code or you have a new concrete failure the reply does not address.

### Every finding

- States the input, the command run, and the wrong outcome; for resource findings, the measured
  cost against the limit that should have bounded it. No scenario, no finding.

For contributors answering a review: two findings of one class in a round means stop patching
instances and sweep the class (every visitor, query or limit of that kind) before the next push.

## Development

```
uv run --extra dev ruff check .
uv run --extra dev pytest
```

- Python 3.12 or newer. The only runtime dependency of the core is `pluggy`.
- Run pytest as a single process (no `-n`). The suite spawns real pytest and interpreter subprocesses.
- CI runs the two commands above (lint, then the tests against FastAPI below and above 0.137 and pytest 8
  and 9, plus a Windows job). There is no format gate: do not run `ruff format` over existing files.
- The tree uses the 100-column limit (`ruff` rules `E`, `F`, `I`, `UP`, `B`).

## Conventions

- **LF line endings** in every file, in the index and the working copy.
- **The static tier never imports target code.** `fixtures_static.py` reads files with `ast` only.
  Anything that has to import the target runs in the sandbox, in the target's own interpreter.
- **Every sandbox or security change ships a regression test and a CHANGELOG note**, and the list in
  [docs/sandbox.md](docs/sandbox.md) of what the guard does and does not cover stays true.
- **Generic code.** Project specifics live in configuration, never in the code, the defaults, the tests
  or the fixtures. Tests build `tmp_path` repositories or use `tests/sample_repo`.
- **Naming rule for the docs:** the origin story names the project and company codeterrain grew out of in
  `README.md`, `docs/why-codeterrain.md` and the CHANGELOG, and nowhere else. Other projects are described
  by shape, never by name.
- Every fix ships its regression test in the file that owns the behaviour, as a new case or function.
