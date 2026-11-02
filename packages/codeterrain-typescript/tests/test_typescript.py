from __future__ import annotations

import importlib.util
import inspect
import json
import random
import time
from pathlib import Path

import codeterrain_typescript
import pytest
from codeterrain_typescript import language, read_calls

from codeterrain import calls_static, languages
from codeterrain.cli import main

CORE_TESTS = Path(__file__).parents[3] / "tests" / "test_calls_static.py"


def calls(source: str, suffix: str = ".tsx") -> list[tuple[str, str, int, str]]:
    facts = read_calls(source, suffix)
    assert facts["error"] is None
    return [
        (c["method"], c["path"] or "?" + c["raw"], c["line"], c["style"]) for c in facts["calls"]
    ]


# --- the core's tokenizer cases ------------------------------------------------------------------

#: Cases of the core's tokenizer whose expectation this reader changes, each asserted below.
CHANGED = {
    # scopes: each function's own `const url` is read
    "test_a_constant_declared_twice_with_different_values_is_not_resolved",
    # `'/v1' as string + '/items'` is a sum of two strings, read whole
    "test_an_assertion_followed_by_more_expression_leaves_the_constant_unresolved",
    # unterminated text and an invalid escape are syntax errors, reported with their lines
    "test_an_unterminated_string_or_template_does_not_raise",
    "test_escapes_in_strings_and_templates_are_decoded",
    # the parser is linear: thousands of nested calls are read, not refused
    "test_thousands_of_nested_calls_are_refused_quickly_not_read_in_quadratic_time",
}


