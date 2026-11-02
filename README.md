<p align="center">
  <img src="https://raw.githubusercontent.com/benchli-sports/codeterrain/main/docs/img/logo.svg" alt="codeterrain logo" width="96">
</p>

<h1 align="center">codeterrain</h1>

<p align="center">
  <b><code>terrain</code>: exact answers about your code, in under a second.</b><br>
  For coding agents, and the people working beside them.
</p>

<p align="center">
  Local only &nbsp;·&nbsp; no LLM &nbsp;·&nbsp; no network calls of its own &nbsp;·&nbsp; Python, pytest, FastAPI &nbsp;·&nbsp; JavaScript and TypeScript front ends &nbsp;·&nbsp; Terraform
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> &nbsp;·&nbsp;
  <a href="#what-it-answers">What it answers</a> &nbsp;·&nbsp;
  <a href="#results">Results</a> &nbsp;·&nbsp;
  <a href="#inferred-token-savings">Token savings</a> &nbsp;·&nbsp;
  <a href="#how-it-works">How it works</a> &nbsp;·&nbsp;
  <a href="#documentation">Docs</a>
</p>

---

A coding agent spends most of its reading on structure it has to rediscover every session: which
fixtures a test really gets, where a route is handled, who calls a function, which tests would catch a
change to it. The answers are in the code, but finding them takes rounds of grep and file reads, and a
wrong guess costs a review round.

`terrain` answers those questions from a cache, the way the framework itself would answer them.

| | |
|:---|:---|
| **Exact** | Setup order equals `pytest --setup-plan` on every node tested. `refs` follows imports and re-exports, so an alias counts and a same-named function elsewhere does not. |
| **Fast** | Most warm answers take 0.3 to 0.5 s on a large backend, Python start-up included. |
| **Cheaper to read** | 4.4x to 8.8x fewer characters than the grep-and-read it replaces, on code bases with a real fixture graph. |
| **Safe to point at a repo** | The static tier never runs the target's code. The tier that has to import it runs in a sandbox with no credentials, and by default it cannot write into the repository. |

## Quick start

```sh
uv tool install <path-to-this-checkout>     # Python 3.12+; the only dependency is pluggy
# or "<path-to-this-checkout>[terraform]" to add the Terraform language pack, "[typescript]"
# to read front-end calls with tree-sitter, "[terraform,typescript]" for both
cd your-repo
terrain doctor                              # what it found, and which config it used
```

Ask about a function: where it is called, how each call passes its arguments, and which tests patch it.
This is `terrain` run on its own source:

```console
$ terrain refs is_test_module
function codeterrain.fixtures_static.is_test_module(rel)  src/codeterrain/fixtures_static.py:327
6 calls (6 in code, 0 in tests), 0 with a shape problem, 0 used as a value, 0 patched
file:line                                 in                             shape         note
src/codeterrain/code_static.py:1349       update                         1 positional
src/codeterrain/code_static.py:1357       update.<listcomp>              1 positional
src/codeterrain/fixtures_static.py:322    unlisted_test_trees.<genexpr>  1 positional
src/codeterrain/fixtures_static.py:435    build                          1 positional
src/codeterrain/impact.py:272             _Fixtures.__init__             1 positional
src/codeterrain/impact.py:424             _inherited_tests               1 positional
```

Ask which tests would catch a change to it, through its callers and the fixtures that call them
(abridged):

```console
$ terrain impact is_test_module --depth 2
17 tests reach codeterrain.fixtures_static.is_test_module (0 directly), through 15 callers within 2 levels; 2 tests patch it …
test                                                 depth  how                               via
tests/test_cli.py::test_a_handler_is_matched_by_…    2      call at tests/test_cli.py:1186    …update <- …is_test_module
tests/test_cli.py::test_a_route_text_that_matches_…  2      call at tests/test_cli.py:1142    …update <- …is_test_module
…
```

Ask what a fixture is and what depends on it (from the sample repository in `tests/sample_repo`):

```console
$ terrain fixture db
name: db
scope: session
autouse: no
params: none
deps: _env_session
defined: tests/conftest.py:9
overrides: -

dependents (4):
fixture  _reset_state  tests/conftest.py:19  arg
fixture  client  tests/conftest.py:34  arg
fixture  mod_cache  tests/conftest.py:14  arg
…
```

The commands that import your application (`setup-order`, `routes`) run in the **target's**
interpreter: pass `--python .venv/bin/python` or set `CODETERRAIN_PYTHON`. Add `--json` to any command
for the full data.

## What it answers

