from __future__ import annotations

import json
import re
import subprocess
import sys

import pytest

from codeterrain.cache import Cache, git_env
from codeterrain.cli import main
from codeterrain.config import USER_CONFIG_ENV, resolve_root


@pytest.fixture
def run(sample_copy, cache_dir, capsys):
    def go(*argv: str) -> tuple[int, str, str]:
        code = main([*argv, "--root", str(sample_copy), "--cache-dir", str(cache_dir)])
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return go


def test_fixtures_table_lists_every_column(run):
    code, out, _ = run("fixtures")
    header, *rows = out.splitlines()
    assert code == 0
    assert header.split() == ["name", "scope", "autouse", "deps", "file:line", "overrides"]
    client = [r for r in rows if r.startswith("client ")]
    assert len(client) == 2
    assert any("tests/api/conftest.py:4" in r and "tests/conftest.py:34" in r for r in client)


def test_fixtures_filters_compose(run):
    _, out, _ = run("fixtures", "--autouse", "--scope", "S")
    assert [r.split()[0] for r in out.splitlines()[1:]] == ["_env_session"]
    _, out, _ = run("fixtures", "no-such-fixture")
    assert out.strip() == "no fixtures match"


def test_fixtures_json_is_the_full_envelope(run):
    code, out, _ = run("fixtures", "--json", "--budget", "1")
    data = json.loads(out)
    assert code == 0 and data["view"] == "fixtures"
    assert len(data["rows"]) == 12 and data["errors"] == []
    keys = {"name", "scope", "autouse", "argnames", "file", "line", "overrides"}
    assert keys <= set(data["rows"][0])


def test_budget_cuts_with_the_narrowing_footer(run):
    _, out, _ = run("fixtures", "--budget", "100")
    assert len(out) <= 100 * 4 + 1
    assert len(out.splitlines()) > 3
    assert re.search(r"\+\d+ more; narrow with a name substring, --scope or --autouse\n?$", out)


def test_fixture_prints_record_and_dependents(run):
    code, out, _ = run("fixture", "renamed")
    assert code == 0
    assert "name: renamed" in out and "function: _renamed_fixture" in out
    assert "dependents (2):" in out
    assert "test  test_marked  tests/api/test_items.py:" in out
    assert out.count("usefixtures") == 1


def test_fixture_json_and_unknown_name(run):
    code, out, _ = run("fixture", "client", "--json")
    data = json.loads(out)
    assert len(data["rows"]) == 2 and len(data["dependents"]) == 3
    code, out, err = run("fixture", "clien")
    assert code == 1 and out == "" and "similar: client" in err


def test_bad_scope_and_bad_config_exit_2_with_a_message(run, tmp_path):
    code, _, err = run("fixtures", "--scope", "global")
    assert code == 2 and err.startswith("terrain: unknown scope")
    bad = tmp_path / "bad.toml"
    bad.write_text("bogus = 1\n", encoding="utf-8")
    code, _, err = run("fixtures", "--config", str(bad))
    assert code == 2 and "unknown config key" in err


def test_doctor_reports_config_cache_versions_and_view_ages(run, cache_dir):
    run("fixtures")
    code, out, _ = run("doctor", "--python", sys.executable)
    assert code == 0
    assert "config: pyproject.toml [tool.codeterrain]" in out
    assert f"cache dir: {cache_dir.as_posix()}" in out
    assert re.search(r"versions: python 3\.\d+\.\d+, pytest \d+\S*, fastapi \S+", out)
    assert re.search(r"^fixtures\s+\d{4}-\d\d-\d\dT\S+Z\s+\d+s\s+0$", out, re.MULTILINE)
    assert re.search(r"^routes\s+never built$", out, re.MULTILINE)


def test_doctor_survives_a_missing_target_interpreter(run):
    code, out, _ = run("doctor", "--python", "definitely-not-a-python")
    assert code == 0 and "versions: unavailable" in out


def test_refresh_forces_a_rebuild(run, capsys):
    run("fixtures")
    code, out, _ = run("refresh", "fixtures")
    assert code == 0
    assert re.fullmatch(r"fixtures: 12 fixtures, 6 tests in \d+\.\d\ds\n", out)


def test_output_has_no_carriage_returns_or_backslash_paths(run):
    _, out, _ = run("fixtures")
    assert "\r" not in out and "\\" not in out


def test_repository_text_cannot_drive_the_terminal(run, sample_copy):
    # A fixture name= literal reached stdout raw.
    (sample_copy / "tests" / "test_hostile.py").write_text(
        "import pytest\n\n\n"
        '@pytest.fixture(name="safe\\x1b[2J\\x1b[HFORGED")\n'
        "def hostile():\n"
        "    return 1\n\n\n"
        '@pytest.mark.usefixtures("safe\\x1b[2J\\x1b[HFORGED")\n'
        "def test_uses_it():\n"
        "    pass\n",
        encoding="utf-8",
    )
    (sample_copy / "sampleapp" / "hostile.py").write_text(
        "from fastapi import FastAPI\n\napp = FastAPI()\n\n\n"
        '@app.get("/safe\\x1b[2J\\x1b[HFORGED")\n'
        "def forged():\n"
        "    return {}\n",
        encoding="utf-8",
    )
    config = sample_copy.parent / "hostile.toml"
    config.write_text('apps = ["sampleapp.hostile:app"]\n', encoding="utf-8")
    _, out, _ = run("fixtures", "safe")
    _, detail, _ = run("fixture", "safe\x1b[2J\x1b[HFORGED")
    _, _, err = run("fixture", "safe")
    _, order, _ = run("setup-order", "tests/test_hostile.py::test_uses_it")
    _, routes, _ = run("routes", "--config", str(config))
    _, route, _ = run("route", "GET", "/safe\x1b[2J\x1b[HFORGED", "--config", str(config))
    for text in (out, detail, err, order, routes, route):
        assert "\x1b" not in text
        assert "safe\\x1b[2J\\x1b[HFORGED" in text
    _, raw, _ = run("fixtures", "safe", "--json")
    assert json.loads(raw)["rows"][0]["name"] == "safe\x1b[2J\x1b[HFORGED"



def test_a_newline_in_repository_text_cannot_forge_a_line(run, sample_copy):
    # `terminal_safe` keeps the renderer's newlines, so a
    # newline inside a fixture name or route path printed as a row of its own.
    (sample_copy / "tests" / "test_forged.py").write_text(
        "import pytest\n\n\n"
        '@pytest.fixture(name="safe\\nFORGED  session  yes")\n'
        "def hostile():\n"
        "    return 1\n\n\n"
        '@pytest.mark.usefixtures("safe\\nFORGED  session  yes")\n'
        "def test_uses_it():\n"
        "    pass\n",
        encoding="utf-8",
    )
    (sample_copy / "sampleapp" / "forged.py").write_text(
        "from fastapi import FastAPI\n\napp = FastAPI()\n\n\n"
        '@app.get("/safe\\nFORGED")\n'
        "def forged():\n"
        "    return {}\n",
        encoding="utf-8",
    )
    config = sample_copy.parent / "forged.toml"
    config.write_text('apps = ["sampleapp.forged:app"]\n', encoding="utf-8")
    name = "safe\nFORGED  session  yes"
    outputs = [
        run("fixtures", "safe")[1],
        run("fixture", name)[1],
        run("fixture", "safe")[2],
        run("setup-order", "tests/test_forged.py::test_uses_it")[1],
        run("routes", "--config", str(config))[1],
        run("route", "GET", "/safe\nFORGED", "--config", str(config))[1],
        run("doctor", "--config", str(config))[1],
    ]
    for text in outputs:
        assert not any(line.startswith("FORGED") for line in text.splitlines()), text
    assert all("safe\\x0aFORGED" in text for text in outputs[:6])