def _core_cases() -> list[tuple[str, object]]:
    spec = importlib.util.spec_from_file_location("core_calls_cases", CORE_TESTS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.parse_source = lambda source: read_calls(source, ".tsx")
    return [
        (name, fn) for name, fn in inspect.getmembers(module, inspect.isfunction)
        if name.startswith("test_") and not inspect.signature(fn).parameters
    ]


@pytest.mark.skipif(not CORE_TESTS.is_file(), reason="run from a checkout of codeterrain")
def test_the_core_tokenizer_cases_hold_except_the_changed_ones():
    cases = _core_cases()
    assert CHANGED <= {name for name, _ in cases}
    for name, case in cases:
        if name not in CHANGED:
            case()


def test_scopes_read_each_function_s_own_constant():
    source = (
        "function a() {\n  const url = '/a';\n  return fetch(url);\n}\n"
        "function b() {\n  const url = '/b';\n  return fetch(url);\n}\n"
        "const SAME = '/same';\nconst SAME = '/same';\nfetch(SAME);\n"
    )
    assert calls(source) == [
        ("GET", "/a", 3, "fetch"), ("GET", "/b", 7, "fetch"), ("GET", "/same", 11, "fetch"),
    ]


def test_an_assertion_inside_a_sum_is_read_through():
    source = (
        "const endpoint = '/v1' as string + '/items';\n"
        "const pick = '/v1' as string ? '/v2' : '/v3';\n"
        "fetch(endpoint);\n"
        "fetch(pick);\n"
    )
    assert calls(source) == [("GET", "/v1/items", 3, "fetch"), ("GET", "?pick", 4, "fetch")]


def test_a_syntax_error_is_reported_and_a_call_in_it_is_unresolved():
    for source in ("apiClient.get('/v1/x\n", "fetch(`/v1/${a", "apiClient.get<"):
        facts = read_calls(source, ".tsx")
        assert facts["error"].startswith("syntax errors at line 1")
        assert all(not c["resolved"] for c in facts["calls"])
    facts = read_calls("fetch('/v1/ok');\nfetch('/v1/\\uZZZZ');\n", ".ts")
    assert facts["error"] == "syntax errors at line 2; calls there are unresolved"
    assert [(c["path"], c["resolved"]) for c in facts["calls"]] == [
        ("/v1/ok", True), ("", False),
    ]


def test_escapes_are_decoded():
    source = (
        "fetch('/v1/\\u0069tems');\n"
        "fetch(\"/v1/\\x6frders\");\n"
        "fetch(`/v1/\\u{6c}ines/${id}`);\n"
        "fetch('/v1/a\\\n"
        "b');\n"
    )
    assert calls(source) == [
        ("GET", "/v1/items", 1, "fetch"),
        ("GET", "/v1/orders", 2, "fetch"),
        ("GET", "/v1/lines/{}", 3, "fetch"),
        ("GET", "/v1/ab", 4, "fetch"),
    ]


def test_thousands_of_nested_calls_are_read_in_linear_time():
    nested = "function f() { return " + "api.get(" * 3000 + "'/v1/x'" + ")" * 3000 + "; }\n"
    started = time.perf_counter()
    facts = read_calls(nested, ".ts")
    assert facts["error"] is None and len(facts["calls"]) == 3000
    assert [c["path"] for c in facts["calls"] if c["resolved"]] == ["/v1/x"]
    assert time.perf_counter() - started < 5


# --- scopes --------------------------------------------------------------------------------------


def test_a_constant_inside_an_interpolated_function_is_that_function_s():
    # The acceptance case of the tree-sitter reader.
    source = (
        'const url = "/outer";\n'
        'const s = `${(() => { const url = "/inner"; return fetch(url); })()}`;\n'
        "fetch(url);\n"
    )
    assert calls(source) == [("GET", "/inner", 2, "fetch"), ("GET", "/outer", 3, "fetch")]


def test_a_parameter_or_block_binding_hides_a_constant_only_where_it_is_bound():
    source = (
        "const url = '/v1/default';\n"
        "export function load(url: string) { return fetch(url); }\n"
        "fetch(url);\n"
        "if (ok) { const url = '/v1/block'; fetch(url); }\n"
        "for (const url of urls) { fetch(url); }\n"
        "try { go(); } catch (url) { fetch(url); }\n"
        "fetch(url);\n"
    )
    assert calls(source) == [
        ("GET", "?url", 2, "fetch"),
        ("GET", "/v1/default", 3, "fetch"),
        ("GET", "/v1/block", 4, "fetch"),
        ("GET", "?url", 5, "fetch"),
        ("GET", "?url", 6, "fetch"),
        ("GET", "/v1/default", 7, "fetch"),
    ]


def test_a_var_belongs_to_its_function_and_an_import_is_not_a_constant():
    source = (
        "import { BASE } from './config';\n"
        "import API from './api';\n"
        "const url = '/v1/outer';\n"
        "function load() {\n"
        "  if (x) { var url = '/v1/var'; }\n"
        "  return fetch(url);\n"
        "}\n"
        "fetch(`${BASE}/items`);\n"
        "fetch(`${API}/orders`);\n"
        "function each() { for (var url of urls) {} fetch(url); }\n"
        "function keys() { for (var url in map) {} fetch(url); }\n"
        "function scoped() { for (let url of urls) {} fetch(url); }\n"
    )
    assert calls(source) == [
        ("GET", "?url", 6, "fetch"),
        ("GET", "/items", 8, "fetch"),
        ("GET", "/orders", 9, "fetch"),
        ("GET", "?url", 10, "fetch"),
        ("GET", "?url", 11, "fetch"),
        ("GET", "/v1/outer", 12, "fetch"),
    ]


def test_a_method_held_in_a_constant_is_read():
    source = (
        "const POST = 'post';\n"
        "fetch('/v1/a', { method: POST });\n"
        "axios({ url: '/v1/b', method: 'purge' });\n"
    )
    assert calls(source) == [("POST", "/v1/a", 2, "fetch"), ("?", "/v1/b", 3, "object")]


# --- grammars ------------------------------------------------------------------------------------


def test_each_suffix_is_read_with_its_grammar():
    # `<T>value` is an assertion in .ts and an element in .tsx; JSX in a .js file.
    assert calls("const u = <string>'/v1/a';\nfetch(u);\n", ".ts") == [("GET", "/v1/a", 2, "fetch")]
    assert calls(
        "const El = () => <button onClick={() => fetch('/v1/b')}>Don't</button>;\n", ".js"
    ) == [("GET", "/v1/b", 1, "fetch")]
    for suffix in language.call_suffixes:
        assert calls("api.post('/v1/c', body);\n", suffix) == [("POST", "/v1/c", 1, "client")]


def test_an_awaited_call_with_type_arguments_is_read():
    # The grammar reads `await api.get<T>(...)` as a call of `await api.get`.
    source = (
        "const load = async (id: string) => {\n"
        "  const r = await apiClient.get<Result>(`/v1/teams/${id}/tasks`);\n"
        "  const s = await fetch<Other>('/v1/b');\n"
        "  return (await getClient()).get<T>('/v1/c');\n"
        "};\n"
    )
    assert calls(source, ".ts") == [
        ("GET", "/v1/teams/{}/tasks", 2, "client"), ("GET", "/v1/b", 3, "fetch"),
    ]


def test_a_bare_ampersand_in_jsx_text_is_text_not_a_syntax_error():
    source = (
        "const Page = () => (\n"
        "  <div onClick={() => fetch('/v1/a')}>\n"
        "    <p>Staff & privilege levels</p>\n"
        "    {render({ url: '/v1/b', label: <b>Retry & cancel</b> })}\n"
        "  </div>\n"
        ");\n"
    )
    assert calls(source) == [("GET", "/v1/a", 2, "fetch"), ("GET", "/v1/b", 4, "object")]
    assert calls(source, ".jsx") == [("GET", "/v1/a", 2, "fetch"), ("GET", "/v1/b", 4, "object")]


def test_discovering_the_pack_does_not_load_tree_sitter():
    # Every terrain command discovers the packs; only the ones reading front-end files need the
    # parser.
    import subprocess
    import sys

    code = (
        "import sys, codeterrain_typescript as p; assert 'tree_sitter' not in sys.modules; "
        "p.read_calls('fetch(\"/v1/a\");', '.ts'); assert 'tree_sitter' in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


# --- bounds --------------------------------------------------------------------------------------


def test_a_file_over_the_size_bound_is_not_read():
    facts = read_calls("x" * (codeterrain_typescript.MAX_FILE_BYTES + 1), ".ts")
    assert facts == {"calls": [], "error": "file larger than 2000000 bytes; not read"}


def test_a_constant_that_doubles_at_every_declaration_stays_cheap_and_the_bound_holds(
    monkeypatch,
):
    lines = ["const p0 = '/a';"] + [f"const p{n} = p{n - 1} + p{n - 1};" for n in range(1, 60)]
    source = "\n".join([*lines, "fetch(p59);", "fetch(p3);"]) + "\n"
    assert calls(source, ".ts") == [("GET", "?p59", 61, "fetch"), ("GET", "/a" * 8, 62, "fetch")]
    # past the bound, the file is an error, not a slow answer
    monkeypatch.setattr(codeterrain_typescript, "WORK_FLOOR", 50)
    monkeypatch.setattr(codeterrain_typescript, "WORK_PER_BYTE", 0)
    facts = read_calls(source, ".ts")
    assert facts["calls"] == [] and "work bound" in facts["error"]


@pytest.mark.parametrize("seed", range(4))
def test_random_and_hostile_input_never_raises(seed):
    rng = random.Random(seed)
    alphabet = "`'\"${}()[]<>/\\=+;:,.!?\n abcfetchurlgetpost0123" + chr(0) + " \udcff"
    for _ in range(50):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randrange(1, 400)))
        for suffix in (".ts", ".tsx", ".js"):
            facts = read_calls(text, suffix)
            assert set(facts) == {"calls", "error"}
            assert all(calls_static._pack_call(c) for c in facts["calls"])


# --- through the core ----------------------------------------------------------------------------


def test_the_core_reads_front_end_calls_through_the_pack(tmp_path, capsys):
    (found,) = [lang for lang in languages.discover()[0] if lang.name == "typescript"]
    assert found.read is None and set(found.call_suffixes) == set(calls_static.SUFFIXES)
    root = tmp_path / "web"
    (root / "src").mkdir(parents=True)
    (root / "src" / "orders.ts").write_text(
        "function a() { const url = '/v1/a'; return fetch(url); }\n"
        "function b() { const url = '/v1/b'; return fetch(url); }\n"
        "fetch(`/v1/${\n",
        encoding="utf-8",
    )
    code = main(["calls", "--json", "--root", str(root), "--cache-dir", str(tmp_path / "c")])
    data = json.loads(capsys.readouterr().out)
    assert code == 0
    assert [(c["path"], c["line"]) for c in data["rows"] if c["resolved"]] == [
        ("/v1/a", 1), ("/v1/b", 2),
    ]
    assert data["errors"] == [
        {"file": "src/orders.ts", "error": "syntax errors at line 3; calls there are unresolved"},
    ]
