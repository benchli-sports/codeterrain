# The sandbox

What the import and pytest tier runs with, and every gap in the network guard. The trust model is in [SECURITY.md](../SECURITY.md).

Running codeterrain's import and pytest tier on a repository runs that repository's code, exactly as
running its tests would. What codeterrain guarantees is about its own process: it never executes target
code itself, never hands the child anything you did not opt into, never lets the child write into the
repository by default, and always ends the child. The trust model and what is guaranteed versus
best-effort are in [SECURITY.md](../SECURITY.md).

Importing an application can call cloud APIs at import time. Every import or pytest subprocess therefore
runs with:

- an environment built from an allowlist: `PATH`, `LANG`, `LC_ALL`, `TMPDIR`, `TEMP`, `TMP` (plus the
  Windows basics such as `SYSTEMROOT`), never the parent's whole environment. `import_env` and then
  `pass_env` (names copied from the parent; user only, see [Configuration](configuration.md)) are layered on top;
- `HOME`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, `XDG_CONFIG_HOME`, `XDG_CACHE_HOME` and
  `XDG_DATA_HOME` pointed at empty directories in the run directory, so SDKs and tools that look for
  credential files under your home (gh, gcloud, az, `~/.aws`, netrc and the like) find none. Only
  `pass_env` in your own config can hand any of them through, on purpose. This hides the files found
  through those variables. Code that reads your real home by absolute path, or asks an OS credential
  store (Windows Credential Manager, macOS Keychain), is not stopped;
- git told to read no system or global config and to use no credential helper (`GIT_CONFIG_NOSYSTEM=1`,
  `GIT_CONFIG_GLOBAL` pointed at a missing file, an empty `credential.helper`, `GIT_TERMINAL_PROMPT=0`,
  `GCM_INTERACTIVE=never`), so a `git` the target starts cannot use your stored token. Git for Windows
  sets Git Credential Manager in the system config, outside the home directory;
- dummy AWS keys, the credentials and config files pointed at a path that does not exist, the EC2
  metadata service off; neither `import_env` nor `pass_env` can bring an `AWS_*` credential back;
- every `*_proxy` variable removed (even if listed in `pass_env`), `NO_PROXY=*`, and `PYTEST_ADDOPTS` /
  `PYTEST_PLUGINS` removed;
- a socket guard that allows loopback only: connect, send, name lookups and `create_connection` to
  anything else raise `codeterrain sandbox: non-loopback network access is blocked`. The extractor is
  started through a bootstrap that installs the guard, checks it is active (otherwise it exits with
  `codeterrain sandbox: network guard not active (<reason>)`, reported as one line) and only then runs
  the extractor. On Windows asyncio's proactor loop (the default loop, whichever way a target selects
  it) is guarded at the proactor itself, and the bootstrap checks that too; the selector loop is only
  preferred.
- unix-domain sockets are refused as well, so a target that talks to a local daemon over one
  (`logging.handlers.SysLogHandler`, the multiprocessing forkserver, docker or redis unix clients)
  fails with the same guard error. `isinstance(sock, _socket.socket)` is False for sockets made by
  `socket.socket` while the guard is installed.
- a fresh run directory under the system temp directory (`0700` on POSIX), never under the cache, so the
  child is not told where the cache is (it can still work it out from the root; see SECURITY.md). Its stdout and stderr go to files there, and only the last
  4,000 characters of each are read back. The result document comes back through a JSON file, never
  stdout, and is refused above 256 MB.
- a timeout (30 minutes for an extraction, 60 s for the version probe) that kills the whole process
  tree. On Windows the child runs in a job object that kills everything it started, orphans included,
  on timeout and after a normal exit; when a job object cannot be created, a warning says so and
  `taskkill /T /F` (by its absolute path) is used on timeout. On POSIX the child's own process group is
  killed on timeout and after a normal exit; a grandchild that starts its own session can survive. The
  run directory, with the child's raw output files, is removed afterwards, and a warning names it if it
  cannot be.

