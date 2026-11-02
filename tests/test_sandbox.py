from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import time

import pytest

from codeterrain import sandbox as sandbox_module
from codeterrain.config import Config, resolve_root
from codeterrain.executables import resolve_executable
from codeterrain.sandbox import (
    DUMMY_CREDENTIAL,
    GUARD_MARKER,
    SandboxError,
    build_env,
    probe_versions,
    run_python,
)


@pytest.fixture
def config(tmp_path) -> Config:
    root = resolve_root(tmp_path / "root")
    root.mkdir()
    return Config(root=root, region="eu-central-1", import_env={"APP_MODE": "test"})


def _run(config, tmp_path, code, **kwargs):
    argv = ["-c", textwrap.dedent(code)]
    return run_python(sys.executable, argv, config, tmp_path / "cache", **kwargs)


def test_env_replaces_every_aws_variable_and_points_files_nowhere(config, tmp_path):
    base = {
        "AWS_PROFILE": "prod",
        "AWS_SESSION_TOKEN": "real-token",
        "AWS_ACCESS_KEY_ID": "AKIAREAL",
        "AWS_WEB_IDENTITY_TOKEN_FILE": "/tmp/token",
        "PATH": "p",
        "PYTHONPATH": "/inherited",
    }
    run_dir = tmp_path / "run"
    env = build_env(config, run_dir, run_dir / "result.json", base=base)
    assert env["AWS_ACCESS_KEY_ID"] == env["AWS_SECRET_ACCESS_KEY"] == DUMMY_CREDENTIAL
    assert env["AWS_EC2_METADATA_DISABLED"] == "true"
    assert env["AWS_DEFAULT_REGION"] == "eu-central-1"
    for name in ("AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE"):
        assert env[name].startswith(str(run_dir))
        assert not (tmp_path / "run" / env[name]).exists()
    assert {"AWS_PROFILE", "AWS_SESSION_TOKEN", "AWS_WEB_IDENTITY_TOKEN_FILE"}.isdisjoint(env)
    assert env["PATH"] == "p"
    assert env["APP_MODE"] == "test"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env["COVERAGE_FILE"].startswith(str(run_dir))
    assert env["PYTHONPATH"] == str(run_dir)


def test_import_env_can_tune_region_but_not_restore_real_credentials(config, tmp_path):
    hostile = Config(
        root=config.root,
        import_env={
            "AWS_ACCESS_KEY_ID": "AKIAREAL",
            "AWS_SECRET_ACCESS_KEY": "real",
            "AWS_SHARED_CREDENTIALS_FILE": "/home/me/.aws/credentials",
            "AWS_EC2_METADATA_DISABLED": "false",
            "AWS_DEFAULT_REGION": "ap-south-1",
        },
    )
    env = build_env(hostile, tmp_path, tmp_path / "r.json", base={})
    assert env["AWS_ACCESS_KEY_ID"] == env["AWS_SECRET_ACCESS_KEY"] == DUMMY_CREDENTIAL
    assert env["AWS_EC2_METADATA_DISABLED"] == "true"
    assert env["AWS_SHARED_CREDENTIALS_FILE"].startswith(str(tmp_path))
    assert env["AWS_DEFAULT_REGION"] == "ap-south-1"


def test_result_comes_from_the_json_file_not_stdout_and_run_dir_is_removed(config, tmp_path):
    out = _run(
        config,
        tmp_path,
        """
        import json, os, sys
        print("{not: json, noise}")
        open(os.environ["CODETERRAIN_RESULT"], "w").write(
            json.dumps({"cwd": os.getcwd(), "run": os.environ["CODETERRAIN_RUN_DIR"]})
        )
        """,
    )
    assert out.returncode == 0
    assert out.result["cwd"] == str(config.root)
    assert "noise" in out.stdout
    assert not os.path.exists(out.result["run"])


def test_the_run_directory_is_a_private_temp_dir_never_under_the_cache(config, tmp_path):
    # Security review P3-12: the child learned the cache path from its run dir's grandparent.
    out = _run(
        config,
        tmp_path,
        """
        import json, os, stat
        run = os.environ["CODETERRAIN_RUN_DIR"]
        mode = stat.S_IMODE(os.stat(run).st_mode)
        open(os.environ["CODETERRAIN_RESULT"], "w").write(json.dumps([run, mode]))
        """,
    )
    run, mode = out.result
    cache = os.path.realpath(tmp_path / "cache")
    assert not os.path.realpath(run).startswith(cache)
    assert os.path.realpath(run).startswith(os.path.realpath(tempfile.gettempdir()))
    if os.name != "nt":
        assert mode == 0o700


def test_the_host_site_packages_never_reach_the_target(config, tmp_path):
    out = _run(
        config,
        tmp_path,
        """
        import json, os, codeterrain
        open(os.environ["CODETERRAIN_RESULT"], "w").write(json.dumps(codeterrain.__file__))
        """,
    )
    location = out.result.replace("\\", "/")
    assert "/codeterrain-run-" in location
    assert "site-packages" not in location


def test_guard_refuses_non_loopback_connect_and_names_itself(config, tmp_path):
    out = _run(
        config,
        tmp_path,
        """
        import json, os, socket
        s = socket.socket()
        s.settimeout(2)
        try:
            s.connect(("192.0.2.1", 80))
            seen = "connected"
        except OSError as exc:
            seen = type(exc).__name__ + ": " + str(exc)
        open(os.environ["CODETERRAIN_RESULT"], "w").write(json.dumps(seen))
        """,
    )
    assert out.result.startswith("SandboxNetworkBlocked")
    assert GUARD_MARKER in out.result and "192.0.2.1:80" in out.result


