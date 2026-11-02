"""The only place codeterrain starts a subprocess that imports or runs the target's code.

Importing an application can call AWS (or any network service) at import time, so every such
subprocess runs with:

- an environment built from an allowlist (PATH, locale and temp variables, plus the Windows
  basics), never the parent's whole environment; the config's ``import_env`` and the opt-in
  ``pass_env`` are layered on top;
- the home and per-user data directories (``HOME``, ``USERPROFILE``, ``APPDATA``,
  ``LOCALAPPDATA``, ``XDG_*``) pointed at an empty directory in the run dir, so SDKs that look for
  credential files there find none; only user-level ``pass_env`` restores them. Code that reads
  the real home by absolute path, or asks an OS credential store, is not stopped;
- git told to read no system or global config and to use no credential helper, so a ``git`` the
  target starts is not authenticated with the user's token (Git Credential Manager is set in the
  system config by Git for Windows);
- dummy AWS keys, and the shared credentials/config files pointed at a path that does not exist;
  neither config key can bring an ``AWS_*`` credential back;
- every ``*_proxy`` variable dropped and ``NO_PROXY=*``; ``PYTEST_ADDOPTS`` and ``PYTEST_PLUGINS``
  dropped;
- a best-effort network guard (``codeterrain._guard``): sockets reach loopback only, any other
  connect, send or name lookup raises. The extractor is started through a bootstrap that installs
  the guard, asserts it is active (exit 3 otherwise) and only then runs the extractor module;
- results written to a JSON file named by ``CODETERRAIN_RESULT``, never parsed from stdout.

The package is copied into a fresh per-run directory under the system temp dir (never the cache
dir, so the child is not told where the cache is) that goes first on ``PYTHONPATH``; the host's
own site-packages is never exposed to the target interpreter. The child's stdout and stderr go to
files in that directory and every read back is capped. On timeout the whole process tree is
killed; after a normal exit the processes the child left behind are killed too (a job object on
Windows, the process group on POSIX).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codeterrain.config import Config
from codeterrain.executables import resolve_executable

DUMMY_CREDENTIAL = "codeterrain-dummy"
RESULT_ENV = "CODETERRAIN_RESULT"
GUARD_MARKER = "codeterrain sandbox"
DEFAULT_TIMEOUT = 1800
#: The largest result document read back from a child.
MAX_RESULT_BYTES = 256 * 1024 * 1024
#: Seconds to wait for a killed process tree to go.
KILL_GRACE = 10

GUARD_FAILED = "codeterrain sandbox: network guard not active"
GUARD_EXIT = 3
RUN_DIR_ENV = "CODETERRAIN_RUN_DIR"
RESPECT_ADDOPTS_ENV = "CODETERRAIN_RESPECT_ADDOPTS"

SITECUSTOMIZE = """\
try:
    import codeterrain._guard as _guard

    _guard.install()
except Exception:
    pass
"""

BOOTSTRAP = r"""\
import os, sys


def _fail(reason):
    sys.stderr.write("codeterrain sandbox: network guard not active (%s)\n" % reason)
    sys.exit(3)


try:
    run_dir = os.environ["CODETERRAIN_RUN_DIR"]
    sys.path.insert(0, run_dir)
    import codeterrain._guard as guard

    here = os.path.realpath(os.path.dirname(guard.__file__))
    root = os.path.realpath(run_dir)
    try:
        inside = os.path.commonpath([here, root]) == root
    except ValueError:
        inside = False
    if not inside:
        _fail("guard module loaded from %s" % here)
    guard.install()
    guard.use_selector_event_loop()
    import socket

    if socket.socket.connect is not guard.connect:
        _fail("socket.socket.connect was not replaced")
    if not guard.proactor_guarded():
        _fail("asyncio proactor was not wrapped")
except SystemExit:
    raise
except BaseException as exc:
    _fail("%s: %s" % (type(exc).__name__, exc))