| Question | Command | What grep can't tell you |
|:---|:---|:---|
| Where is this defined? | `terrain def NAME` | Resolves a re-export to the real definition |
| Who calls it, passes it, patches it? | `terrain refs NAME` | Follows imports and aliases, flags calls that don't fit the signature, and separates tests that patch it out |
| Who imports this module, and what does it import? | `terrain importers MODULE`, `terrain imports MODULE` | Every import of it or of a name from it, relative imports resolved |
| Which tests would catch a change? | `terrain impact NAME` | Walks callers and fixtures the way pytest resolves them |
| Which fixtures does a test get, in what order? | `terrain setup-order NODEID` | pytest's own resolution, overrides and scopes included |
| What fixtures exist, and who uses one? | `terrain fixtures`, `terrain fixture NAME` | Which definition wins where, and every dependent |
| Where is a route handled, with what access? | `terrain routes`, `terrain route METHOD PATH` | Prefixes from router calls in other files, joined to your access manifest |
| Which HTTP calls does a front end make? | `terrain calls` | Paths built from templates, constants and concatenation |
| Which backend route does each call hit? | `terrain callers` | Front-end calls matched to backend routes across repositories |
| Where is a Terraform resource, variable or module, and who reads it? | `terrain def NAME`, `terrain refs NAME` (Terraform pack) | Terraform's own names (`aws_s3_bucket.logs`, `var.region`, `module.vpc`) and the block each reference sits in |
| What is the module tree, and which providers are pinned? | `terrain def --kind module`, `terrain def --kind required_provider` | Every module call with its source and version; provider aliases and what selects them |

Every option is in **[docs/commands.md](docs/commands.md)**.

## Results

Measured read-only on real code bases: a large FastAPI backend with a very large test suite, three
smaller Python code bases, an analytics project with a large fixture graph, and five front ends.

<table>
<tr>
<td align="center" width="25%"><h2>11 / 11</h2>test nodes where setup order equals <code>pytest --setup-plan</code></td>
<td align="center" width="25%"><h2>0.35 s</h2>median warm <code>fixtures</code> answer on the large backend</td>
<td align="center" width="25%"><h2>up to 8.8x</h2>fewer characters for an agent to read</td>
<td align="center" width="25%"><h2>0</h2>answers marked wrong in agents' logged lookups</td>
</tr>
</table>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/benchli-sports/codeterrain/main/docs/img/accuracy-dark.svg">
  <img src="https://raw.githubusercontent.com/benchli-sports/codeterrain/main/docs/img/accuracy-light.svg" alt="Accuracy: setup order equal to pytest on 11 of 11 nodes; every route joined to the access manifest; 97% and 96% of front-end calls matched to a route; 99% of calls found by refs against grep in a monorepo" width="760">
</picture>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/benchli-sports/codeterrain/main/docs/img/speed-dark.svg">
  <img src="https://raw.githubusercontent.com/benchli-sports/codeterrain/main/docs/img/speed-light.svg" alt="Warm answers on the backend, Python start-up included: fixtures 0.35 s, routes 0.35 s, refs 0.36 s, def 0.45 s, setup-order 0.49 s, impact 1.17 s, against a goal of under 1 s" width="760">
</picture>

`impact` is the one answer over a second: it walks callers and fixtures, and on the backend a heavily
tested function is reached by about a thousand tests. A cold build costs seconds for the static index
and about a minute and a half for the routes. After that, answers come from the cache, and a view is
stamped stale when files change.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/benchli-sports/codeterrain/main/docs/img/tokens-dark.svg">
  <img src="https://raw.githubusercontent.com/benchli-sports/codeterrain/main/docs/img/tokens-light.svg" alt="Characters an agent reads, indexed to today's grep-and-read at 100: backend autouse fixtures 23, a route's handler and access 14, who uses the main fixture 67; analytics who uses the main fixture 11, fixtures of a test 29; a small suite's fixtures of a test 125" width="760">
</picture>

**The saving scales with the code.** Where the fixture graph is large, an answer is a small fraction
of the grep-and-read it replaces. In a small suite with a couple of fixtures the answer can be longer
than the grep; there the gain is the exact order, which grep can't show.

### In the field

Coding agents working on real tasks logged each `terrain` lookup: what they asked, whether the answer
was right, and what they would have done instead.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/benchli-sports/codeterrain/main/docs/img/field-dark.svg">
  <img src="https://raw.githubusercontent.com/benchli-sports/codeterrain/main/docs/img/field-light.svg" alt="Outcomes of logged lookups: answered 79%, partly answered 12%, no help 2%, error 7% (mostly the wrong command), wrong answer 0%" width="760">
</picture>