@pytest.mark.parametrize("call", ["socket.getaddrinfo('example.invalid', 80)",
                                  "socket.create_connection(('203.0.113.9', 443), timeout=2)"])
def test_guard_covers_name_resolution_and_create_connection(config, tmp_path, call):
    out = _run(
        config,
        tmp_path,
        f"""
        import json, os, socket
        try:
            {call}
            seen = "allowed"
        except OSError as exc:
            seen = str(exc)
        open(os.environ["CODETERRAIN_RESULT"], "w").write(json.dumps(seen))
        """,
    )
    assert GUARD_MARKER in out.result


def test_guard_lets_loopback_through(config, tmp_path):
    server = socket.socket()
    server.settimeout(10)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    try:
        out = _run(
            config,
            tmp_path,
            f"""
            import json, os, socket
            s = socket.create_connection(("127.0.0.1", {port}), timeout=5)
            s.sendall(b"hi")
            s.close()
            open(os.environ["CODETERRAIN_RESULT"], "w").write(json.dumps("sent"))
            """,
        )
        assert out.result == "sent", out.stderr
        conn, _ = server.accept()
        assert conn.recv(2) == b"hi"
        conn.close()
    finally:
        server.close()
    assert out.result == "sent"


def test_boto3_never_sees_real_credentials_and_cannot_reach_the_network(
    config, tmp_path, monkeypatch
):
    pytest.importorskip("boto3")
    home = tmp_path / "home"
    (home / ".aws").mkdir(parents=True)
    (home / ".aws" / "credentials").write_text(
        "[default]\naws_access_key_id = AKIAREALREALREAL\naws_secret_access_key = real\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("AWS_PROFILE", "default")
    out = _run(
        config,
        tmp_path,
        """
        import json, os, traceback
        import boto3
        creds = boto3.Session().get_credentials()
        client = boto3.client("sts", region_name="us-east-1")
        try:
            client.get_caller_identity()
            error = "reached the network"
        except Exception as exc:
            error = "".join(traceback.format_exception(exc))
        result = {"key": creds.access_key, "method": creds.method, "error": error}
        open(os.environ["CODETERRAIN_RESULT"], "w").write(json.dumps(result))
        """,
        timeout=120,
    )
    assert out.result["key"] == DUMMY_CREDENTIAL
    assert out.result["method"] == "env"
    assert GUARD_MARKER in out.result["error"]
    assert "AKIAREAL" not in out.stdout + out.stderr


def test_probe_versions_reports_the_target_interpreter(config, tmp_path):
    found = probe_versions(sys.executable, config, tmp_path / "cache")
    assert found["python"].startswith(f"{sys.version_info.major}.{sys.version_info.minor}")
    assert found["pytest"]


def test_unstartable_interpreter_and_timeout_raise_sandbox_error(config, tmp_path):
    with pytest.raises(SandboxError, match="cannot start"):
        run_python(tmp_path / "nope.exe", [], config, tmp_path / "cache")
    with pytest.raises(SandboxError, match="timed out"):
        _run(config, tmp_path, "import time; time.sleep(30)", timeout=1)


def test_the_home_and_user_data_dirs_are_empty_dirs_in_the_run_dir(config, tmp_path, monkeypatch):
    # Security review P2-4: %APPDATA%\GitHub CLI\hosts.yml (a gh token) was readable in the child.
    home = tmp_path / "parent-home"
    for hosts in (home / "AppData" / "Roaming" / "GitHub CLI", home / ".config" / "gh"):
        hosts.mkdir(parents=True)
        (hosts / "hosts.yml").write_text("github.com:\n  oauth_token: CANARY-gh\n", "utf-8")
    for name, rel in (("HOME", ""), ("USERPROFILE", ""), ("APPDATA", "AppData/Roaming"),
                      ("LOCALAPPDATA", "AppData/Local"), ("XDG_CONFIG_HOME", ".config")):
        monkeypatch.setenv(name, str(home / rel))
    code = """
        import json, os
        run = os.environ["CODETERRAIN_RUN_DIR"]
        names = ["HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME",
                 "XDG_CACHE_HOME", "XDG_DATA_HOME"]
        found = []
        for base, dirs, files in os.walk(os.path.expanduser("~")):
            found += files
        out = {"home": os.path.expanduser("~"), "run": run, "files": found,
               "inside": all(os.environ[n].startswith(run) for n in names),
               "exist": all(os.path.isdir(os.environ[n]) for n in names)}
        open(os.environ["CODETERRAIN_RESULT"], "w").write(json.dumps(out))
        """
    out = _run(config, tmp_path, code).result
    assert out["inside"] and out["exist"] and out["files"] == []
    assert os.path.realpath(out["home"]).startswith(os.path.realpath(out["run"]))
    opted = Config(root=config.root, pass_env=("HOME", "USERPROFILE"))
    out = _run(opted, tmp_path, code).result
    assert os.path.realpath(out["home"]) == os.path.realpath(home)


def _alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True
        ).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        waited, _ = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass
    else:
        return waited == 0
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as handle:
            return handle.read().split()[2] != "Z"
    except (FileNotFoundError, ProcessLookupError):
        return False  # reaped since the check above


def test_a_timeout_kills_the_whole_tree_and_returns_promptly(config, tmp_path):
    # Security review P2-3: a grandchild holding the pipes kept terrain waiting 42 s for a 5 s
    # timeout (forever if it never exits), and survived as an orphan running target code.
    pid_file = tmp_path / "grandchild.pid"
    code = f"""
        import subprocess, sys, time
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        open({str(pid_file)!r}, "w").write(str(child.pid))
        time.sleep(120)
        """
    started = time.monotonic()
    with pytest.raises(SandboxError, match="timed out after 5s"):
        _run(config, tmp_path, code, timeout=5)
    assert time.monotonic() - started < 15
    grandchild = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while _alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not _alive(grandchild)