import runpy

mode, target, rest = sys.argv[1], sys.argv[2], sys.argv[3:]
if mode == "-m":
    sys.argv = [target] + rest
    runpy.run_module(target, run_name="__main__", alter_sys=True)
else:
    sys.argv = ["-c"] + rest
    exec(compile(target, "<string>", "exec"), {"__name__": "__main__"})
"""

# Always allowed through from the parent environment.
ENV_ALLOWLIST = frozenset({"PATH", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP"})
WINDOWS_ENV_ALLOWLIST = frozenset(
    {
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "USERNAME",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
    }
)
# The home and per-user data directories, pointed at an empty directory in the run dir so the
# child sees no credential store (gh, gcloud, az, git, netrc, keyring files); user-level
# ``pass_env`` can hand any of them through on purpose.
HOME_DIRS = {
    "HOME": "home",
    "USERPROFILE": "home",
    "APPDATA": "home/AppData/Roaming",
    "LOCALAPPDATA": "home/AppData/Local",
    "XDG_CONFIG_HOME": "home/.config",
    "XDG_CACHE_HOME": "home/.cache",
    "XDG_DATA_HOME": "home/.local/share",
}
# git reads no system or global config and has no credential helper, so a git the target starts
# cannot use the user's stored token. An empty ``credential.helper`` resets the helper list, and
# the command-line scope (``GIT_CONFIG_COUNT``) is read last. User-level ``pass_env`` can override.
GIT_SANDBOX_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GCM_INTERACTIVE": "never",
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "credential.helper",
    "GIT_CONFIG_VALUE_0": "",
}
# Never reach the target, whichever config key asks for them.
ENV_ALWAYS_DROPPED = frozenset({"PYTEST_ADDOPTS", "PYTEST_PLUGINS"})
WINDOWS = os.name == "nt"


class SandboxError(RuntimeError):
    """The sandboxed subprocess could not be run to completion."""


@dataclass
class SandboxResult:
    returncode: int
    stdout: str
    stderr: str
    result: Any | None


def credential_env(run_dir: Path, region: str) -> dict[str, str]:
    """The AWS environment every sandboxed process gets: dummy keys, no files, no metadata."""
    return {
        "AWS_ACCESS_KEY_ID": DUMMY_CREDENTIAL,
        "AWS_SECRET_ACCESS_KEY": DUMMY_CREDENTIAL,
        "AWS_SHARED_CREDENTIALS_FILE": str(run_dir / "no-such-credentials"),
        "AWS_CONFIG_FILE": str(run_dir / "no-such-config"),
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_DEFAULT_REGION": region,
    }


#: Shorter values are not redacted: they hold no meaningful secret, and replacing a value such as
#: ``test`` or ``1`` would corrupt every path and traceback that contains it.
MIN_REDACTED_LENGTH = 6
REDACTED = "[REDACTED]"
#: A run of this many characters or more from the start or the end of a longer value is redacted
#: too: libraries print a long value shortened (pydantic keeps both ends around ``...``).
MIN_FRAGMENT_LENGTH = 12


def _end_fragments(text: str, secret: str) -> list[tuple[int, int]]:
    """Spans of ``text`` that are a prefix or a suffix of ``secret`` of at least
    ``MIN_FRAGMENT_LENGTH`` characters, each grown as far as it keeps matching."""
    n = MIN_FRAGMENT_LENGTH
    spans = []
    start = text.find(secret[:n])
    while start != -1:
        end = start + n
        while end - start < len(secret) and end < len(text) and text[end] == secret[end - start]:
            end += 1
        spans.append((start, end))
        start = text.find(secret[:n], end)
    stop = text.find(secret[-n:])
    while stop != -1:
        begin, k = stop, len(secret) - n
        while k > 0 and begin > 0 and text[begin - 1] == secret[k - 1]:
            begin, k = begin - 1, k - 1
        spans.append((begin, stop + n))
        stop = text.find(secret[-n:], stop + 1)
    return spans


def _replace_spans(text: str, spans: list[tuple[int, int]]) -> str:
    out, last = [], 0
    for begin, end in sorted(spans):
        if end <= last:
            continue
        out.append(text[last:max(begin, last)])
        out.append(REDACTED if begin >= last else "")
        last = end
    return "".join(out) + text[last:]


def redactor(config: Config, source: dict[str, str] | None = None) -> Callable[[str], str]:
    """A function that replaces every ``import_env`` value, every ``pass_env`` value present in
    the parent environment and the dummy credential with ``[REDACTED]``, and a long value's
    leading and trailing fragments too (``MIN_FRAGMENT_LENGTH``). Child text (errors, tracebacks,
    output tails, node ids, route fields) goes through it before it is cached or printed."""
    env = os.environ if source is None else source
    values = {*config.import_env.values(), DUMMY_CREDENTIAL}
    values.update(env[name] for name in config.pass_env if name in env)
    secrets = sorted((v for v in values if len(v) >= MIN_REDACTED_LENGTH), key=len, reverse=True)

    def redact(text: str) -> str:
        for secret in secrets:
            text = text.replace(secret, REDACTED)
        spans = [
            span
            for secret in secrets
            if len(secret) > MIN_FRAGMENT_LENGTH
            for span in _end_fragments(text, secret)
        ]
        return _replace_spans(text, spans) if spans else text

    return redact


def redact_document(value: Any, redact: Callable[[str], str]) -> Any:
    """``value`` with every string in it, object keys included, passed through ``redact``; other
    types are kept. A child document can carry optional or plugin fields the core never reads."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [redact_document(v, redact) for v in value]
    if isinstance(value, dict):
        return {redact(k) if isinstance(k, str) else k: redact_document(v, redact)
                for k, v in value.items()}
    return value