def test_a_newline_in_a_config_error_cannot_forge_a_line(run, sample_copy):
    # An unknown quoted TOML key with an escaped newline was
    # printed as two stderr lines by every command.
    (sample_copy / "pyproject.toml").write_text(
        '[tool.codeterrain]\n"bad\\nFORGED line" = 1\n', encoding="utf-8"
    )
    code, _, err = run("fixtures")
    assert code == 2 and "bad\\x0aFORGED line" in err
    assert not any(line.startswith("FORGED") for line in err.splitlines()), err

@pytest.fixture
def warm(warm_sample, capsys):
    repo, cache = warm_sample

    def go(*argv: str) -> tuple[int, str, str]:
        code = main([*argv, "--root", str(repo), "--cache-dir", str(cache)])
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return go


def test_setup_order_prints_scope_letter_name_and_location(warm):
    code, out, _ = warm("setup-order", "tests/api/test_items.py::TestThings::test_one")
    lines = out.splitlines()
    assert code == 0 and lines[0] == "tests/api/test_items.py::TestThings::test_one"
    assert lines[1] == "S _env_session  tests/conftest.py:4"
    assert lines[5].startswith("C thing  tests/api/test_items.py:")
    assert [line[0] for line in lines[1:]] == list("SSMMCFF")


def test_setup_order_groups_a_function_with_its_params_and_flags_param_fixtures(warm):
    _, out, _ = warm("setup-order", "tests/api/test_items.py::test_get_item")
    first = out.splitlines()[0]
    assert first == "tests/api/test_items.py::test_get_item[a] (+1 more with this setup)"
    assert "F flavor [params]  tests/conftest.py:29" in out


def test_setup_order_unknown_node_exits_1(warm):
    code, out, err = warm("setup-order", "tests/nope.py::test_x")
    assert code == 1 and out == "" and "no collected test matches" in err


def test_setup_order_json(warm):
    _, out, _ = warm("setup-order", "tests/test_root.py::test_root", "--json")
    data = json.loads(out)
    assert data["view"] == "order" and data["stale_files"] == 0
    assert data["rows"][0]["nodes"] == ["tests/test_root.py::test_root"]


def test_routes_table_joins_access_and_filters(warm):
    code, out, _ = warm("routes", "items")
    header, *rows = out.splitlines()
    assert code == 0
    assert header.split() == ["method", "path", "app", "access", "handler", "file:line"]
    assert len(rows) == 4
    assert any(r.startswith("GET") and "/v1/items/{item_id} " in r and "member" in r for r in rows)
    _, out, _ = warm("routes", "--access", "staff")
    assert "/v1/admin/audit" in out and len(out.splitlines()) == 2
    _, out, _ = warm("routes", "--app", "other", "health")
    assert "public" in out and "sampleapp/other.py:" in out


def test_a_cut_route_listing_counts_what_it_left_out_per_app_and_survives_a_pipe(warm):
    _, out, err = warm("routes", "--budget", "40")
    footer = out.splitlines()[-1]
    match = re.fullmatch(r"\+(\d+) more: (.+); narrow with a path/handler substring.*", footer)
    assert match
    counts = dict(part.rsplit(" ", 1) for part in match.group(2).split(", "))
    assert sum(int(n) for n in counts.values()) == int(match.group(1))
    assert {"sampleapp.main:app", "sampleapp.other:app"} & set(counts)
    # captured stdout is not a terminal: the notice is repeated where a pipe cannot drop it
    assert f"terrain: output cut: {footer}" in err


def test_a_path_the_shell_rewrote_is_named_when_nothing_matches(warm, monkeypatch):
    from codeterrain import cli

    monkeypatch.setattr(cli, "_WINDOWS", True)
    _, out, _ = warm("routes", "C:/Program Files/Git/items")
    assert "no routes match" in out and "MSYS_NO_PATHCONV=1" in out
    monkeypatch.setattr(cli, "_WINDOWS", False)
    _, out, _ = warm("routes", "C:/Program Files/Git/items")
    assert "MSYS_NO_PATHCONV" not in out


def test_routes_json_reports_the_join_gaps(warm):
    _, out, _ = warm("routes", "--json")
    data = json.loads(out)
    assert data["manifest_rows_without_route"] == 1 and data["errors"] == []
    assert data["routes_without_manifest_row"] > 0


def test_route_prints_the_full_record_and_reports_misses(warm):
    code, out, _ = warm("route", "get", "/v1/items/{item_id}")
    assert code == 0
    assert "route: GET /v1/items/{item_id}" in out
    assert "access: member" in out and "response_model: Item" in out
    assert "dependencies: sampleapp.main.require_token" in out
    code, out, err = warm("route", "GET", "/v1/items")
    assert code == 1 and "no route GET /v1/items; similar:" in err and "POST /v1/items" in err


def _front_end(tmp_path):
    web = tmp_path / "web"
    (web / "src").mkdir(parents=True)
    (web / "src" / "items.ts").write_text(
        "export const one = (id: string) => apiClient.get<Item>(`/items/${id}`);\n"
        "export const add = (body: Item) => apiClient.post('/items', body);\n"
        "export const gone = () => apiClient.get('/items/old/stuff/here');\n"
        "export const dyn = () => api.get(endpoint);\n",
        encoding="utf-8",
    )
    (web / "codeterrain.toml").write_text('path_prefix = "/v1"\n', encoding="utf-8")
    user = tmp_path / "user.toml"
    user.write_text(f"client_repos = [{web.as_posix()!r}]\n", encoding="utf-8")
    return user


def test_callers_joins_front_end_calls_to_routes(warm, tmp_path, monkeypatch):
    # client_repos is user only: the user config names the front ends, the backend's own
    # pyproject keeps its apps.
    monkeypatch.setenv(USER_CONFIG_ENV, str(_front_end(tmp_path)))
    code, out, _ = warm("callers")
    assert code == 0
    lines = out.splitlines()
    assert lines[0] == "4 calls in 1 front ends: 2 hit a route, 1 hit none, 1 have no static path"
    assert lines[1].split() == ["repo", "method", "call", "path", "route", "file:line"]
    assert any("/v1/items/{}" in r and "GET /v1/items/{item_id}" in r for r in lines)
    code, out, _ = warm("callers", "post", "/v1/items")
    assert code == 0 and "src/items.ts:2" in out and "src/items.ts:1" not in out
    _, out, _ = warm("callers", "--unmatched", "--json")
    doc = json.loads(out)
    assert [(r["path"], r["route"]) for r in doc["rows"]] == [("/v1/items/old/stuff/here", None)]
    assert doc["clients"][0]["error"] is None


