# Security

## Trust model

Running codeterrain's import and pytest tier (`setup-order`, `routes`, `refresh`) on a repository runs
that repository's code in a child process, exactly as running its tests would. Read a repository you do
not trust the way you would run its tests: in a container or a throwaway machine.

What codeterrain does guarantee is about its own, parent process:

1. **It never executes target code.** The static tier (`fixtures`, `fixture`) reads files with `ast`
   only. Every program it starts (`git`, the target interpreter) is resolved to an absolute path from
   the absolute `PATH` entries, never from the current directory, never a `.bat` or `.cmd` on Windows,
   and never a program a bare name finds inside the target repository or, for `terrain callers`,
   inside any front-end checkout it reads, whichever checkout it is asking about. The `git status` that
   `terrain callers` runs in each checkout switches `core.fsmonitor` off, so a command named in that
   checkout's `.git/config` is not run, and takes no optional locks, so it writes nothing there.
   A language pack runs in terrain's own process. Before any pack is looked up, every import-path
   entry inside the repository being read (and an empty or relative one) is removed, so neither a
   pack, the parts of a namespace package nor anything a pack imports later resolves there; a pack
   whose distribution or module still lies inside it is refused before it is imported. What terrain
   itself runs from is the exception, when it lies inside the checkout: its virtual environment and
   the folder its own code is imported from. A checkout inside terrain's environment is not trusted.
2. **It never hands the child anything you did not opt into.** The child's environment is an allowlist;
   proxies, `PYTEST_ADDOPTS` and `PYTEST_PLUGINS` are dropped; AWS credentials are dummies; the home and
   per-user data directories point at an empty directory; git reads no system or global config and has
   no credential helper. Configuration in the target repository is **not** your opt-in: `pass_env`,
   `pytest_args` and `respect_addopts` are honoured only from `--config` or your user config, and
   ignored, with a warning, when the repository's `pyproject.toml` sets them. A user-config `pass_env`
   applies to every repository you run on, trusted or not; a `--config` file scopes it to one run.
3. **It never lets the child write into the repository by default.** `addopts` are switched off,
   `log_file` is blanked, and the known file-writing options of pytest and common plugins are refused,
   from the project's options and from `pytest_args`. The child runs in a private temp directory,
   outside the cache, which is removed afterwards.
4. **It ends the child and what it started.** A timeout kills the whole process tree. On Windows a job
   object also kills what the child left behind after a normal exit; on POSIX its process group is.

Text that comes back from the child (errors, tracebacks, output tails, node ids, route fields) has
your `import_env` and `pass_env` values, and their long leading and trailing fragments, replaced with
`[REDACTED]` before it is cached or printed, and terminal controls are escaped in every plaintext
output. `test_paths` (the default `tests` included) and `route_manifest` must resolve inside the
repository root. On POSIX the cache is `0700`/`0600`. `pass_env` value digests are keyed with a random
per-user secret kept in the per-user cache directory, never in the checkout, so a copied `.git` does
not carry it.

What the redirected home does not do: it hides credential files from the tools that look for them
through `HOME`, `APPDATA` and the like. Code that reads your real home by absolute path, or asks an OS
credential store (Windows Credential Manager, macOS Keychain), still reaches them; that is hostile code,
for which the trust model above applies.

The child is not told where the cache is, but can work it out from the root (`.git/codeterrain`, or the
per-user directory named by a hash of the root) and rewrite it. On Windows a cache inside the checkout
inherits the checkout's permissions; on a shared machine with a permissive drive ACL (the default on a
non-system drive grants every authenticated user modify rights) other local users can read and change
it. Use `--cache-dir` or `CODETERRAIN_CACHE_DIR` under your profile there.

The code index is a SQLite database that is updated in place, not replaced. It lives in a folder of
its own, where SQLite also creates its journal. codeterrain does not open it when that folder or the
database is a link or the database has a second name (a hard link), and, on POSIX, when the folder is
not the user's alone, or the database or any folder above belongs to another user or is writable by
others without the sticky bit; the command stops and names `--cache-dir`. Its identity is checked
again right after SQLite opens it, so a file swapped in that window is refused. On Windows, where a
folder cannot be made private without ACL calls, the code index goes in the per-user cache under your
profile whenever the cache directory is outside it.

Every view is signed with the per-user key when it is written: the slow-tier views (setup order and
routes) after the protocol check, the static views (fixtures, front-end calls, language packs) after
the facts they are built from were checked. A signed view of the current format is served without
checking it again; a view whose signature does not hold (edited, unsigned, another user's key) is
checked before it is served, as every view was before, and a static view that passes is signed
again. The signature saves that check and nothing more: it is not a
boundary against a child that reads the key by absolute path, which could already write a view that
passes the check.

## Guaranteed versus best-effort

| | |
|---|---|
| **Guaranteed** (each has a regression test) | the parent never runs target code or a repository-shipped executable; the environment allowlist; repository config cannot widen it; git in the child has no credential helper; no default writes into the target; tree kill on timeout and of stragglers after exit (except a POSIX grandchild that started its own session); redaction of the values codeterrain knows; terminal escaping; path confinement |
| **Best-effort** | the network guard. It blocks accidental network calls from Python code in the child, and is not a boundary against hostile code: non-Python subprocesses, isolated-mode Python children, a freshly loaded `_socket`, target code that undoes the patch, `getfqdn`, the C socket class and C extensions are not covered. [docs/sandbox.md](docs/sandbox.md) lists every gap. |
| **Not covered** | secrets codeterrain does not know (a value the target reads from a file and echoes); loopback services, which are reachable by design; a POSIX grandchild that started its own session; hostile code reading the real home by absolute path or an OS credential store; a child that rewrites the cache it worked out from the root; a crash of a native parser a language pack uses (the TypeScript pack's tree-sitter parses front-end source in terrain's process: it runs nothing from that source, and a file over 2,000,000 bytes is not read, but a parser fault would end the command rather than report the file) |

## Reporting a vulnerability

The repository is private for now. Open a private security advisory on the repository
(Security, then Advisories, then "Report a vulnerability"), with a reproduction if you have one. Do not
open a public issue.