def _allowed(name: str) -> bool:
    if name in ENV_ALLOWLIST:
        return True
    return WINDOWS and name.upper() in WINDOWS_ENV_ALLOWLIST


def _is_proxy(name: str) -> bool:
    return name.lower().endswith("_proxy")


def build_env(
    config: Config,
    run_dir: Path,
    result_path: Path,
    base: dict[str, str] | None = None,
) -> dict[str, str]:
    source = os.environ if base is None else base
    env = {k: v for k, v in source.items() if _allowed(k)}
    aws = credential_env(run_dir, config.region)
    env.update(aws)
    env.update(config.import_env)
    env.update({name: str(run_dir / rel) for name, rel in HOME_DIRS.items()})
    env.update(GIT_SANDBOX_ENV, GIT_CONFIG_GLOBAL=str(run_dir / "no-such-gitconfig"))
    for name in config.pass_env:
        upper = name.upper()
        if name in source and (not upper.startswith("AWS_") or upper == "AWS_DEFAULT_REGION"):
            env[name] = source[name]
    # import_env and pass_env may tune the region but can never bring real credentials back.
    env.update({k: v for k, v in aws.items() if k != "AWS_DEFAULT_REGION"})
    for name in list(env):
        if _is_proxy(name) or name.upper() in ENV_ALWAYS_DROPPED:
            del env[name]
    env.update(
        {
            "NO_PROXY": "*",
            "no_proxy": "*",
            "PYTHONPATH": os.pathsep.join(p for p in (str(run_dir), env.get("PYTHONPATH")) if p),
            "PYTHONDONTWRITEBYTECODE": "1",
            "COVERAGE_FILE": str(run_dir / ".coverage"),
            RESULT_ENV: str(result_path),
            RUN_DIR_ENV: str(run_dir),
            "CODETERRAIN_ROOT": config.root.as_posix(),
        }
    )
    if config.respect_addopts:
        env[RESPECT_ADDOPTS_ENV] = "1"
    return env