def test_callers_needs_client_repos_and_a_known_route(warm, tmp_path, monkeypatch):
    monkeypatch.setenv(USER_CONFIG_ENV, str(tmp_path / "no-user-config.toml"))
    code, _, err = warm("callers")
    assert code == 2 and "no client_repos configured" in err
    monkeypatch.setenv(USER_CONFIG_ENV, str(_front_end(tmp_path)))
    code, _, err = warm("callers", "GET", "/v1/nowhere")
    assert code == 1 and "no route GET /v1/nowhere" in err
    code, _, err = warm("callers", "GET")
    assert code == 2 and "METHOD and PATH together" in err
    # a Python name in place of the method: answered by `refs`, with a note saying so
    code, out, err = warm("callers", "require_token")
    assert code == 0 and "answering as terrain refs require_token" in err
    assert "function sampleapp.main.require_token" in out


def test_callers_never_runs_a_program_a_front_end_checkout_ships(warm, tmp_path, monkeypatch):
    # A git on PATH inside a front end must not run for the
    # backend's git status, nor the other way round.
    import os

    from codeterrain.executables import distrust, resolve_executable

    monkeypatch.setenv(USER_CONFIG_ENV, str(_front_end(tmp_path)))
    bin_dir = tmp_path / "web" / "bin"
    bin_dir.mkdir()
    planted = bin_dir / ("git.exe" if os.name == "nt" else "git")
    planted.write_bytes(b"")
    planted.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_dir), os.environ["PATH"]]))
    try:
        code, _, _ = warm("callers")
        assert code == 0
        with pytest.raises(FileNotFoundError, match="inside a target repository"):
            resolve_executable("git")
    finally:
        distrust([])


def test_callers_reports_front_end_files_it_could_not_parse(warm, tmp_path, monkeypatch):
    # The per-file errors of a front end were dropped, so `callers`
    # showed a partial join as a whole one.
    from codeterrain import languages

    # the built-in reader refuses this file; a language pack that reads calls may not
    monkeypatch.setattr(languages, "discover", lambda path=None: ([], []))
    user = _front_end(tmp_path)
    deep = "export const x = () => " + "api.get(" * 3000 + "'/v1/x'" + ")" * 3000 + ";"
    (tmp_path / "web" / "src" / "deep.ts").write_text(deep + chr(10), encoding="utf-8")
    monkeypatch.setenv(USER_CONFIG_ENV, str(user))
    code, out, err = warm("callers")
    assert code == 0 and "4 calls in 1 front ends" in out
    assert "warning: web: 1 files could not be parsed and their calls are missing" in err
    code, out, err = warm("callers", "--json")
    assert "could not be parsed" not in err
    (failure,) = json.loads(out)["clients"][0]["errors"]
    assert failure["file"] == "src/deep.ts" and "work bound" in failure["error"]


def test_callers_warns_when_a_front_end_checkout_has_uncommitted_changes(
    warm, tmp_path, monkeypatch
):
    import subprocess

    user = _front_end(tmp_path)
    web = tmp_path / "web"
    subprocess.run(["git", "init", "-q"], cwd=web, check=True, capture_output=True)
    monkeypatch.setenv(USER_CONFIG_ENV, str(user))
    code, _, err = warm("callers")
    assert code == 0
    assert "warning: web: 2 uncommitted changes; a gap may be the checkout, not the code" in err
    code, out, err = warm("callers", "--json")
    assert "warning: web" not in err
    assert json.loads(out)["clients"][0]["state"]["dirty"] == 2


def test_stale_stamp_leads_the_output_and_nothing_blocks(sample_copy, cache_dir, capsys):
    argv = ["--root", str(sample_copy), "--cache-dir", str(cache_dir)]
    assert main(["refresh", "routes", *argv]) == 0
    capsys.readouterr()
    (sample_copy / "sampleapp" / "extra.py").write_text("x = 1\n", encoding="utf-8")
    assert main(["routes", "health", *argv]) == 0
    out = capsys.readouterr().out.splitlines()
    assert re.fullmatch(r"stale: 1 files changed since \d{4}-\d\d-\d\dT\S+Z", out[0])
    assert "/health" in out[2]
    assert main(["routes", "health", "--fresh", *argv]) == 0
    assert not capsys.readouterr().out.startswith("stale:")


@pytest.mark.parametrize(
    ("argv", "schema"),
    [
        (["setup-order", "tests/test_root.py::test_root"], "codeterrain.order/2"),
        (["routes"], "codeterrain.routes/2"),
    ],
)
def test_a_wrong_extractor_schema_version_is_refused_with_exit_1(
    sample_copy, cache_dir, capsys, monkeypatch, argv, schema
):
    from codeterrain import fixtures_order, routes_view
    from codeterrain.sandbox import SandboxResult

    fake = SandboxResult(0, "", "", {"schema": schema})
    monkeypatch.setattr(fixtures_order, "run_python", lambda *a, **k: fake)
    monkeypatch.setattr(routes_view, "run_python", lambda *a, **k: fake)
    code = main([*argv, "--root", str(sample_copy), "--cache-dir", str(cache_dir)])
    captured = capsys.readouterr()
    assert code == 1 and captured.out == ""
    assert f"unsupported extractor schema '{schema}'" in captured.err


def test_routes_with_a_failing_app_warns_with_the_root_cause_and_lists_the_rest(
    sample_copy, cache_dir, capsys
):
    (sample_copy / "pyproject.toml").write_text(
        '[tool.codeterrain]\napps = ["sampleapp.main:app", "sampleapp.gone:app"]\n',
        encoding="utf-8",
    )
    code = main(["routes", "ping", "--root", str(sample_copy), "--cache-dir", str(cache_dir)])
    captured = capsys.readouterr()
    assert code == 0 and "/legacy/ping" in captured.out
    assert "app sampleapp.gone:app failed to import: ModuleNotFoundError" in captured.err
    # imported with codeterrain's own interpreter: say which flag picks the project's
    assert "pass --python or set CODETERRAIN_PYTHON" in captured.err


def test_a_secret_in_an_import_error_is_redacted_in_the_view_and_the_output(
    sample_copy, cache_dir, tmp_path, capsys, monkeypatch
):
    # Security review P3-1: a pass_env value echoed by the target's exception was printed and
    # cached in plaintext.
    monkeypatch.setenv("DATABASE_URL", "postgres://u:CANARY-dbpw@db/x")
    (sample_copy / "sampleapp" / "leaky.py").write_text(
        "import os\n"
        "raise RuntimeError('could not connect with DATABASE_URL=' + os.environ['DATABASE_URL']"
        " + ' as ' + os.environ['AWS_ACCESS_KEY_ID'] + ' token ' + os.environ['API_TOKEN'])\n",
        encoding="utf-8",
    )
    config = tmp_path / "leaky.toml"
    config.write_text(
        'apps = ["sampleapp.main:app", "sampleapp.leaky:app"]\npass_env = ["DATABASE_URL"]\n'
        'import_env = { API_TOKEN = "CANARY-import-token" }\n',
        encoding="utf-8",
    )
    argv = ["--root", str(sample_copy), "--cache-dir", str(cache_dir), "--config", str(config)]
    assert main(["routes", *argv]) == 0
    assert main(["doctor", *argv]) == 0
    captured = capsys.readouterr()
    shown = captured.out + captured.err
    assert "DATABASE_URL=[REDACTED] as [REDACTED] token [REDACTED]" in shown
    cached = "".join(p.read_text(encoding="utf-8") for p in cache_dir.rglob("*.json"))
    for text in (shown, cached):
        assert "CANARY" not in text and "codeterrain-dummy" not in text