def test_child_output_is_read_back_as_a_bounded_tail(config, tmp_path):
    # Security review P3-9: all child output was buffered in the parent.
    out = _run(config, tmp_path, "import sys; sys.stdout.write('x' * 3_000_000 + 'END')")
    assert out.stdout.startswith("...") and out.stdout.endswith("END")
    assert len(out.stdout) <= 4003


@pytest.mark.parametrize(
    ("payload", "message"),
    [("'[' * 100000 + ']' * 100000", "RecursionError"), ("'{not json'", "unreadable result")],
    ids=["deep", "malformed"],
)
def test_a_deep_or_malformed_result_is_a_clean_sandbox_error(config, tmp_path, payload, message):
    # Security review P3-2: a deeply nested result.json raised an uncaught RecursionError.
    code = f"import os; open(os.environ['CODETERRAIN_RESULT'], 'w').write({payload})"
    with pytest.raises(SandboxError, match=message):
        _run(config, tmp_path, code)


def test_an_oversized_result_is_refused_before_it_is_read(config, tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox_module, "MAX_RESULT_BYTES", 1000)
    code = "import os; open(os.environ['CODETERRAIN_RESULT'], 'w').write('[' + '1,' * 1000 + '1]')"
    with pytest.raises(SandboxError, match="over the 1000 byte cap"):
        _run(config, tmp_path, code)


def _outcomes(config, tmp_path, code):
    out = _run(config, tmp_path, code, timeout=120)
    assert out.result is not None, out.stderr
    return out.result