def _tail(text: str, limit: int = 4000) -> str:
    return text if len(text) <= limit else "..." + text[-limit:]


def _read_tail(path: Path, limit: int = 4000) -> str:
    """The last ``limit`` characters of a child's output file, read without loading all of it."""
    try:
        with open(path, "rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - 4 * limit))
            data = handle.read()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    return "..." + text[-limit:] if len(data) < size else _tail(text, limit)


def _warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


class _Job:
    """Windows only: a job object holding the child and everything it starts, orphans included.

    ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` makes closing it kill every process still in it. The
    child is assigned straight after it starts, before its interpreter has run any target code.
    """

    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        import ctypes
        from ctypes import wintypes

        class _Basic(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _Extended(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _Basic),
                ("IoInfo", ctypes.c_uint64 * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
        k32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD
        )
        k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        k32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self._k32 = k32
        self.handle = k32.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        info = _Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = k32.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info))
        if ok:
            ok = k32.AssignProcessToJobObject(self.handle, int(proc._handle))  # type: ignore[attr-defined]
        if not ok:
            error = ctypes.get_last_error()
            k32.CloseHandle(self.handle)
            raise OSError(error, "cannot put the child in a job object")

    def close(self) -> None:
        """Kill every process left in the job and release it."""
        if self.handle:
            self._k32.TerminateJobObject(self.handle, 1)
            self._k32.CloseHandle(self.handle)
            self.handle = None


def _new_job(proc: subprocess.Popen[bytes]) -> _Job | None:
    try:
        return _Job(proc)
    except OSError as exc:
        _warn(f"processes the sandboxed child starts may outlive it: {exc}")
        return None