def test_refresh_all_reports_every_view(sample_copy, cache_dir, capsys):
    code = main(["refresh", "--root", str(sample_copy), "--cache-dir", str(cache_dir)])
    out = capsys.readouterr().out
    assert code == 0
    assert re.search(r"^fixtures: 12 fixtures, 6 tests in", out, re.MULTILINE)
    assert re.search(r"^calls: 0 calls, 0 with a static path in", out, re.MULTILINE)
    assert re.search(r"^order: 10 tests, 6 distinct setups in", out, re.MULTILINE)
    assert re.search(r"^routes: \d+ routes from 2 apps in", out, re.MULTILINE)


def test_doctor_reports_each_app_from_the_cached_walk(warm):
    code, out, _ = warm("doctor")
    assert code == 0
    assert re.search(r"^  sampleapp\.main:app: ok, \d+ routes$", out, re.MULTILINE)
    assert re.search(r"^  sampleapp\.other:other: ok, \d+ routes$", out, re.MULTILINE)


def test_doctor_shows_a_failed_import_as_one_line_without_a_traceback(
    sample_copy, cache_dir, capsys
):
    (sample_copy / "pyproject.toml").write_text(
        '[tool.codeterrain]\napps = ["sampleapp.gone:app", "sampleapp.main:app"]\n',
        encoding="utf-8",
    )
    argv = ["--root", str(sample_copy), "--cache-dir", str(cache_dir)]
    main(["doctor", *argv])
    assert "sampleapp.gone:app: not imported yet" in capsys.readouterr().out
    main(["refresh", "routes", *argv])
    capsys.readouterr()
    main(["doctor", *argv])
    out = capsys.readouterr().out
    failed = [line for line in out.splitlines() if "import failed" in line]
    assert len(failed) == 1 and "ModuleNotFoundError" in failed[0] and "sampleapp.gone" in failed[0]
    assert "Traceback" not in out and "File " not in out


def test_missing_test_paths_are_called_out_in_doctor_and_in_an_empty_fixtures_answer(
    tmp_path, capsys
):
    root = tmp_path / "bare"
    root.mkdir()
    argv = ["--root", str(root), "--cache-dir", str(tmp_path / "cache")]
    assert main(["fixtures", *argv]) == 0
    assert capsys.readouterr().out.strip() == "no fixtures found in test_paths: tests (missing)"
    main(["doctor", *argv])
    assert "test_paths: tests (missing)" in capsys.readouterr().out


def _snapshot(root, skip=()):
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not any(part in skip for part in path.parts):
            stat = path.stat()
            out[path.relative_to(root).as_posix()] = (stat.st_size, stat.st_mtime_ns)
    return out


@pytest.mark.parametrize("in_git", [True, False])
def test_refresh_writes_nothing_into_the_target(sample_copy, tmp_path, monkeypatch, capsys, git,
                                                 in_git):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "userdata"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "userdata"))
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    if in_git:
        git(sample_copy, "init", "-q")
        git(sample_copy, "add", ".")
        git(sample_copy, "commit", "-q", "-m", "sample")
    cache = Cache(sample_copy)
    skip = (cache.dir.name,) if in_git else ()
    before = _snapshot(sample_copy, skip)
    assert main(["refresh", "--root", str(sample_copy)]) == 0
    capsys.readouterr()
    assert _snapshot(sample_copy, skip) == before
    assert any(cache.dir.rglob("*.json"))
    if not in_git:
        assert sample_copy not in cache.dir.parents


def test_setup_order_with_no_match_says_how_many_files_failed_to_collect(
    sample_copy, cache_dir, capsys
):
    (sample_copy / "tests" / "test_broken.py").write_text("import not_a_module\n", encoding="utf-8")
    code = main(["setup-order", "tests/nope.py::test_x", "--root", str(sample_copy),
                 "--cache-dir", str(cache_dir)])
    err = capsys.readouterr().err
    assert code == 1
    assert "no collected test matches tests/nope.py::test_x" in err
    assert re.search(r"note: [1-9]\d* collection errors in the extraction run", err)


def test_refresh_carries_on_past_a_failing_view_reports_it_and_exits_1(
    sample_copy, cache_dir, capsys, monkeypatch
):
    from codeterrain import fixtures_order

    def boom(config, cache_dir, python):
        raise fixtures_order.ExtractError("pytest left no order document (exit 4): usage\nerror")

    monkeypatch.setattr(fixtures_order, "extract", boom)
    code = main(["refresh", "--root", str(sample_copy), "--cache-dir", str(cache_dir)])
    captured = capsys.readouterr()
    assert code == 1
    assert re.search(r"^fixtures: ", captured.out, re.MULTILINE)
    assert re.search(r"^routes: \d+ routes", captured.out, re.MULTILINE)
    assert "order:" not in captured.out
    errors = [x for x in captured.err.splitlines() if "building" not in x]
    assert errors == ["terrain: order failed: pytest left no order document (exit 4): usage error"]


def test_unparseable_files_warn_in_text_mode_only(sample_copy, cache_dir, capsys):
    (sample_copy / "tests" / "test_syntax.py").write_text("def broken(:\n", encoding="utf-8")
    argv = ["--root", str(sample_copy), "--cache-dir", str(cache_dir)]
    assert main(["fixtures", *argv]) == 0
    err = capsys.readouterr().err
    assert err == "warning: 1 files could not be parsed (run with --json for details)\n"
    assert main(["fixtures", "--json", *argv]) == 0
    captured = capsys.readouterr()
    assert captured.err == "" and json.loads(captured.out)["errors"][0]["file"].endswith(
        "test_syntax.py"
    )
    assert main(["refresh", "fixtures", *argv]) == 0
    assert "1 files could not be parsed" in capsys.readouterr().err


def _only_line(err):
    """The one error line on stderr (progress notes aside); no traceback anywhere."""
    lines = [x for x in err.strip().splitlines() if not x.startswith("terrain: building ")]
    assert len(lines) == 1 and lines[0].startswith("terrain: ")
    assert "Traceback" not in err and "File \"" not in err
    return lines[0]


def test_a_bad_python_is_one_stderr_line_and_exit_1(sample_copy, cache_dir, capsys):
    code = main(["setup-order", "x", "--python", "definitely-not-a-python",
                 "--root", str(sample_copy), "--cache-dir", str(cache_dir)])
    assert code == 1
    assert "cannot start" in _only_line(capsys.readouterr().err)


def test_a_failing_extraction_is_one_stderr_line_and_exit_1(
    sample_copy, cache_dir, tmp_path, capsys
):
    bad = tmp_path / "bad.toml"
    bad.write_text('pytest_args = ["--no-such-pytest-flag"]\n', encoding="utf-8")
    code = main(["setup-order", "x", "--root", str(sample_copy), "--cache-dir", str(cache_dir),
                 "--config", str(bad)])
    line = _only_line(capsys.readouterr().err)
    assert code == 1 and "pytest left no order document" in line