Every program codeterrain starts (`git`, the target interpreter) is resolved to an absolute path from the
absolute `PATH` entries only, never from the current directory, so a `git.exe` or `python3.exe` shipped
in the repository is never run. On Windows only `.exe` and `.com` files are run, as `CreateProcess`
does: a `.bat` or `.cmd` runs through `cmd.exe`, which would read `&` in a checkout path as a command, so
one is refused, and a driveless entry such as `\tools` is skipped. A bare name whose match lies inside the
target repository (an absolute `PATH` entry such as `<checkout>/bin`) is refused with a message; an
interpreter path you give explicitly, such as the target's `.venv`, is your choice.

Text from the child (import errors, tracebacks, output tails, node ids, fixture and route fields) has
every `import_env` value, every `pass_env` value and the dummy credential replaced with `[REDACTED]`
before it is cached or printed. A leading or trailing run of 12 characters or more of a longer value is
replaced too, since some libraries print a long value shortened (pydantic keeps both ends around `...`).
Values shorter than 6 characters are left alone; they hold no meaningful secret and replacing `test` or
`1` would corrupt the paths in a traceback. Only values codeterrain knows are redacted: a secret the
target reads from a file and prints is not.

The project's pytest `addopts` are switched off for the extraction run (`-o addopts=`) so a report option
cannot write into the repository; `respect_addopts = true` keeps them. Either way the dump plugin
refuses the run, before pytest opens anything, if `--junitxml`, `--html`, `--log-file`, `--debug`,
`--report-log`, `--json-report`, `--alluredir`, a file-writing `--cov-report` or an ini `log_file` set
by `-o` is in force, whether it came from the project's options or from `pytest_args`.

What pytest 8 and 9 can write, and what codeterrain does about each:

| writer | handled by |
|---|---|
| `--junitxml`, `junit_*` | `junit_*` only shape that report; the report itself needs the option, which is refused from `pytest_args` and from the project's options |
| `log_file`, `log_file_*` (ini) | `-o log_file=` on every extraction run, in both modes; a later `-o log_file=...` is refused |
| `--log-file` | refused |
| `--debug` (`pytestdebug.log`) | refused, before pytest opens the file |
| cache provider (`cache_dir`, `.pytest_cache`) | `-p no:cacheprovider` on every run |
| `--basetemp`, `tmp_path_retention_*` | collection creates no temporary directories |
| `__pycache__`, `.coverage` | `PYTHONDONTWRITEBYTECODE=1`; `COVERAGE_FILE` points into the run directory |
| `--html`, `--report-log`, `--json-report`, `--alluredir`, `--cov-report` (plugins) | refused |

The refusal is a list of the known writers above, not a general guard: an option of another plugin
that writes files is not refused. Other options in `pytest_args` are passed through unchanged and are
your responsibility. A `data_file`
set in the project's own coverage configuration is the project's choice.

Names, paths and messages taken from the repository are printed with terminal controls escaped: in each
field every C0 and C1 control (newline and tab included), DEL, the bidi marks, overrides and isolates,
the Unicode line and paragraph separators and lone surrogates (an undecodable file name) become `\xNN` or
`\uNNNN` before the field is placed in a line, and the finished output is escaped again apart
from its own newlines. A hostile string cannot clear the screen or forge a row. `--json` output carries
them as JSON escapes.

**The guard is best-effort protection against accidental network calls from the code under analysis, not
a security boundary against hostile code.** It is installed before the extractor imports the target;
`.pth` files in the target environment run earlier. It does not cover:

- non-Python subprocesses (`curl`, `az`, `git`, anything started with `os.system` or `subprocess`);
- Python children started in isolated mode (`python -I`, `-E` or `-S`), which skip the guard's
  `sitecustomize`;
- a freshly loaded `_socket` extension module, or target code that simply undoes the patch
  (`del socket.socket.connect`);
- references to socket functions taken before it was installed, including the original C socket class
  (`socket.socket.__mro__[1]`), which Python will not let anyone patch;
- `getfqdn`, which swallows the guard error from its reverse lookup;
- uvloop, aiodns, or C extensions that open sockets themselves.

Loopback services (a local proxy on 127.0.0.1, localstack, a docker TCP socket) are reachable by design.