def _kill_tree(proc: subprocess.Popen[bytes], job: _Job | None = None) -> None:
    """Kill the child and everything it started, then wait a bounded time for it to go.

    Windows: the job object when there is one, else ``taskkill /T /F`` by its absolute path under
    ``%SystemRoot%``. POSIX: the child leads its own session (``start_new_session``), so the whole
    process group is killed. On POSIX a grandchild that started its own session survives.
    """
    if job is not None:
        job.close()
    elif WINDOWS:
        system_root = os.environ.get("SYSTEMROOT") or r"C:\Windows"
        taskkill = Path(system_root) / "System32" / "taskkill.exe"
        try:
            subprocess.run(
                [str(taskkill), "/T", "/F", "/PID", str(proc.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=KILL_GRACE,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            _warn(f"could not kill the sandboxed process tree: {exc}")
    else:
        _kill_group(proc.pid)
    proc.kill()
    try:
        proc.wait(timeout=KILL_GRACE)
    except subprocess.TimeoutExpired:
        pass


def _kill_group(pid: int) -> None:
    """POSIX only: kill the child's process group, stragglers included; gone is fine."""
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _remove_run_dir(run_dir: Path) -> None:
    """Remove the run dir; on Windows a just-killed process can hold its files for a moment."""
    for _ in range(10):
        shutil.rmtree(run_dir, ignore_errors=True)
        if not run_dir.exists():
            return
        time.sleep(0.2)
    _warn(f"could not remove the sandbox run directory {run_dir}")


def _read_result(path: Path) -> Any | None:
    if not path.is_file():
        return None
    size = path.stat().st_size
    if size > MAX_RESULT_BYTES:
        raise SandboxError(f"result file is {size} bytes, over the {MAX_RESULT_BYTES} byte cap")
    try:
        return json.loads(path.read_bytes())
    except (ValueError, RecursionError) as exc:
        raise SandboxError(f"unreadable result file: {type(exc).__name__}: {exc}") from exc


def _stage(run_dir: Path) -> None:
    """Fill the per-run directory: a copy of this package and the guard sitecustomize."""
    package = Path(__file__).resolve().parent
    shutil.copytree(package, run_dir / package.name, ignore=shutil.ignore_patterns("__pycache__"))
    (run_dir / "sitecustomize.py").write_bytes(SITECUSTOMIZE.encode("utf-8"))
    for rel in set(HOME_DIRS.values()):
        (run_dir / rel).mkdir(parents=True, exist_ok=True)


def run_python(
    python: str | os.PathLike[str],
    args: list[str],
    config: Config,
    cache_dir: Path,
    timeout: int = DEFAULT_TIMEOUT,
) -> SandboxResult:
    """Run ``python *args`` in the target root under the sandbox and return its JSON result.

    ``args`` starts with ``-m module`` or ``-c code``; both run through the guard bootstrap. The
    run directory is a fresh ``tempfile.mkdtemp`` (``cache_dir`` is not used for it, so the child
    never learns the cache path); it is removed afterwards.
    """
    if args and args[0] not in ("-m", "-c"):
        raise SandboxError(f"unsupported interpreter arguments: {' '.join(args[:2])}")
    try:
        exe = resolve_executable(python, untrusted=config.root)
    except FileNotFoundError as exc:
        raise SandboxError(f"cannot start {python}: {exc}") from exc
    run_dir = Path(tempfile.mkdtemp(prefix="codeterrain-run-"))
    result_path = run_dir / "result.json"
    out_path, err_path = run_dir / "stdout.txt", run_dir / "stderr.txt"
    try:
        _stage(run_dir)
        with open(out_path, "wb") as out, open(err_path, "wb") as err:
            try:
                proc = subprocess.Popen(
                    [exe, "-c", BOOTSTRAP, *args],
                    cwd=config.root,
                    env=build_env(config, run_dir, result_path),
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    start_new_session=not WINDOWS,
                )
            except OSError as exc:
                raise SandboxError(f"cannot start {python}: {exc}") from exc
            job = _new_job(proc) if WINDOWS else None
            try:
                returncode = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                _kill_tree(proc, job)
                raise SandboxError(
                    f"timed out after {timeout}s: {python} {' '.join(args)}"
                ) from exc
            finally:
                if job is not None:
                    job.close()
                elif not WINDOWS:
                    _kill_group(proc.pid)
        redact = redactor(config)
        stdout, stderr = redact(_read_tail(out_path)), redact(_read_tail(err_path))
        if returncode == GUARD_EXIT and GUARD_FAILED in stderr:
            raise SandboxError(stderr.strip().splitlines()[-1])
        return SandboxResult(returncode, stdout, stderr, _read_result(result_path))
    finally:
        _remove_run_dir(run_dir)


VERSION_PROBE = (
    "import json, os, sys\n"
    "from importlib import metadata\n"
    "def v(n):\n"
    "    try:\n"
    "        return metadata.version(n)\n"
    "    except metadata.PackageNotFoundError:\n"
    "        return None\n"
    "sites = [p for p in sys.path\n"
    "         if os.path.basename(p) in ('site-packages', 'dist-packages') and os.path.isdir(p)]\n"
    "dists = sorted({n for p in sites for n in os.listdir(p) if n.endswith('.dist-info')})\n"
    "out = {'python': sys.version.split()[0], 'pytest': v('pytest'), 'fastapi': v('fastapi'),\n"
    "       'prefix': sys.prefix, 'sites': sorted(sites), 'dists': dists}\n"
    "open(os.environ['CODETERRAIN_RESULT'], 'w', encoding='utf-8').write(json.dumps(out))\n"
)


def probe_versions(
    python: str | os.PathLike[str], config: Config, cache_dir: Path
) -> dict[str, Any]:
    """Python, pytest and fastapi versions, the prefix, site dirs and installed distributions."""
    result = run_python(python, ["-c", VERSION_PROBE], config, cache_dir, timeout=60)
    if not isinstance(result.result, dict):
        raise SandboxError(f"version probe failed: {result.stderr.strip()[-300:]}")
    return result.result