def test_env_is_an_allowlist_the_parent_secrets_and_proxies_do_not_pass(config, tmp_path):
    base = {
        "PATH": "p",
        "HOME": "/home/me",
        "LANG": "C",
        "LC_ALL": "C",
        "TMPDIR": "/t",
        "TEMP": "/t",
        "TMP": "/t",
        "CT_FAKE_SECRET": "hunter2",
        "GITHUB_TOKEN": "ghp_x",
        "PYTHONPATH": "/inherited",
        "VIRTUAL_ENV": "/venv",
        "HTTPS_PROXY": "http://proxy:3128",
        "http_proxy": "http://proxy:3128",
        "ALL_PROXY": "socks5://proxy",
        "NO_PROXY": "internal",
        "PYTEST_ADDOPTS": "--junitxml=out.xml",
        "PYTEST_PLUGINS": "evil",
        "SYSTEMROOT": "C:/Windows",
    }
    env = build_env(config, tmp_path / "run", tmp_path / "r.json", base=base)
    for kept in ("PATH", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP"):
        assert env[kept] == base[kept]
    assert env["HOME"] == str(tmp_path / "run" / "home")
    absent = {"CT_FAKE_SECRET", "GITHUB_TOKEN", "VIRTUAL_ENV", "PYTEST_ADDOPTS", "PYTEST_PLUGINS"}
    assert absent.isdisjoint(env)
    assert {k for k in env if k.lower().endswith("_proxy")} == {"NO_PROXY", "no_proxy"}
    assert env["NO_PROXY"] == env["no_proxy"] == "*"
    assert env["PYTHONPATH"] == str(tmp_path / "run")
    assert env["CODETERRAIN_ROOT"] == config.root.as_posix()
    assert ("SYSTEMROOT" in env) is (os.name == "nt")


def test_windows_basics_pass_case_insensitively_only_on_windows(config, tmp_path, monkeypatch):
    base = {"SystemRoot": "C:/Windows", "ComSpec": "cmd.exe", "WINDIR": "C:/Windows", "OTHER": "x"}
    monkeypatch.setattr("codeterrain.sandbox.WINDOWS", True)
    env = build_env(config, tmp_path / "run", tmp_path / "r.json", base=base)
    assert {"SystemRoot", "ComSpec", "WINDIR"} <= set(env) and "OTHER" not in env
    monkeypatch.setattr("codeterrain.sandbox.WINDOWS", False)
    env = build_env(config, tmp_path / "run", tmp_path / "r.json", base=base)
    assert {"SystemRoot", "ComSpec", "WINDIR", "OTHER"}.isdisjoint(env)


def test_pass_env_is_opt_in_and_never_restores_credentials_proxies_or_pytest_options(
    config, tmp_path
):
    base = {
        "PATH": "p",
        "CT_FAKE_SECRET": "hunter2",
        "HTTPS_PROXY": "http://proxy:3128",
        "all_proxy": "socks5://proxy",
        "PYTEST_ADDOPTS": "-x",
        "AWS_ACCESS_KEY_ID": "AKIAREAL",
        "AWS_SECRET_ACCESS_KEY": "real",
        "AWS_PROFILE": "prod",
        "AWS_CONFIG_FILE": "/home/me/.aws/config",
        "AWS_DEFAULT_REGION": "ap-south-1",
    }
    wanted = Config(root=config.root, pass_env=tuple(sorted(base)))
    env = build_env(wanted, tmp_path / "run", tmp_path / "r.json", base=base)
    assert env["CT_FAKE_SECRET"] == "hunter2"
    assert env["AWS_ACCESS_KEY_ID"] == env["AWS_SECRET_ACCESS_KEY"] == DUMMY_CREDENTIAL
    assert env["AWS_CONFIG_FILE"].startswith(str(tmp_path / "run"))
    assert "AWS_PROFILE" not in env
    assert env["AWS_DEFAULT_REGION"] == "ap-south-1"
    assert {"HTTPS_PROXY", "all_proxy", "PYTEST_ADDOPTS"}.isdisjoint(env)
    assert env["NO_PROXY"] == "*"
    assert "CT_FAKE_SECRET" not in build_env(config, tmp_path / "run", tmp_path / "r.json", base)


def test_import_env_cannot_bring_back_proxies_or_pytest_options(config, tmp_path):
    hostile = Config(
        root=config.root,
        import_env={"HTTP_PROXY": "http://p", "PYTEST_ADDOPTS": "-x", "PYTEST_PLUGINS": "x"},
    )
    env = build_env(hostile, tmp_path / "run", tmp_path / "r.json", base={})
    assert {"HTTP_PROXY", "PYTEST_ADDOPTS", "PYTEST_PLUGINS"}.isdisjoint(env)


def test_respect_addopts_is_signalled_to_the_extractor_only_when_asked(config, tmp_path):
    off = build_env(config, tmp_path / "run", tmp_path / "r.json", base={})
    on = build_env(
        Config(root=config.root, respect_addopts=True), tmp_path / "run", tmp_path / "r.json", {}
    )
    assert "CODETERRAIN_RESPECT_ADDOPTS" not in off and on["CODETERRAIN_RESPECT_ADDOPTS"] == "1"


PROBE_ENV = """
import json, os
names = ["HTTPS_PROXY", "https_proxy", "CT_FAKE_SECRET", "PYTEST_ADDOPTS", "NO_PROXY",
         "CODETERRAIN_ROOT", "PATH"]
open(os.environ["CODETERRAIN_RESULT"], "w").write(
    json.dumps({n: os.environ.get(n) for n in names})
)
"""


def test_a_child_sees_none_of_the_planted_parent_secrets(config, tmp_path, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:3128")
    monkeypatch.setenv("CT_FAKE_SECRET", "hunter2")
    monkeypatch.setenv("PYTEST_ADDOPTS", "--junitxml=leak.xml")
    seen = _run(config, tmp_path, PROBE_ENV).result
    assert seen["HTTPS_PROXY"] is None and seen["https_proxy"] is None
    assert seen["CT_FAKE_SECRET"] is None and seen["PYTEST_ADDOPTS"] is None
    assert seen["NO_PROXY"] == "*"
    assert seen["CODETERRAIN_ROOT"] == config.root.as_posix()
    assert seen["PATH"]


def test_pass_env_reaches_the_child_but_a_listed_proxy_still_does_not(
    config, tmp_path, monkeypatch
):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:3128")
    monkeypatch.setenv("CT_FAKE_SECRET", "hunter2")
    opted = Config(root=config.root, pass_env=("CT_FAKE_SECRET", "HTTPS_PROXY"))
    seen = _run(opted, tmp_path, PROBE_ENV).result
    assert seen["CT_FAKE_SECRET"] == "hunter2"
    assert seen["HTTPS_PROXY"] is None


GUARD_ERROR = "SandboxNetworkBlocked"
HOSTS = {"v4": "192.0.2.1", "v6": "2001:db8::1", "mapped": "::ffff:192.0.2.1"}
LOOPBACKS = {
    "lo4": "127.0.0.1",
    "lo6": "::1",
    "lo-name": "localhost",
    "lo-mapped": "::ffff:127.0.0.1",
}
CALLS = ("connect", "connect_ex", "sendto", "sendto_flags", "sendmsg", "create_connection", "raw")

MATRIX = """
import json, os, socket
import _socket

HOSTS = %(hosts)r
LOOPBACKS = %(loopbacks)r
out = {}


def family(host):
    return socket.AF_INET6 if ":" in host else socket.AF_INET


def target(host):
    return (host, 9, 0, 0) if ":" in host else (host, 9)


def make(host, kind=socket.SOCK_STREAM, factory=socket.socket):
    s = factory(family(host), kind)
    s.settimeout(0.3)
    return s


def do(call, host):
    addr = target(host)
    if call == "connect":
        make(host).connect(addr)
    elif call == "connect_ex":
        make(host).connect_ex(addr)
    elif call == "sendto":
        make(host, socket.SOCK_DGRAM).sendto(b"x", addr)
    elif call == "sendto_flags":
        make(host, socket.SOCK_DGRAM).sendto(b"x", 0, addr)
    elif call == "sendmsg":
        make(host, socket.SOCK_DGRAM).sendmsg([b"x"], [], 0, addr)
    elif call == "create_connection":
        socket.create_connection(addr[:2], timeout=0.3)
    elif call == "raw":
        make(host, factory=_socket.socket).connect(addr)


for call in %(calls)r:
    if call == "sendmsg" and not hasattr(socket.socket, "sendmsg"):
        continue
    for label, host in list(HOSTS.items()) + list(LOOPBACKS.items()):
        try:
            do(call, host)
            result = "ok"
        except Exception as exc:
            result = type(exc).__name__ + ": " + str(exc)
        out[call + "|" + label] = result
open(os.environ["CODETERRAIN_RESULT"], "w").write(json.dumps(out))
"""


@pytest.fixture(scope="module")
def matrix(tmp_path_factory):
    base = tmp_path_factory.mktemp("matrix")
    root = resolve_root(base / "root")
    root.mkdir()
    code = MATRIX % {"hosts": HOSTS, "loopbacks": LOOPBACKS, "calls": CALLS}
    out = run_python(sys.executable, ["-c", code], Config(root=root), base / "cache", timeout=300)
    assert out.result is not None, out.stderr
    return out.result


@pytest.mark.parametrize("label", sorted(HOSTS))
@pytest.mark.parametrize("call", CALLS)
def test_every_outbound_call_to_a_non_loopback_address_raises_the_guard_error(
    matrix, call, label
):
    if f"{call}|{label}" not in matrix:
        pytest.skip(f"{call} does not exist on this platform")
    assert matrix[f"{call}|{label}"].startswith(GUARD_ERROR)
    assert GUARD_MARKER in matrix[f"{call}|{label}"]


@pytest.mark.parametrize("label", sorted(LOOPBACKS))
@pytest.mark.parametrize("call", CALLS)
def test_loopback_addresses_are_never_refused_by_the_guard(matrix, call, label):
    if f"{call}|{label}" not in matrix:
        pytest.skip(f"{call} does not exist on this platform")
    assert GUARD_ERROR not in matrix[f"{call}|{label}"]


DNS_CALLS = (
    "socket.getaddrinfo('example.com', 80)",
    "socket.gethostbyname('example.com')",
    "socket.gethostbyname_ex('example.com')",
    "socket.gethostbyaddr('example.com')",
    "socket.getnameinfo(('example.com', 80), 0)",
    "_socket.getaddrinfo('example.com', 80)",
    "_socket.gethostbyname('example.com')",
)


@pytest.fixture(scope="module")
def dns(tmp_path_factory):
    base = tmp_path_factory.mktemp("dns")
    root = resolve_root(base / "root")
    root.mkdir()
    lines = ["import json, os, socket", "import _socket", "out = {}"]
    for call in (*DNS_CALLS, "socket.getaddrinfo('localhost', 80)"):
        label = "loopback" if "localhost" in call else call
        lines += [
            "try:",
            f"    {call}",
            f"    out[{label!r}] = 'ok'",
            "except Exception as exc:",
            f"    out[{label!r}] = type(exc).__name__",
        ]
    lines.append("open(os.environ['CODETERRAIN_RESULT'], 'w').write(json.dumps(out))")
    code = "\n".join(lines)
    out = run_python(sys.executable, ["-c", code], Config(root=root), base / "cache", timeout=120)
    assert out.result is not None, out.stderr
    return out.result


@pytest.mark.parametrize("call", DNS_CALLS)
def test_name_resolution_of_a_remote_name_raises_the_guard_error(dns, call):
    assert dns[call] == GUARD_ERROR


def test_resolving_localhost_is_still_allowed(dns):
    assert dns["loopback"] == "ok"


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="no AF_UNIX on this platform")
def test_unix_domain_connects_are_refused_with_the_same_error(config, tmp_path):
    seen = _outcomes(
        config,
        tmp_path,
        """
        import json, os, socket
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect("/tmp/codeterrain-no-such.sock")
            seen = "connected"
        except OSError as exc:
            seen = type(exc).__name__ + ": " + str(exc)
        open(os.environ["CODETERRAIN_RESULT"], "w").write(json.dumps(seen))
        """,
    )
    assert seen.startswith(GUARD_ERROR) and GUARD_MARKER in seen


NOT_GUARDED = {
    "accept": "server side of a listening socket; originates no outbound traffic",
    "bind": "claims a local address; originates no outbound traffic",
    "listen": "server side of a bound socket; originates no outbound traffic",
    "recv": "reads from a socket whose connect was already checked",
    "recv_into": "reads from a socket whose connect was already checked",
    "recvfrom": "reads from a socket whose connect or bind was already checked",
    "recvfrom_into": "reads from a socket whose connect or bind was already checked",
    "recvmsg": "reads from a socket whose connect or bind was already checked",
    "recvmsg_into": "reads from a socket whose connect or bind was already checked",
    "send": "writes to a socket whose connect was already checked",
    "sendall": "writes to a socket whose connect was already checked",
    "sendfile": "writes to a socket whose connect was already checked",
    "sendmsg_afalg": "AF_ALG kernel crypto sockets; local only",
    "send_fds": "passes descriptors over an already-connected AF_UNIX socket (connect is refused)",
    "recv_fds": "reads descriptors from an already-connected AF_UNIX socket",
    "getfqdn": "built on the guarded gethostbyaddr; swallows its OSError and returns the name",
    "getprotobyname": "reads the local protocol database; no traffic",
    "getservbyname": "reads the local service database; no traffic",
    "getservbyport": "reads the local service database; no traffic",
}

CLASS_PROBE = """
import json, os, socket
import _socket

seen = {}
for owner_name, owner in (("socket", socket), ("_socket", _socket)):
    for name in dir(owner):
        cls = getattr(owner, name)
        if isinstance(cls, type) and hasattr(cls, "connect") and hasattr(cls, "sendto"):
            seen[owner_name + "." + name] = [
                getattr(getattr(cls, method), "__module__", None) == "codeterrain._guard"
                for method in ("connect", "connect_ex", "sendto")
            ]
open(os.environ["CODETERRAIN_RESULT"], "w").write(json.dumps(seen))
"""

REGISTRY_PROBE = """
import json, os, re, socket
import _socket

pattern = re.compile(
    "connect|send|recv|accept|getaddrinfo|gethostby|getnameinfo|getfqdn|getservby|getprotoby"
    "|bind|listen"
)
seen = {}
owners = (("socket", socket), ("_socket", _socket), ("socket.socket", socket.socket))
for owner_name, owner in owners:
    for name in dir(owner):
        if name.startswith("_") or not pattern.search(name):
            continue
        fn = getattr(owner, name)
        if callable(fn) and not isinstance(fn, type):
            seen[owner_name + "." + name] = getattr(fn, "__module__", None) == "codeterrain._guard"
open(os.environ["CODETERRAIN_RESULT"], "w").write(json.dumps(seen))
"""


def test_guard_entry_points_are_enumerated(config, tmp_path):
    from codeterrain._guard import GUARDED_FUNCTIONS, GUARDED_METHODS

    seen = _outcomes(config, tmp_path, REGISTRY_PROBE)
    assert seen, "the probe found no socket entry points at all"
    unpatched = {key.rsplit(".", 1)[1] for key, patched in seen.items() if not patched}
    assert unpatched - set(NOT_GUARDED) == set(), (
        "a network-reaching socket function is neither guarded nor listed in NOT_GUARDED"
    )
    patched = {key.rsplit(".", 1)[1] for key, ok in seen.items() if ok}
    for name in (*GUARDED_METHODS, *GUARDED_FUNCTIONS):
        if name == "sendmsg" and not hasattr(socket.socket, "sendmsg"):
            continue
        assert name in patched, f"{name} is declared guarded but was not replaced"
    assert all(reason for reason in NOT_GUARDED.values())
    assert patched.isdisjoint(NOT_GUARDED)


@pytest.mark.skipif(sys.platform != "win32", reason="the proactor loop is Windows-only")
def test_windows_asyncio_connects_go_through_the_guard(config, tmp_path):
    seen = _outcomes(
        config,
        tmp_path,
        """
        import asyncio, json, os

        async def go():
            try:
                await asyncio.wait_for(asyncio.open_connection("192.0.2.1", 9), 3)
                return "connected"
            except Exception as exc:
                return type(exc).__name__ + ": " + str(exc)

        open(os.environ["CODETERRAIN_RESULT"], "w").write(json.dumps(asyncio.run(go())))
        """,
    )
    assert seen.startswith(GUARD_ERROR)


def test_the_bootstrap_installs_the_guard_before_the_module_runs_and_asserts_it(
    config, tmp_path
):
    seen = _outcomes(
        config,
        tmp_path,
        """
        import json, os, socket, sys
        open(os.environ["CODETERRAIN_RESULT"], "w").write(
            json.dumps([socket.socket.connect.__module__, sys.argv])
        )
        """,
    )
    assert seen == ["codeterrain._guard", ["-c"]]


def test_m_runs_a_module_with_its_arguments_through_the_same_bootstrap(config, tmp_path):
    out = run_python(sys.executable, ["-m", "json.tool", "--help"], config, tmp_path / "cache")
    assert out.returncode == 0 and "usage:" in out.stdout


def test_a_missing_guard_module_is_a_sandbox_error_not_a_silent_unguarded_run(
    config, tmp_path, monkeypatch
):
    from codeterrain import sandbox

    real_stage = sandbox._stage

    def stage_without_the_guard(run_dir):
        real_stage(run_dir)
        (run_dir / "codeterrain" / "_guard.py").unlink()
        (run_dir / "sitecustomize.py").unlink()

    monkeypatch.setattr(sandbox, "_stage", stage_without_the_guard)
    with pytest.raises(SandboxError, match="network guard not active"):
        _run(config, tmp_path, "open('ran-unguarded', 'w').write('x')")
    assert not (config.root / "ran-unguarded").exists()


def test_exit_status_3_without_the_guard_message_is_not_mistaken_for_a_missing_guard(
    config, tmp_path
):
    out = _run(config, tmp_path, "import sys; sys.exit(3)")
    assert out.returncode == 3


def test_every_socket_class_alias_carries_the_guarded_methods(config, tmp_path):
    seen = _outcomes(config, tmp_path, CLASS_PROBE)
    aliases = {"socket.socket", "socket.SocketType", "_socket.socket", "_socket.SocketType"}
    assert aliases <= set(seen)
    assert all(all(flags) for flags in seen.values()), seen


PROACTOR_CONNECT = """
import asyncio, json, os, socket

loop = asyncio.ProactorEventLoop()
sock = socket.socket()
sock.setblocking(False)
try:
    loop.run_until_complete(loop.sock_connect(sock, ("192.0.2.1", 9)))
    seen = "connected"
except Exception as exc:
    seen = type(exc).__name__ + ": " + str(exc)
finally:
    sock.close()
    loop.close()
open(os.environ["CODETERRAIN_RESULT"], "w").write(json.dumps(seen))
"""

PROACTOR_POLICY = """
import asyncio, json, os, warnings

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


async def go():
    try:
        await asyncio.wait_for(asyncio.open_connection("192.0.2.1", 9), 3)
        return "connected"
    except Exception as exc:
        return type(exc).__name__ + ": " + str(exc)

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    seen = asyncio.run(go())
open(os.environ["CODETERRAIN_RESULT"], "w").write(json.dumps(seen))
"""

PROACTOR_FACTORY = """
import asyncio, json, os


async def go():
    try:
        await asyncio.wait_for(asyncio.open_connection("192.0.2.1", 9), 3)
        return "connected"
    except Exception as exc:
        return type(exc).__name__ + ": " + str(exc)

seen = asyncio.run(go(), loop_factory=asyncio.ProactorEventLoop)
open(os.environ["CODETERRAIN_RESULT"], "w").write(json.dumps(seen))
"""


@pytest.mark.skipif(sys.platform != "win32", reason="the proactor loop is Windows-only")
@pytest.mark.parametrize(
    "code", [PROACTOR_CONNECT, PROACTOR_POLICY, PROACTOR_FACTORY],
    ids=["explicit-loop", "policy-switched-back", "loop-factory"],
)
def test_the_windows_proactor_is_guarded_however_a_target_selects_it(config, tmp_path, code):
    assert _outcomes(config, tmp_path, code).startswith(GUARD_ERROR)


PROACTOR_SENDTO = """
import asyncio, json, os, socket

loop = asyncio.ProactorEventLoop()
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setblocking(False)
try:
    loop.run_until_complete(loop._proactor.sendto(sock, b"x", 0, ("192.0.2.1", 9)))
    seen = "sent"
except Exception as exc:
    seen = type(exc).__name__ + ": " + str(exc)
finally:
    sock.close()
    loop.close()
open(os.environ["CODETERRAIN_RESULT"], "w").write(json.dumps(seen))
"""


@pytest.mark.skipif(sys.platform != "win32", reason="the proactor loop is Windows-only")
def test_the_windows_proactor_refuses_datagrams_to_a_remote_address(config, tmp_path):
    assert _outcomes(config, tmp_path, PROACTOR_SENDTO).startswith(GUARD_ERROR)


def _stage_without_sitecustomize(monkeypatch, patch=None):
    """Stage the run dir without sitecustomize, so only the bootstrap can install the guard."""
    from codeterrain import sandbox

    real_stage = sandbox._stage

    def stage(run_dir):
        real_stage(run_dir)
        (run_dir / "sitecustomize.py").unlink()
        if patch is not None:
            patch(run_dir)

    monkeypatch.setattr(sandbox, "_stage", stage)


def test_the_bootstrap_installs_the_guard_by_itself_without_sitecustomize(
    config, tmp_path, monkeypatch
):
    _stage_without_sitecustomize(monkeypatch)
    seen = _outcomes(
        config,
        tmp_path,
        """
        import json, os, socket
        run_dir = os.environ["CODETERRAIN_RUN_DIR"]
        open(os.environ["CODETERRAIN_RESULT"], "w").write(
            json.dumps([
                os.path.exists(os.path.join(run_dir, "sitecustomize.py")),
                socket.socket.connect.__module__,
            ])
        )
        """,
    )
    assert seen == [False, "codeterrain._guard"]


def _append_to_guard(text):
    def patch(run_dir):
        guard = run_dir / "codeterrain" / "_guard.py"
        guard.write_bytes(guard.read_bytes() + text.encode("utf-8"))

    return patch


def test_a_guard_whose_install_does_nothing_is_a_sandbox_error(config, tmp_path, monkeypatch):
    _stage_without_sitecustomize(
        monkeypatch, _append_to_guard("\n\ndef install():\n    return None\n")
    )
    with pytest.raises(SandboxError, match="was not replaced"):
        _run(config, tmp_path, "open('ran-unguarded', 'w').write('x')")
    assert not (config.root / "ran-unguarded").exists()


def test_a_guard_that_reports_the_proactor_unwrapped_is_a_sandbox_error(
    config, tmp_path, monkeypatch
):
    _stage_without_sitecustomize(
        monkeypatch, _append_to_guard("\n\ndef proactor_guarded():\n    return False\n")
    )
    with pytest.raises(SandboxError, match="proactor was not wrapped"):
        _run(config, tmp_path, "open('ran-unguarded', 'w').write('x')")
    assert not (config.root / "ran-unguarded").exists()


def test_a_guard_loaded_from_outside_the_run_directory_is_refused_even_by_name_prefix(
    config, tmp_path, monkeypatch
):
    import shutil
    from pathlib import Path

    import codeterrain
    from codeterrain import sandbox

    real_stage = sandbox._stage
    decoys = []

    def stage(run_dir):
        real_stage(run_dir)
        # A sibling whose path starts with the run directory's path: a startswith check accepts it.
        decoy = Path(str(run_dir) + "-decoy")
        shutil.copytree(
            Path(codeterrain.__file__).parent, decoy / "codeterrain",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        decoys.append(decoy)
        (run_dir / "sitecustomize.py").write_text(
            f"import sys\nsys.path.insert(0, {str(decoy)!r})\nimport codeterrain._guard\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(sandbox, "_stage", stage)
    try:
        with pytest.raises(SandboxError, match="guard module loaded from"):
            _run(config, tmp_path, "open('ran-unguarded', 'w').write('x')")
    finally:
        for decoy in decoys:
            shutil.rmtree(decoy, ignore_errors=True)
    assert not (config.root / "ran-unguarded").exists()


def test_connecting_to_the_empty_host_is_not_a_remote_connect(config, tmp_path):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        seen = _outcomes(
            config,
            tmp_path,
            f"""
            import json, os, socket
            s = socket.socket()
            s.settimeout(2)
            try:
                s.connect(("", {server.getsockname()[1]}))
                seen = "connected"
            except OSError as exc:
                seen = type(exc).__name__ + ": " + str(exc)
            open(os.environ["CODETERRAIN_RESULT"], "w").write(json.dumps(seen))
            """,
        )
    finally:
        server.close()
    assert GUARD_MARKER not in seen


def test_import_env_and_pass_env_extend_pythonpath_behind_the_run_directory(config, tmp_path):
    run = tmp_path / "run"
    through_import_env = Config(root=config.root, import_env={"PYTHONPATH": "/extra"})
    env = build_env(through_import_env, run, run / "r.json", base={})
    assert env["PYTHONPATH"] == os.pathsep.join([str(run), "/extra"])
    through_pass_env = Config(root=config.root, pass_env=("PYTHONPATH",))
    env = build_env(through_pass_env, run, run / "r.json", base={"PYTHONPATH": "/inherited"})
    assert env["PYTHONPATH"] == os.pathsep.join([str(run), "/inherited"])


@pytest.mark.parametrize("args", [["--version"], ["-X", "dev"], ["script.py"], ["-I", "-c", "1"]])
def test_only_dash_m_and_dash_c_may_start_the_interpreter(config, tmp_path, args):
    with pytest.raises(SandboxError, match="unsupported interpreter arguments"):
        run_python(sys.executable, args, config, tmp_path / "cache")


def test_the_allowlists_are_exactly_these_names():
    from codeterrain import sandbox

    assert sandbox.ENV_ALLOWLIST == {"PATH", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP"}
    assert sandbox.WINDOWS_ENV_ALLOWLIST == {
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


def test_each_windows_basic_reaches_the_child_in_whatever_case_windows_spells_it(
    config, tmp_path, monkeypatch
):
    from codeterrain import sandbox

    base = {name.title(): f"v-{name}" for name in sandbox.WINDOWS_ENV_ALLOWLIST}
    base["ProgramFiles(x86)"] = "C:/Program Files (x86)"
    base["UNLISTED"] = "x"
    monkeypatch.setattr(sandbox, "WINDOWS", True)
    env = build_env(config, tmp_path / "run", tmp_path / "r.json", base=base)
    assert set(base) - {"UNLISTED"} <= set(env) and "UNLISTED" not in env


def test_the_redactor_hides_env_values_and_the_dummy_but_not_short_values(config):
    opted = Config(
        root=config.root, import_env={"MODE": "test", "KEY": "CANARY-import"}, pass_env=("DB",)
    )
    redact = sandbox_module.redactor(opted, {"DB": "CANARY-db-password", "OTHER": "CANARY-x"})
    text = "test CANARY-import CANARY-db-password codeterrain-dummy CANARY-x"
    assert redact(text) == "test [REDACTED] [REDACTED] [REDACTED] CANARY-x"


def test_child_output_tails_are_redacted(config, tmp_path):
    opted = Config(root=config.root, import_env={"API_TOKEN": "CANARY-tok-123"})
    code = (
        "import os, sys; print(os.environ['API_TOKEN']); sys.stderr.write(os.environ['API_TOKEN'])"
    )
    out = _run(opted, tmp_path, code)
    assert "CANARY" not in out.stdout + out.stderr and "[REDACTED]" in out.stdout


def test_a_long_value_printed_shortened_is_redacted_at_both_ends(config):
    # Security review r2 P3-2: pydantic prints a string over 50 characters as its first and last
    # characters around "...", which the exact-match redactor missed.
    value = "postgres://u:CANARY-dbpw-0123456789abcdefghij@db.example.internal/x"
    redact = sandbox_module.redactor(
        Config(root=config.root, pass_env=("DATABASE_URL",)), {"DATABASE_URL": value}
    )
    shown = f"input_value='{value[:24]}...{value[-23:]}', input_type=str] postgres://localhost"
    assert redact(shown) == (
        "input_value='[REDACTED]...[REDACTED]', input_type=str] postgres://localhost"
    )
    assert redact(f"<{value}>") == "<[REDACTED]>"


def test_git_in_the_child_reads_no_user_config_and_has_no_credential_helper(config, tmp_path):
    # Security review r2 P2-1: Git for Windows sets credential.helper=manager in the system
    # config, outside the redirected home, so `git credential fill` in the child returned the
    # user's stored token.
    try:
        git = resolve_executable("git")
    except FileNotFoundError:
        pytest.skip("git is not installed")
    code = f"""
        import os, subprocess
        from pathlib import Path
        Path(os.environ["HOME"], ".gitconfig").write_text("[credential]\\n\\thelper = store\\n")
        out = subprocess.run([{git!r}, "config", "--get-all", "credential.helper"],
                             capture_output=True, text=True, cwd=os.environ["HOME"])
        print(repr([line for line in out.stdout.splitlines() if line.strip()]))
        """
    out = _run(config, tmp_path, code)
    assert out.stdout.strip() == "[]", out.stdout + out.stderr
    env = build_env(config, tmp_path / "run", tmp_path / "r.json", base={})
    assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GCM_INTERACTIVE"] == "never"
    assert env["GIT_CONFIG_GLOBAL"].startswith(str(tmp_path / "run"))


def test_processes_left_behind_after_a_normal_exit_are_killed_and_the_run_dir_goes(
    config, tmp_path
):
    # Security review r2 P3-9: on Windows a grandchild outlived a normal exit, kept running target
    # code, and held the run dir (with the raw child output) open so it was never removed.
    pid_file = tmp_path / "grandchild.txt"
    code = f"""
        import os, subprocess, sys
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        open({str(pid_file)!r}, "w").write(f"{{child.pid}}\\n{{os.environ['CODETERRAIN_RUN_DIR']}}")
        """
    started = time.monotonic()
    _run(config, tmp_path, code)
    assert time.monotonic() - started < 30
    pid, run_dir = pid_file.read_text().splitlines()
    deadline = time.monotonic() + 5
    while _alive(int(pid)) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not _alive(int(pid))
    assert not os.path.exists(run_dir)


#: Modules that run inside the target's interpreter, which may be older than codeterrain's own.
CHILD_MODULES = (
    "_guard.py",
    "_pytest_dump_plugin.py",
    "_routes_runner.py",
    "hookspecs.py",
    "plugins/fastapi_routes.py",
)
CHILD_PYTHON_FLOOR = (3, 9)


@pytest.mark.parametrize("name", CHILD_MODULES)
def test_child_modules_stay_runnable_on_the_oldest_target_python(name):
    # A target on Python 3.9 failed every extraction: isinstance(x, list | tuple) is 3.10+ at
    # runtime, which the parser alone does not catch.
    import ast
    from pathlib import Path

    import codeterrain

    source = (Path(codeterrain.__file__).parent / name).read_text(encoding="utf-8")
    tree = ast.parse(source, feature_version=CHILD_PYTHON_FLOOR)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("isinstance", "issubclass")
            and len(node.args) == 2
        ):
            assert not isinstance(node.args[1], ast.BinOp), f"{name}:{node.lineno} uses X | Y"
        if isinstance(node, ast.Call) and any(k.arg == "strict" for k in node.keywords):
            assert getattr(node.func, "id", "") != "zip", f"{name}:{node.lineno} zip(strict=)"
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("write_text", "read_text")
        ):
            used = {k.arg for k in node.keywords}
            assert "newline" not in used, f"{name}:{node.lineno} {node.func.attr}(newline=)"