def test_a_bad_manifest_is_one_stderr_line_and_exit_1(sample_copy, cache_dir, capsys):
    (sample_copy / "docs" / "route-access.json").write_text("{not json", encoding="utf-8")
    code = main(["routes", "--root", str(sample_copy), "--cache-dir", str(cache_dir)])
    line = _only_line(capsys.readouterr().err)
    assert code == 1 and "route manifest is not JSON" in line


def test_a_locked_cache_file_is_one_stderr_line_and_exit_1(
    sample_copy, cache_dir, capsys, monkeypatch
):
    from codeterrain import cache as cache_module

    def locked(src, dst):
        raise PermissionError("locked")

    monkeypatch.setattr(cache_module.os, "replace", locked)
    monkeypatch.setattr(cache_module.time, "sleep", lambda s: None)
    code = main(["fixtures", "--root", str(sample_copy), "--cache-dir", str(cache_dir)])
    assert code == 1
    assert "locked by another process" in _only_line(capsys.readouterr().err)


def test_the_target_interpreter_is_flag_then_environment_then_the_tools_own(monkeypatch):
    import argparse

    from codeterrain.cli import Context

    def python(flag):
        return Context(None, None, argparse.Namespace(python=flag)).python

    assert python(None) == sys.executable
    monkeypatch.setenv("CODETERRAIN_PYTHON", "from-env")
    assert python(None) == "from-env"
    assert python("from-flag") == "from-flag"


def test_doctor_points_at_the_targets_venv_when_it_would_run_in_its_own_interpreter(
    sample_copy, cache_dir, capsys
):
    argv = ["--root", str(sample_copy), "--cache-dir", str(cache_dir)]
    main(["doctor", *argv])
    assert "hint:" not in capsys.readouterr().out
    (sample_copy / ".venv").mkdir()
    main(["doctor", *argv])
    out = capsys.readouterr().out
    assert re.search(r"^hint: .*\.venv.* --python \S*\.venv/(Scripts/python\.exe|bin/python)$",
                     out, re.MULTILINE)
    main(["doctor", "--python", "definitely-not-a-python", *argv])
    assert "hint:" not in capsys.readouterr().out


def test_doctor_lists_pass_env_names_and_respect_addopts(
    sample_copy, cache_dir, tmp_path, capsys, monkeypatch
):
    user = tmp_path / "user.toml"
    user.write_text('pass_env = ["TOKEN_A", "TOKEN_B"]\nrespect_addopts = true\n', encoding="utf-8")
    monkeypatch.setenv(USER_CONFIG_ENV, str(user))
    main(["doctor", "--root", str(sample_copy), "--cache-dir", str(cache_dir)])
    out = capsys.readouterr().out
    assert "pass_env: TOKEN_A, TOKEN_B" in out and "respect_addopts: yes" in out
    assert f"user config: {user.as_posix()}" in out


def _canary_conftest(repo, seen):
    """A conftest that records which parent secrets reached the sandboxed child."""
    (repo / "tests" / "conftest.py").write_text(
        (repo / "tests" / "conftest.py").read_text(encoding="utf-8")
        + "\nimport os as _os\n"
        + f"open({str(seen)!r}, 'w').write(_os.environ.get('CT_CANARY_TOKEN', 'absent'))\n",
        encoding="utf-8",
    )


def test_a_repository_pass_env_never_reaches_the_child_and_is_reported(
    sample_copy, cache_dir, tmp_path, capsys, monkeypatch
):
    # Security review P2-2: pass_env in the target's pyproject handed it the user's secrets.
    monkeypatch.setenv("CT_CANARY_TOKEN", "CANARY-token")
    seen = tmp_path / "seen.txt"
    _canary_conftest(sample_copy, seen)
    pyproject = sample_copy / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace(
            "[tool.codeterrain]\n", '[tool.codeterrain]\npass_env = ["CT_CANARY_TOKEN"]\n'
        ),
        encoding="utf-8",
    )
    argv = ["refresh", "order", "--root", str(sample_copy), "--cache-dir", str(cache_dir)]
    assert main(argv) == 0
    assert seen.read_text() == "absent"
    err = capsys.readouterr().err
    assert "warning: ignored pass_env from the repository's pyproject.toml" in err
    opted = tmp_path / "opted.toml"
    opted.write_text('pass_env = ["CT_CANARY_TOKEN"]\n', encoding="utf-8")
    assert main([*argv, "--config", str(opted)]) == 0
    assert seen.read_text() == "CANARY-token"
    assert "warning" not in capsys.readouterr().err


def test_a_config_switch_rebuilds_and_answers_with_the_other_apps_rows(
    sample_copy, cache_dir, tmp_path, capsys
):
    def alt(app):
        path = tmp_path / f"{app}.toml"
        path.write_text(f'apps = ["{app}"]\n', encoding="utf-8")
        return str(path)

    argv = ["--root", str(sample_copy), "--cache-dir", str(cache_dir)]
    assert main(["routes", "--config", alt("sampleapp.main:app"), *argv]) == 0
    first = capsys.readouterr().out
    assert "/v1/items/{item_id}" in first and "/health" not in first
    assert main(["routes", "--config", alt("sampleapp.other:other"), *argv]) == 0
    second = capsys.readouterr().out
    assert "/health" in second and "/v1/items/{item_id}" not in second
    assert not second.startswith("stale:")


def test_doctor_says_needs_refresh_when_the_view_was_built_under_another_config(
    sample_copy, cache_dir, tmp_path, capsys
):
    first = tmp_path / "first.toml"
    first.write_text('apps = ["sampleapp.main:app"]\n', encoding="utf-8")
    second = tmp_path / "second.toml"
    second.write_text('apps = ["sampleapp.main:app"]\nregion = "eu-west-1"\n', encoding="utf-8")
    argv = ["--root", str(sample_copy), "--cache-dir", str(cache_dir)]
    assert main(["refresh", "routes", "--config", str(first), *argv]) == 0
    capsys.readouterr()
    main(["doctor", "--config", str(first), *argv])
    assert "needs refresh" not in capsys.readouterr().out
    main(["doctor", "--config", str(second), *argv])
    out = capsys.readouterr().out
    assert re.search(r"^routes .*  needs refresh \(config or environment changed\)$", out,
                     re.MULTILINE)
    assert "sampleapp.main:app: needs refresh" in out


def test_doctor_warns_about_host_routes_the_walker_skipped(sample_copy, cache_dir, capsys):
    (sample_copy / "sampleapp" / "hosted.py").write_text(
        "from starlette.applications import Starlette\n"
        "from starlette.routing import Host\n"
        "app = Starlette(routes=[Host('api.example.com', app=Starlette())])\n",
        encoding="utf-8",
    )
    (sample_copy / "pyproject.toml").write_text(
        '[tool.codeterrain]\napps = ["sampleapp.hosted:app"]\n', encoding="utf-8"
    )
    argv = ["--root", str(sample_copy), "--cache-dir", str(cache_dir)]
    assert main(["refresh", "routes", *argv]) == 0
    capsys.readouterr()
    main(["doctor", *argv])
    out = capsys.readouterr().out
    assert "warning: sampleapp.hosted:app: Host route 'api.example.com' is not walked" in out


def _valid_docs():
    from codeterrain.protocol import ORDER_SCHEMA, ROUTES_SCHEMA

    order = {"schema": ORDER_SCHEMA, "closures": [], "nodes": {}, "errors": 0}
    routes = {"schema": ROUTES_SCHEMA, "routes": [], "errors": []}
    return order, routes