- Each fully answered lookup saved the agent **about 1.3 tool calls**, by its own count.
- **No answer was marked wrong.** The partial answers came from output cut by the token budget, a cache
  built from a different checkout than the one being edited, and a value read through a module alias.
- Most errors were the wrong command: `callers`, which is for routes, where `refs` was meant.

## Inferred token savings

An estimate from the two measurements above, not an A/B experiment:

| Step | Figure |
|:---|---:|
| Reading saved on a question `terrain` answers, large backend (4.4x to 6.9x fewer characters) | 77% to 86% |
| Lookups answered fully, in the field | 79% |
| Lookups answered partly (counted at half the saving) | 12% |
| **Inferred share of lookup reading saved on a large Python backend** | **about 65% to 73%** |

Lookups are only part of a session: reasoning, editing and test runs are untouched, and small code
bases save little. Tokens here are characters divided by four. A session-level A/B, the same lane run
with and without `terrain`, is the next measurement. The method and the per-repository results are in
[docs/why-codeterrain.md](docs/why-codeterrain.md).

## How it works

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/benchli-sports/codeterrain/main/docs/img/architecture-dark.svg">
  <img src="https://raw.githubusercontent.com/benchli-sports/codeterrain/main/docs/img/architecture-light.svg" alt="How it works: a terrain question is answered from a cache per repository, keyed on content, in under a second when warm. The static tier (fixtures and tests and the code index with Python's ast, front-end calls with tree-sitter or a tokenizer) fills it from changed files only and never runs the code. The import tier, a sandboxed subprocess, fills it on refresh with the setup order and the routes." width="760">
</picture>

- **The static tier** reads source with Python's `ast`, and front-end files with tree-sitter when the
  TypeScript pack is installed (chosen for speed and accuracy) or a tokenizer written in Python when
  it is not. It never imports or runs the target, and re-reads only the files that changed.
- **The import tier** needs the real framework: pytest's fixture resolution, FastAPI's route table. It
  runs in a subprocess using the target's own interpreter, with an allowlisted environment, dummy cloud
  credentials, no proxy and a best-effort network guard.
- **The cache** lives outside the checkout, in the git common directory or your user cache, and is
  keyed on content hashes.

More in [docs/how-it-works.md](docs/how-it-works.md), [docs/sandbox.md](docs/sandbox.md) and
[SECURITY.md](SECURITY.md).

## Why this exists

codeterrain started inside [benchli](https://www.benchli.com), the team sports platform built by Mitbolas GmbH. Its Python backend
is a large FastAPI service with a very large test suite, developed with coding agents. Most of the
agents' tokens went on re-mapping the same structure every session, and a hand-kept map went stale
within weeks. The goal became exact answers, straight from the code, locally and in under a second, in
every repository benchli and Mitbolas develop with agents. The full story, and why we did not adopt a
knowledge-graph tool, are in [docs/why-codeterrain.md](docs/why-codeterrain.md).

## Documentation

| | |
|:---|:---|
| [Commands](docs/commands.md) | Every command, option and output |
| [Configuration](docs/configuration.md) | Precedence, every key with its default, monorepos and non-default layouts |
| [How it works](docs/how-it-works.md) | The two tiers, the cache, extractor programs |
| [The sandbox](docs/sandbox.md) | What the import tier runs with, and every gap in the network guard |
| [Limits](docs/limits.md) | What a static walk does not see, and the bounds set on purpose |
| [Security](SECURITY.md) | The trust model: what is guaranteed and what is best-effort |
| [Extractor protocol](docs/extractor-protocol.md) | Writing an extractor or a language pack for another language or framework |
| [Terraform pack](packages/codeterrain-terraform/README.md) | `.tf` files in `def` and `refs`: resources, modules, providers |
| [TypeScript pack](packages/codeterrain-typescript/README.md) | Front-end calls read with tree-sitter, and why it is the reader to install |
| [Design notes](docs/design-v0.2.md) | The decisions behind the front-end calls and the language packs |
| [Why codeterrain](docs/why-codeterrain.md) | The origin, the measurements, what is not proven yet |
| [Changelog](CHANGELOG.md) | What changed, release by release |

## Development

```sh
uv run --extra dev ruff check .
uv run --extra dev pytest
```

Run pytest as a single process (no `-n`). The suite spawns real pytest and interpreter subprocesses, and
a few tests skip on Windows (no `sendmsg`, no unix-domain sockets). CI also runs it against FastAPI below
and above 0.137 and pytest 8 and 9. Contributor and review rules are in [AGENTS.md](AGENTS.md).

Copyright Mitbolas GmbH. Licensed under Apache-2.0 (see LICENSE and NOTICE).
