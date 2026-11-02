# How it works

The two tiers, the cache, and the extractor programs.

## Two tiers, one cache

- **Static tier** (`ast` only): fixtures and the tests that request them. Always rebuilt incrementally,
  well under a second when warm. The front-end calls view is static too: tree-sitter when the
  TypeScript pack is installed (more accurate, and faster on a cold read), otherwise a tokenizer
  written in Python; neither runs Node or the code it reads. The code index behind
  `def` and `refs` is static too, kept in a SQLite database (`code-<id>/index.sqlite3`) in the cache
  directory (on Windows, in the per-user cache when the cache directory is outside your profile) and
  updated for the files whose content changed. Language packs (Terraform's `.tf` files) add views of
  their own, read the same way: parse only, never run.
- **One walk per answer.** The code index and each language pack share one walk of the checkout,
  and a file is re-read only when its `(mtime_ns, size)` changed.
- **Import and pytest tier**: setup order and routes. These import your application and run
  `pytest --collect-only` in a sandboxed subprocess. The cached result is served at once, stamped
  `stale: N files changed since <time>` when files moved, and refreshed by `codeterrain refresh` or
  `--fresh`. It never blocks otherwise.

The cache lives in `<git-common-dir>/codeterrain/`, so one repository has one cache shared by all its
worktrees, keyed on content hashes and memoised on `(mtime_ns, size)`. Outside a git repository (or when
git cannot be run) it lives in a per-user cache directory instead
(`%LOCALAPPDATA%\codeterrain\<id>` on Windows, `$XDG_CACHE_HOME/codeterrain/<id>` otherwise, default
`~/.cache`). codeterrain never writes into the repository it reads. `--cache-dir` and
`CODETERRAIN_CACHE_DIR` override both. On POSIX the directories codeterrain creates there are `0700` and
its files `0600`, whatever the umask; on Windows the per-user profile's permissions apply.

**Worktrees.** `codeterrain` answers about the checkout it runs in (the current directory, or `--root`),
uncommitted changes included. Run it from the worktree you are editing: an answer from the main
checkout does not see a worktree's changes. One cache directory, the default or one
`CODETERRAIN_CACHE_DIR`, can serve every worktree of a repository: parsed files are shared by content,
and the file memo, the code index and the views are kept per checkout. When the repository has other
worktrees, each answer ends with a line on stderr naming the checkout it came from, the JSON envelope
carries `root`, `codeterrain doctor` lists the other checkouts, and `--root` pointing at another checkout
than the current directory's is flagged.

Every cached file carries a format number and the codeterrain version; a file from another version, with
a field missing or unreadable, is discarded and rebuilt. Every view is signed with a per-user key when
it is written, so a warm answer does not check every row again; an unsigned or edited view is checked
first (see [SECURITY.md](../SECURITY.md)). A setup-order or routes view also records the
config, target interpreter (its venv config and installed distributions) and codeterrain version it was
built under: if any of those changed it is not served, `codeterrain doctor` shows `needs refresh`, and the
next query rebuilds it. The staleness count also watches `pyproject.toml`, `pytest.ini`, `setup.cfg`,
`tox.ini` and every `conftest.py`.

## Extractors are separate programs

The pytest and route extractors run inside the target's own interpreter and hand back JSON documents
(`codeterrain.order/1`, `codeterrain.routes/1`); the core only reads them and refuses unknown versions.
See `docs/extractor-protocol.md`.