@pytest.mark.parametrize(
    "error",
    ["ExtractError", "SandboxError", "ProtocolError", "ManifestError", "CacheBusy"],
)
def test_refresh_carries_on_past_every_error_type_codeterrain_raises(
    sample_copy, cache_dir, capsys, monkeypatch, error
):
    from codeterrain import cache, fixtures_order, protocol, routes_view, sandbox
    from codeterrain.plugins import manifest_join

    raised = {
        "ExtractError": fixtures_order.ExtractError,
        "SandboxError": sandbox.SandboxError,
        "ProtocolError": protocol.ProtocolError,
        "ManifestError": manifest_join.ManifestError,
        "CacheBusy": cache.CacheBusy,
    }[error]
    order, routes = _valid_docs()
    attempted = []

    def failing(config, cache_dir, python):
        attempted.append("order")
        raise raised(f"{error} happened")

    def routes_extract(config, cache_dir, python):
        attempted.append("routes")
        return routes

    monkeypatch.setattr(fixtures_order, "extract", failing)
    monkeypatch.setattr(routes_view, "extract", routes_extract)
    code = main(["refresh", "--root", str(sample_copy), "--cache-dir", str(cache_dir)])
    captured = capsys.readouterr()
    assert code == 1 and attempted == ["order", "routes"]
    assert re.search(r"^fixtures: ", captured.out, re.MULTILINE)
    assert re.search(r"^routes: 0 routes", captured.out, re.MULTILINE)
    assert f"terrain: order failed: {error} happened" in captured.err


def test_a_bad_python_does_not_stop_refresh_from_attempting_every_view(
    sample_copy, cache_dir, capsys
):
    code = main(["refresh", "--python", "definitely-not-a-python", "--root", str(sample_copy),
                 "--cache-dir", str(cache_dir)])
    captured = capsys.readouterr()
    assert code == 1
    assert re.search(r"^fixtures: 12 fixtures", captured.out, re.MULTILINE)
    failures = [x for x in captured.err.splitlines() if " failed: " in x]
    assert len(failures) == 2
    assert failures[0].startswith("terrain: order failed: cannot start")
    assert failures[1].startswith("terrain: routes failed: cannot start")


@pytest.mark.parametrize("view", ["order", "routes"])
def test_refreshing_a_warm_slow_view_rebuilds_it_every_time(
    sample_copy, cache_dir, capsys, monkeypatch, view
):
    from codeterrain import fixtures_order, routes_view

    order, routes = _valid_docs()
    module, doc = (fixtures_order, order) if view == "order" else (routes_view, routes)
    calls = []

    def counting(config, cache_dir, python):
        calls.append(1)
        return doc

    monkeypatch.setattr(module, "extract", counting)
    argv = ["--root", str(sample_copy), "--cache-dir", str(cache_dir)]
    assert main(["refresh", view, *argv]) == 0
    assert main(["refresh", view, *argv]) == 0
    capsys.readouterr()
    assert len(calls) == 2


def test_fixtures_fresh_forces_the_static_build_and_a_plain_run_does_not(
    sample_copy, cache_dir, capsys, monkeypatch
):
    from codeterrain import fixtures_static

    real = fixtures_static.build
    forced = []

    def spy(cache, config, force=False):
        forced.append(force)
        return real(cache, config, force=force)

    monkeypatch.setattr(fixtures_static, "build", spy)
    argv = ["--root", str(sample_copy), "--cache-dir", str(cache_dir)]
    assert main(["fixtures", *argv]) == 0
    assert main(["fixtures", "--fresh", *argv]) == 0
    capsys.readouterr()
    assert forced == [False, True]


def test_collection_errors_are_in_the_json_and_in_the_note_when_the_node_is_found(
    sample_copy, cache_dir, capsys
):
    (sample_copy / "tests" / "test_broken.py").write_text("import not_a_module\n", encoding="utf-8")
    argv = ["--root", str(sample_copy), "--cache-dir", str(cache_dir)]
    node = "tests/test_root.py::test_root"
    assert main(["setup-order", node, "--json", *argv]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["collection_errors"] >= 1 and data["rows"]
    assert main(["setup-order", node, *argv]) == 0
    out = capsys.readouterr().out
    assert re.search(r"note: [1-9]\d* collection errors in the extraction run$", out.strip())


def test_each_app_gets_only_its_own_host_warning(sample_copy, cache_dir, capsys):
    for name, host in (("hosted_a", "a.example.com"), ("hosted_b", "b.example.com")):
        (sample_copy / "sampleapp" / f"{name}.py").write_text(
            "from starlette.applications import Starlette\n"
            "from starlette.routing import Host\n"
            f"app = Starlette(routes=[Host({host!r}, app=Starlette())])\n",
            encoding="utf-8",
        )
    (sample_copy / "pyproject.toml").write_text(
        '[tool.codeterrain]\napps = ["sampleapp.hosted_a:app", "sampleapp.main:app",'
        ' "sampleapp.hosted_b:app"]\n',
        encoding="utf-8",
    )
    argv = ["--root", str(sample_copy), "--cache-dir", str(cache_dir)]
    assert main(["refresh", "routes", *argv]) == 0
    capsys.readouterr()
    main(["doctor", *argv])
    warnings = [x for x in capsys.readouterr().out.splitlines() if x.startswith("  warning:")]
    assert warnings == [
        "  warning: sampleapp.hosted_a:app: Host route 'a.example.com' is not walked, "
        "its routes are missing",
        "  warning: sampleapp.hosted_b:app: Host route 'b.example.com' is not walked, "
        "its routes are missing",
    ]


def test_ci_workflow_pins_actions_by_sha():
    from pathlib import Path

    text = (Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    uses = re.findall(r"^\s*-?\s*uses:\s*(\S+)", text, re.MULTILINE)
    assert len(uses) >= 3
    assert all(re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", ref) for ref in uses), uses
    assert re.search(r"^permissions:\n  contents: read$", text, re.MULTILINE)
    assert "windows-latest" in text
    assert re.search(r"^\s+- os: windows-latest$", text, re.MULTILINE)


def test_ci_installs_from_the_lockfile_and_drops_checkout_credentials():
    # Security review P3-10: uv.lock was ignored and checkout kept its token in .git/config.
    from pathlib import Path

    repo = Path(__file__).parents[1]
    text = (repo / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    checkouts = re.findall(r"uses: actions/checkout@\S+[^\n]*\n\s+with:\n\s+(.+)", text)
    assert checkouts and all(c == "persist-credentials: false" for c in checkouts)
    assert text.count("actions/checkout@") == len(checkouts)
    assert "uv sync --locked" in text and "uv run --locked" in text
    assert (repo / "uv.lock").is_file()
    ignored = (repo / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "uv.lock" not in ignored


def test_one_deep_generated_file_does_not_stop_the_fixture_index(run, sample_copy):
    # 100,000 terms: the C recursion limit is far higher on Linux than on Windows.
    (sample_copy / "tests" / "test_gen.py").write_text(
        "x = " + " + ".join(['"a"'] * 100_000) + "\n", encoding="utf-8"
    )
    code, out, err = run("fixtures")
    assert code == 0 and out.startswith("name")
    assert "1 files could not be parsed" in err
    _, raw, _ = run("fixtures", "--json")
    assert [e["file"] for e in json.loads(raw)["errors"]] == ["tests/test_gen.py"]


def test_user_only_keys_in_a_codeterrain_toml_are_ignored_with_a_warning_naming_it(
    tmp_path, capsys
):
    root = tmp_path / "web"
    root.mkdir()
    (root / "codeterrain.toml").write_text('pass_env = ["TOKEN"]\n', encoding="utf-8")
    assert main(["calls", "--root", str(root), "--cache-dir", str(tmp_path / "cache")]) == 0
    err = capsys.readouterr().err
    assert "warning: ignored pass_env from the repository's codeterrain.toml" in err


def _git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd, check=True, capture_output=True, env=git_env(),
    )


def test_worktrees_share_a_cache_and_each_answer_names_its_checkout(tmp_path, monkeypatch, capsys):
    # From the field: an agent edited a worktree and asked from the main checkout; the answer
    # missed the worktree's new call and gave no sign it described another checkout.
    main_root = resolve_root(tmp_path / "main")
    main_root.mkdir()
    _git(main_root, "init", "-q")
    (main_root / "lib.py").write_text("def create():\n    pass\n", encoding="utf-8")
    (main_root / "a.py").write_text("from lib import create\ncreate()\n", encoding="utf-8")
    _git(main_root, "add", ".")
    _git(main_root, "commit", "-q", "-m", "init")
    linked = resolve_root(tmp_path / "linked")
    _git(main_root, "worktree", "add", "-q", str(linked), "-b", "edit")
    (linked / "b.py").write_text("from lib import create\ncreate()\n", encoding="utf-8")
    shared = str(tmp_path / "shared-cache")

    def refs(where):
        monkeypatch.chdir(where)
        assert main(["refs", "create", "--json", "--cache-dir", shared]) == 0
        captured = capsys.readouterr()
        return json.loads(captured.out), captured.err

    for _ in range(2):  # twice each, alternating: neither run poisons the other's index
        doc, err = refs(linked)
        assert doc["root"] == linked.as_posix()
        assert sorted(r["file"] for r in doc["rows"]) == ["a.py", "b.py"]
        assert f"answered from {linked.as_posix()} (a worktree)" in err
        assert "1 other checkout: run terrain from the one you are editing" in err
        doc, err = refs(main_root)
        assert [r["file"] for r in doc["rows"]] == ["a.py"]
        assert f"answered from {main_root.as_posix()} (the main checkout)" in err

    monkeypatch.chdir(linked)
    assert main(["def", "create", "--root", str(main_root), "--cache-dir", shared]) == 0
    err = capsys.readouterr().err
    assert f"warning: --root is {main_root.as_posix()}, but the current directory" in err
    assert linked.as_posix() in err
    assert main(["doctor", "--root", str(main_root), "--cache-dir", shared]) == 0
    captured = capsys.readouterr()
    assert f"checkout: the main checkout; other checkouts: {linked.as_posix()}" in captured.out
    assert "answered from" not in captured.err


def test_def_kind_lists_or_filters_definitions_of_one_kind(tmp_path, monkeypatch, capsys):
    from codeterrain import languages

    monkeypatch.setattr(languages, "discover", lambda path=None: ([], []))
    root = tmp_path / "repo"
    root.mkdir()
    (root / "lib.py").write_text(
        "class Order:\n    def create(self):\n        pass\n\n\ndef create():\n    pass\n",
        encoding="utf-8",
    )
    argv = ["--root", str(root), "--cache-dir", str(tmp_path / "cache")]
    assert main(["def", "--kind", "class", "--json", *argv]) == 0
    assert [r["name"] for r in json.loads(capsys.readouterr().out)["rows"]] == ["lib.Order"]
    assert main(["def", "create", "--kind", "method", "--json", *argv]) == 0
    assert [r["name"] for r in json.loads(capsys.readouterr().out)["rows"]] == [
        "lib.Order.create"
    ]
    assert main(["def", "create", "--kind", "class", *argv]) == 1
    assert "no definition of kind class named create; kinds here: class, function, method" in (
        capsys.readouterr().err
    )
    assert main(["def", *argv]) == 2
    assert "give a name, --kind, or both" in capsys.readouterr().err


def test_def_kind_filters_before_the_match_limit(tmp_path, monkeypatch, capsys):
    # More same-named functions than the match limit never hide the one method asked for.
    from codeterrain import code_static, languages

    monkeypatch.setattr(languages, "discover", lambda path=None: ([], []))
    root = tmp_path / "repo"
    root.mkdir()
    for n in range(code_static.MAX_MATCHES + 5):
        (root / f"a{n:03}.py").write_text("def create():\n    pass\n", encoding="utf-8")
    (root / "z.py").write_text("class Order:\n    def create(self):\n        pass\n", "utf-8")
    argv = ["--root", str(root), "--cache-dir", str(tmp_path / "cache")]
    assert main(["def", "create", "--kind", "method", "--json", *argv]) == 0
    assert [r["name"] for r in json.loads(capsys.readouterr().out)["rows"]] == ["z.Order.create"]


def test_a_single_checkout_gets_no_checkout_note(tmp_path, monkeypatch, capsys):
    root = resolve_root(tmp_path / "solo")
    root.mkdir()
    _git(root, "init", "-q")
    (root / "lib.py").write_text("def create():\n    pass\n", encoding="utf-8")
    monkeypatch.chdir(root)
    assert main(["def", "create", "--cache-dir", str(tmp_path / "cache")]) == 0
    assert "answered from" not in capsys.readouterr().err


def test_a_test_tree_outside_test_paths_is_named_by_fixture_answers_and_doctor(tmp_path, capsys):
    # A same-named fixture in `tests/` is not the one a test in the other tree gets.
    root = tmp_path / "repo"
    for rel, text in {
        "tests/conftest.py": "import pytest\n@pytest.fixture\ndef game_id():\n    return 1\n",
        "tests_live/conftest.py": "import pytest\n@pytest.fixture\ndef game_id():\n    return 2\n",
    }.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    argv = ["--root", str(root), "--cache-dir", str(tmp_path / "cache")]
    note = "test_paths leaves out tests_live, which holds a conftest.py"
    assert main(["fixture", "game_id", *argv]) == 0
    assert note in capsys.readouterr().err
    assert main(["fixtures", "--json", *argv]) == 0
    assert json.loads(capsys.readouterr().out)["unlisted_test_paths"] == ["tests_live"]
    # A miss prints no JSON, so the note stays on stderr.
    assert main(["fixture", "live_only", "--json", *argv]) == 1
    assert note in capsys.readouterr().err
    main(["doctor", *argv])
    assert f"warning: {note}" in capsys.readouterr().out


def test_importers_and_imports_answer_for_a_module(tmp_path, capsys):
    root = tmp_path / "repo"
    for rel, text in {
        "app/__init__.py": "",
        "app/orders.py": "import json\ndef create():\n    pass\n",
        "app/views.py": "from app.orders import create\n",
        "tests/test_orders.py": "from app import orders\n",
    }.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    argv = ["--root", str(root), "--cache-dir", str(tmp_path / "cache")]
    assert main(["importers", "orders", *argv]) == 0
    out = capsys.readouterr().out
    assert "module app.orders  app/orders.py" in out
    assert "2 imports in 2 files (1 in code, 1 in tests)" in out
    assert "app/views.py:1" in out and "tests/test_orders.py:1" in out
    assert main(["importers", "app.orders", "--tests", "--json", *argv]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert [r["file"] for r in doc["rows"]] == ["tests/test_orders.py"]
    assert doc["modules"] == ["app.orders"] and not doc["truncated"]
    assert main(["imports", "app.orders", *argv]) == 0
    assert "app/orders.py: 1 import" in capsys.readouterr().out
    assert main(["importers", "nowhere", *argv]) == 1
    assert "no module named nowhere" in capsys.readouterr().err
    # `refs` on a module name points at `importers`.
    assert main(["refs", "orders", *argv]) == 1
    assert "orders is a module: terrain importers orders" in capsys.readouterr().err


def test_a_route_text_that_matches_no_route_finds_the_handlers_reading_such_a_name(tmp_path):
    # `routes legal` matched no path or handler, though a handler reads LEGAL_PRIVACY_VERSION.
    from codeterrain.cli import routes_reading
    from codeterrain.code_static import Index, update
    from codeterrain.config import load_config

    root = tmp_path / "repo"
    (root / "api").mkdir(parents=True)
    (root / "api" / "settings.py").write_text("LEGAL_PRIVACY_VERSION = '3'\n", encoding="utf-8")
    (root / "api" / "routes.py").write_text(
        "from api.settings import LEGAL_PRIVACY_VERSION\n"
        "def get_features_version():\n"
        "    return {'privacy': LEGAL_PRIVACY_VERSION}\n"
        "def helper():\n"
        "    return LEGAL_PRIVACY_VERSION\n"
        "def other():\n"
        "    return helper()\n",
        encoding="utf-8",
    )
    index = Index(update(Cache(root, tmp_path / "cache"), load_config(root)))
    routes = [
        {"method": "GET", "path": "/v1/regulations/features/version", "file": "api/routes.py",
         "handler": "api.routes.get_features_version", "line": 2},
        {"method": "GET", "path": "/v1/other", "file": "api/routes.py",
         "handler": "api.routes.other", "line": 6},
    ]
    found, truncated = routes_reading(index, "legal", routes)
    assert not truncated
    assert [(r["path"], name) for r, name in found] == [
        ("/v1/regulations/features/version", "api.settings.LEGAL_PRIVACY_VERSION"),
    ]
    assert routes_reading(index, "nothing_like_it", routes) == ([], False)


def test_a_handler_is_matched_by_its_whole_qualified_name(tmp_path):
    from codeterrain.cli import routes_reading
    from codeterrain.code_static import Index, update
    from codeterrain.config import load_config

    root = tmp_path / "repo"
    (root / "apps" / "web").mkdir(parents=True)
    (root / "apps" / "web" / "pyproject.toml").write_text("[project]\nname='web'\n")
    (root / "apps" / "web" / "routes.py").write_text(
        "LEGAL_VERSION = '1'\n"
        "def version():\n"
        "    return LEGAL_VERSION\n"
        "class Controller:\n"
        "    def version(self):\n"
        "        return '2'\n"
        "    def legal(self):\n"
        "        return LEGAL_VERSION\n"
        "def get_versions():\n"
        "    return [LEGAL_VERSION for _ in range(2)]\n"
        "def create_app():\n"
        "    def version():\n"
        "        return LEGAL_VERSION\n"
        "    def wrapped():\n"
        "        return lambda: LEGAL_VERSION\n"
        "class Version:\n"
        "    def __call__(self):\n"
        "        return LEGAL_VERSION\n",
        encoding="utf-8",
    )
    index = Index(update(Cache(root, tmp_path / "cache"), load_config(root)))
    # the nested project imports the file as `routes`, not `apps.web.routes`
    routes = [
        {"method": "GET", "path": f"/{name}", "file": "apps/web/routes.py",
         "handler": f"routes.{name}", "line": 1}
        for name in ("Controller.version", "Controller.legal", "get_versions",
                     "create_app.<locals>.version", "create_app.<locals>.wrapped", "Version")
    ]
    # a comprehension is the handler's own code, a factory's endpoint is named with
    # `<locals>` at run time, a lambda the handler returns is not its own body, and a
    # callable instance is named by its class
    assert [r["path"] for r, _ in routes_reading(index, "legal", routes)[0]] == [
        "/Controller.legal", "/get_versions", "/create_app.<locals>.version", "/Version",
    ]


def test_the_name_read_fallback_keeps_the_app_and_access_filters(warm, monkeypatch):
    from codeterrain import cli

    seen = []
    monkeypatch.setattr(
        cli, "routes_reading", lambda index, text, routes: (seen.extend(routes), ([], False))[1]
    )
    code, out, _ = warm("routes", "zzz_nothing", "--app", "other")
    assert code == 0 and "no route handler reads a name containing zzz_nothing" in out
    assert seen and all("other" in r["app"] for r in seen)


def test_the_name_read_fallback_reads_past_unrelated_names_and_says_when_it_stopped(
    tmp_path, monkeypatch
):
    # Only the first 20 names containing the text were read, so twenty
    # unrelated constants hid the one the handler reads.
    from codeterrain import cli
    from codeterrain.code_static import Index, update
    from codeterrain.config import load_config

    root = tmp_path / "repo"
    (root / "api").mkdir(parents=True)
    (root / "api" / "settings.py").write_text(
        "".join(f"LEGAL_{n:02} = {n}\n" for n in range(20)) + "LEGAL_PRIVACY_VERSION = '3'\n",
        encoding="utf-8",
    )
    (root / "api" / "routes.py").write_text(
        "from api.settings import LEGAL_PRIVACY_VERSION\n"
        "def version():\n"
        "    return LEGAL_PRIVACY_VERSION\n",
        encoding="utf-8",
    )
    index = Index(update(Cache(root, tmp_path / "cache"), load_config(root)))
    routes = [{"method": "GET", "path": "/version", "file": "api/routes.py",
               "handler": "api.routes.version", "line": 2}]
    found, truncated = cli.routes_reading(index, "legal", routes)
    assert [name for _, name in found] == ["api.settings.LEGAL_PRIVACY_VERSION"] and not truncated
    # past the bound, the answer says it is partial rather than claiming none
    monkeypatch.setattr(cli, "MAX_NAMES_READ", 5)
    assert cli.routes_reading(index, "legal", routes) == ([], True)
    lines = cli._routes_reading_table("legal", *cli.routes_reading(index, "legal", routes), 0)
    assert lines[0] == "no route handler reads a name containing legal"
    assert "the search stopped early" in lines[1]


def test_the_name_read_fallback_is_in_the_json_too(warm, monkeypatch):
    from codeterrain import cli

    monkeypatch.setattr(
        cli, "routes_reading", lambda index, text, routes: ([(routes[0], "m.LEGAL_V")], True)
    )
    code, out, _ = warm("routes", "zzz_nothing", "--json")
    data = json.loads(out)
    assert code == 0 and data["rows"] == []
    (row,) = data["handlers_reading"]
    assert row["reads"] == ["m.LEGAL_V"] and {"method", "path", "handler"} <= set(row)
    assert data["handlers_reading_truncated"] is True
    _, out, _ = warm("routes", "items", "--json")
    assert "handlers_reading" not in json.loads(out)
