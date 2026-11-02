from __future__ import annotations

import json

import pytest

from codeterrain.cache import Cache
from codeterrain.calls_static import (
    build,
    is_skipped_client_file,
    parse_source,
    select,
    tokenize,
)
from codeterrain.cli import main
from codeterrain.config import load_config


def calls(source: str) -> list[tuple[str, str, int, str]]:
    facts = parse_source(source)
    assert facts["error"] is None
    return [
        (c["method"], c["path"] or "?" + c["raw"], c["line"], c["style"]) for c in facts["calls"]
    ]


def test_client_verbs_with_nested_generics_across_lines():
    source = (
        "export const list = () => apiClient.get<Paginated<Order<Line>>>('/v1/orders');\n"
        "export function one(id: string) {\n"
        "  return apiClient\n"
        "    .patch<Order>(\n"
        "      `/v1/orders/${id}`,\n"
        "      body,\n"
        "    );\n"
        "}\n"
    )
    assert calls(source) == [
        ("GET", "/v1/orders", 1, "client"),
        ("PATCH", "/v1/orders/{}", 4, "client"),
    ]


def test_nested_templates_comments_and_type_only_mentions_are_not_calls():
    source = (
        "// apiClient.get('/v1/in-a-line-comment')\n"
        "/* apiClient.post('/v1/in-a-block-comment') */\n"
        "type Getter = typeof apiClient.get;\n"
        "const label = `apiClient.get('/v1/in-a-template')`;\n"
        "const s = 'apiClient.get(\"/v1/in-a-string\")';\n"
        "apiClient.get(`/v1/orders/${id}/lines?expand=${`a${b}`}`);\n"
    )
    assert calls(source) == [("GET", "/v1/orders/{}/lines?expand={}", 6, "client")]


def test_any_receiver_counts_only_with_a_path_and_known_clients_count_unresolved():
    source = (
        "cache.get('key');\n"
        "params.get(name);\n"
        "this.http.delete('/v1/tokens/' + id);\n"
        "api.post(endpoint, body);\n"
        "other.put(endpoint);\n"
        "client.get('https://geo.example.org/json/');\n"
    )
    assert calls(source) == [
        ("DELETE", "/v1/tokens/{}", 3, "client"),
        ("POST", "?endpoint", 4, "client"),
    ]


def test_fetch_reads_the_method_defaults_to_get_and_skips_absolute_urls():
    source = (
        "fetch('/v1/health');\n"
        "fetch(`/v1/files/${id}`, { method: 'DELETE', headers });\n"
        "window.fetch('/v1/a', { body });\n"
        "fetch('https://cdn.example.org/x.json');\n"
        "fetch(url, options);\n"
        "async function fetch(x) {}\n"
        "client.fetch('/not/a/global');\n"
    )
    assert calls(source) == [
        ("GET", "/v1/health", 1, "fetch"),
        ("DELETE", "/v1/files/{}", 2, "fetch"),
        ("GET", "/v1/a", 3, "fetch"),
        ("?", "?url", 5, "fetch"),
    ]


def test_object_literals_with_a_url_path_and_a_method():
    source = (
        "dataProvider.custom({ url: `/reports/${id}`, method: 'post', config: {} });\n"
        "axios({ method: 'put', url: '/v1/items/' + id });\n"
        "const link = { url: 'https://example.org' };\n"
        "dataProvider.custom({ url: endpoint });\n"
    )
    assert calls(source) == [
        ("POST", "/reports/{}", 1, "object"),
        ("PUT", "/v1/items/{}", 2, "object"),
    ]


def test_same_file_constants_and_a_leading_base_url_resolve():
    source = (
        "const BASE = `${import.meta.env.VITE_API}/v1`;\n"
        "const ORDERS = '/orders';\n"
        "const ITEMS = BASE + '/items';\n"
        "fetch(`${BASE}${ORDERS}`, { method: 'POST' });\n"
        "apiClient.get(ORDERS);\n"
        "fetch(ITEMS);\n"
        "fetch(`${API_URL}/v1/users`);\n"
    )
    assert calls(source) == [
        ("POST", "/v1/orders", 4, "fetch"),
        ("GET", "/orders", 5, "client"),
        ("GET", "/v1/items", 6, "fetch"),
        ("GET", "/v1/users", 7, "fetch"),
    ]


def test_regex_literals_jsx_text_and_division_do_not_derail_the_tokenizer():
    source = (
        r"const re = /\/v1\/['`]fake/g;" "\n"
        "const half = total / 2 / count;\n"
        "const el = <p title=\"x\">Don't stop / here</p>;\n"
        "const self = <Icon />;\n"
        "apiClient.get('/v1/after');\n"
    )
    assert calls(source) == [("GET", "/v1/after", 5, "client")]
    assert [t.line for t in tokenize("a\n`x\n${b}\ny`\nc")] == [1, 2, 5]


def test_an_unterminated_string_or_template_does_not_raise():
    assert parse_source("apiClient.get('/v1/x\n")["error"] is None
    assert parse_source("fetch(`/v1/${a")["error"] is None
    assert parse_source("apiClient.get<")["error"] is None


@pytest.mark.parametrize(
    ("rel", "excluded"),
    [
        ("src/api/orders.ts", False),
        ("src/api/orders.test.ts", True),
        ("src/api/orders.spec.tsx", True),
        ("src/Button.stories.tsx", True),
        ("src/types/env.d.ts", True),
        ("src/__tests__/api.ts", True),
        ("src/__mocks__/api.ts", True),
        ("src/mocks/handlers.ts", True),
        ("e2e/login.ts", True),
        ("src/testing.ts", False),
        ("frontend/out/_next/static/chunks/app.js", True),
        ("vendor/chart.min.js", True),
        ("coverage/lcov-report/sorter.js", True),
        ("src/app/[locale]/[[...id]]/page.tsx", False),
    ],
)
def test_test_declaration_and_build_output_files_are_not_scanned(rel, excluded):
    assert is_skipped_client_file(rel) is excluded


def _client_repo(tmp_path):
    root = tmp_path / "web"
    (root / "src" / "api").mkdir(parents=True)
    (root / "src" / "api" / "orders.ts").write_text(
        "export const list = () => apiClient.get('/v1/orders');\n", encoding="utf-8"
    )
    (root / "src" / "api" / "orders.test.ts").write_text(
        "apiClient.get('/v1/from-a-test');\n", encoding="utf-8"
    )
    (root / "node_modules" / "lib").mkdir(parents=True)
    (root / "node_modules" / "lib" / "index.js").write_text(
        "axios.get('/v1/vendored');\n", encoding="utf-8"
    )
    return root


def test_the_view_skips_tests_and_vendored_code_and_rebuilds_one_changed_file(tmp_path):
    root = _client_repo(tmp_path)
    cache = Cache(root, tmp_path / "cache")
    record, built = build(cache, load_config(root))
    assert built
    assert [(c["path"], c["file"]) for c in record.data["calls"]] == [
        ("/v1/orders", "src/api/orders.ts")
    ]
    assert build(Cache(root, tmp_path / "cache"), load_config(root))[1] is False
    # written signed, so the warm read above skipped the row-by-row check
    assert record.verified
    (root / "src" / "api" / "orders.ts").write_text(
        "apiClient.post('/v1/orders', body);\n", encoding="utf-8"
    )
    record, built = build(Cache(root, tmp_path / "cache"), load_config(root))
    assert built and [c["method"] for c in record.data["calls"]] == ["POST"]


def test_client_paths_limit_the_scan(tmp_path):
    root = _client_repo(tmp_path)
    (root / "scripts").mkdir()
    (root / "scripts" / "seed.js").write_text("fetch('/v1/seed');\n", encoding="utf-8")
    (root / "codeterrain.toml").write_text("", encoding="utf-8")
    config_file = tmp_path / "web.toml"
    config_file.write_text('[tool.codeterrain]\nclient_paths = ["src"]\n', encoding="utf-8")
    record, _ = build(Cache(root, tmp_path / "cache"), load_config(root, config_file))
    assert [c["file"] for c in record.data["calls"]] == ["src/api/orders.ts"]


def test_select_filters_by_substring_method_and_unresolved():
    data = {
        "calls": [
            {"method": "GET", "path": "/v1/orders", "file": "a.ts", "raw": "", "resolved": True},
            {"method": "POST", "path": "", "file": "b.ts", "raw": "endpoint", "resolved": False},
        ]
    }
    assert [c["file"] for c in select(data, "orders")] == ["a.ts"]
    assert [c["file"] for c in select(data, method="post")] == ["b.ts"]
    assert [c["file"] for c in select(data, unresolved=True)] == ["b.ts"]


def test_terrain_calls_table_and_json(tmp_path, capsys):
    root = _client_repo(tmp_path)
    (root / "src" / "api" / "users.ts").write_text(
        "api.post(endpoint, body);\n", encoding="utf-8"
    )
    argv = ["--root", str(root), "--cache-dir", str(tmp_path / "cache")]
    assert main(["calls", *argv]) == 0
    out = capsys.readouterr().out
    assert "GET     /v1/orders  src/api/orders.ts:1  client" in out
    assert "POST    ? endpoint  src/api/users.ts:1   client" in out
    assert main(["calls", "--unresolved", "--json", *argv]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["view"] == "calls"
    assert [(r["method"], r["raw"], r["resolved"]) for r in doc["rows"]] == [
        ("POST", "endpoint", False)
    ]
    assert main(["calls", "nothing-matches", *argv]) == 0
    assert capsys.readouterr().out.strip() == "no calls match"


def test_repository_text_in_a_call_cannot_drive_the_terminal(tmp_path, capsys):
    root = tmp_path / "web"
    root.mkdir()
    (root / "a.ts").write_text("apiClient.get('/v1/\x1b[2Jx\u202e');\n", encoding="utf-8")
    assert main(["calls", "--root", str(root), "--cache-dir", str(tmp_path / "cache")]) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\u202e" not in out


def test_type_arguments_may_hold_object_and_array_types():
    source = (
        "apiClient.get<{id: number}>('/v1/items');\n"
        "apiClient.get<Array<{ id: number; tags: string[] }>>('/v1/items/{}');\n"
        "apiClient.post<Record<string, {a: 1}[]>>('/v1/items', body);\n"
        "if (a < b) { apiClient.get('/v1/after'); }\n"
    )
    assert calls(source) == [
        ("GET", "/v1/items", 1, "client"),
        ("GET", "/v1/items/{}", 2, "client"),
        ("POST", "/v1/items", 3, "client"),
        ("GET", "/v1/after", 4, "client"),
    ]


def test_fetch_options_without_a_method_default_to_get():
    source = (
        "fetch('/v1/a', {});\n"
        "fetch('/v1/b', { headers });\n"
        "fetch('/v1/c', { ...opts });\n"
        "fetch('/v1/d', { ...opts, method: 'PUT' });\n"
        "fetch('/v1/e', opts);\n"
        "fetch('/v1/f', );\n"
    )
    assert calls(source) == [
        ("GET", "/v1/a", 1, "fetch"),
        ("GET", "/v1/b", 2, "fetch"),
        ("?", "/v1/c", 3, "fetch"),
        ("PUT", "/v1/d", 4, "fetch"),
        ("?", "/v1/e", 5, "fetch"),
        ("GET", "/v1/f", 6, "fetch"),
    ]


def test_a_constant_declared_twice_with_different_values_is_not_resolved():
    source = (
        "function a() {\n"
        "  const url = '/a';\n"
        "  return fetch(url);\n"
        "}\n"
        "function b() {\n"
        "  const url = '/b';\n"
        "  return fetch(url);\n"
        "}\n"
        "function c() {\n"
        "  const url = '/a';\n"
        "  return fetch(url);\n"
        "}\n"
        "const SAME = '/same';\n"
        "const SAME = '/same';\n"
        "fetch(SAME);\n"
    )
    assert calls(source) == [
        ("GET", "?url", 3, "fetch"),
        ("GET", "?url", 7, "fetch"),
        ("GET", "?url", 11, "fetch"),
        ("GET", "/same", 15, "fetch"),
    ]


def test_let_and_var_bindings_are_not_resolved_because_they_can_be_reassigned():
    source = (
        "let url = '/v1/a';\n"
        "url = '/v1/b';\n"
        "fetch(url);\n"
        "var other = '/v1/c';\n"
        "fetch(other);\n"
        "const fixed = '/v1/d';\n"
        "fetch(fixed);\n"
    )
    assert calls(source) == [
        ("GET", "?url", 3, "fetch"),
        ("GET", "?other", 5, "fetch"),
        ("GET", "/v1/d", 7, "fetch"),
    ]


def test_a_spread_after_a_field_makes_that_field_unknown():
    source = (
        "fetch('/v1/a', { method: 'POST', ...opts });\n"
        "fetch('/v1/b', { ...opts, method: 'PUT' });\n"
        "useCustom({ url: '/v1/c', method: 'POST', ...rest });\n"
        "useCustom({ ...rest, url: '/v1/d', method: 'DELETE' });\n"
    )
    assert calls(source) == [
        ("?", "/v1/a", 1, "fetch"),
        ("PUT", "/v1/b", 2, "fetch"),
        ("DELETE", "/v1/d", 4, "object"),
    ]


def test_calls_inside_a_template_interpolation_are_found_and_its_text_is_not():
    source = (
        "const BASE = '/v1';\n"
        "const label = `fetch('/nope') ${await fetch(`${BASE}/a`)}\n"
        "  and ${api.post('/v1/b', `${x ? `${await fetch('/v1/c')}` : ''}`)}`;\n"
        "fetch('/v1/d');\n"
    )
    assert calls(source) == [
        ("GET", "/v1/a", 2, "fetch"),
        ("POST", "/v1/b", 3, "client"),
        ("GET", "/v1/c", 3, "fetch"),
        ("GET", "/v1/d", 4, "fetch"),
    ]


def test_every_declarator_of_a_comma_separated_const_is_read():
    source = (
        "const BASE = '/v1', timeout = 5000, ITEMS = `${BASE}/items`;\n"
        "fetch(`${BASE}/orders`);\n"
        "fetch(ITEMS);\n"
    )
    assert calls(source) == [("GET", "/v1/orders", 2, "fetch"), ("GET", "/v1/items", 3, "fetch")]


def test_a_string_name_the_scanner_cannot_read_is_never_dropped_as_a_base_url():
    source = (
        "let base = '/v1';\n"
        "const PICK = flag ? '/v1' : '/v2';\n"
        "const API = import.meta.env.VITE_API;\n"
        "fetch(`${base}/a`);\n"
        "fetch(`${PICK}/b`);\n"
        "fetch(`${API}/c`);\n"
    )
    assert calls(source) == [
        ("GET", "?`${base}/a`", 4, "fetch"),
        ("GET", "?`${PICK}/b`", 5, "fetch"),
        ("GET", "/c", 6, "fetch"),
    ]


def test_thousands_of_nested_calls_are_refused_quickly_not_read_in_quadratic_time():
    # Each call re-split the arguments holding all the calls
    # nested in it, so one small file of nested calls held the parent CLI for seconds.
    import time

    nested = "function f() { return " + "api.get(" * 3000 + "'/v1/x'" + ")" * 3000 + "; }\n"
    objects = "const a = [" + ", ".join("{ url: '/v1/o', method: 'GET' }" for _ in range(3000))
    started = time.perf_counter()
    facts = parse_source(nested)
    assert facts["calls"] == [] and "work bound" in facts["error"]
    assert len(parse_source(objects + "];\n")["calls"]) == 3000  # many flat calls are fine
    assert time.perf_counter() - started < 5


def test_a_constant_shadowed_by_a_parameter_or_binding_is_not_resolved():
    # The outer value was read into a function whose parameter has the
    # same name, so a call whose path is its argument read as a fixed, resolved path.
    source = (
        "const url = '/v1/default';\n"
        "export function load(url: string) { return fetch(url); }\n"
        "const path = '/v1/p';\n"
        "list.forEach(path => fetch(path));\n"
        "const { item } = props;\n"
        "const item = '/v1/item';\n"
        "fetch(item);\n"
        "const BASE = '/v1';\n"
        "if (BASE) { fetch(`${BASE}/ok`); }\n"
    )
    assert calls(source) == [
        ("GET", "?url", 2, "fetch"),
        ("GET", "?path", 4, "fetch"),
        ("GET", "?item", 7, "fetch"),
        ("GET", "/v1/ok", 9, "fetch"),
    ]


def test_a_constant_that_doubles_at_every_declaration_stays_small():
    # `const pN = pM + pM` copied the whole expansion each time, so a
    # file of a few hundred bytes could take the CLI's memory.
    import time

    lines = ["const p0 = '/a';"] + [f"const p{n} = p{n - 1} + p{n - 1};" for n in range(1, 60)]
    started = time.perf_counter()
    facts = parse_source("\n".join([*lines, "fetch(p59);", "fetch(p3);"]) + "\n")
    assert facts["error"] is None
    assert [(c["path"] or "?" + c["raw"]) for c in facts["calls"]] == ["?p59", "/a" * 8]
    assert time.perf_counter() - started < 2


@pytest.mark.parametrize(
    "source",
    [
        "api.get < " * 4000 + "x;\n",  # every `<` looked ahead for its `>`
        "x" + ".get" * 8000 + ";\n",  # every `.get` walked back over the whole chain
        "const a: (" * 4000 + "\n",  # every annotation looked ahead for its `=`
    ],
    ids=["type-args", "receiver-chain", "annotations"],
)
def test_lookahead_and_lookbehind_are_charged_to_the_work_bound(source):
    # `_skip_type_args` scanned the rest of the file for every
    # `<` without charging the work bound, so a small file of comparisons ran in quadratic time.
    import time

    started = time.perf_counter()
    facts = parse_source(source)
    assert time.perf_counter() - started < 2
    assert facts["error"] is None or "work bound" in facts["error"]


def test_escapes_in_strings_and_templates_are_decoded():
    source = (
        "fetch('/v1/\\u0069tems');\n"
        "fetch(\"/v1/\\x6frders\");\n"
        "fetch(`/v1/\\u{6c}ines/${id}`);\n"
        "fetch('/v1/\\uZZZZ');\n"
        "fetch('/v1/a\\\n"
        "b');\n"
    )
    assert calls(source) == [
        ("GET", "/v1/items", 1, "fetch"),
        ("GET", "/v1/orders", 2, "fetch"),
        ("GET", "/v1/lines/{}", 3, "fetch"),
        ("GET", "/v1/uZZZZ", 4, "fetch"),
        ("GET", "/v1/ab", 5, "fetch"),
    ]


def test_a_constant_with_a_type_assertion_is_resolved():
    source = (
        "const BASE = '/v1' as const;\n"
        "const ITEMS = `${BASE}/items` satisfies string;\n"
        "fetch(`${BASE}/orders`);\n"
        "fetch(ITEMS);\n"
    )
    assert calls(source) == [("GET", "/v1/orders", 3, "fetch"), ("GET", "/v1/items", 4, "fetch")]


def test_an_assertion_followed_by_more_expression_leaves_the_constant_unresolved():
    # Cutting at `as` dropped `+ '/items'`, so the path read as `/v1`.
    source = (
        "const endpoint = '/v1' as string + '/items';\n"
        "const typed = '/v1/t' as Record<string, number>;\n"
        "fetch(endpoint);\n"
        "fetch(typed);\n"
    )
    assert calls(source) == [("GET", "?endpoint", 3, "fetch"), ("GET", "/v1/t", 4, "fetch")]


def test_an_assertion_followed_by_a_ternary_leaves_the_constant_unresolved():
    # Only `+` ended the asserted type, so `? '/v2' : '/v3'` was dropped
    # and the path read as `/v1`.
    source = (
        "const endpoint = '/v1' as string ? '/v2' : '/v3';\n"
        "fetch(endpoint);\n"
    )
    assert calls(source) == [("GET", "?endpoint", 2, "fetch")]


def test_an_initializer_continued_on_the_next_line_is_read_whole():
    # The value was cut at the line break, so `'/v1'` then
    # `.concat('/items')` read as `/v1`.
    source = (
        "const url = '/v1'\n"
        "  .concat('/items');\n"
        "const joined = '/v1' +\n"
        "  '/orders';\n"
        "const plain = '/v1/plain'\n"
        "fetch(url);\n"
        "fetch(joined);\n"
        "fetch(plain);\n"
    )
    assert calls(source) == [
        ("GET", "?url", 6, "fetch"),
        ("GET", "/v1/orders", 7, "fetch"),
        ("GET", "/v1/plain", 8, "fetch"),
    ]


def test_a_regex_inside_a_template_interpolation_does_not_close_it():
    # The `}` of `/}/` closed the `${`, leaving the call in the text.
    source = (
        "const label = `${/}/.test(value) ? await fetch('/v1/a') : ''}`;\n"
        "const half = `${total / 2} of ${fetch('/v1/b')}`;\n"
    )
    assert calls(source) == [("GET", "/v1/a", 1, "fetch"), ("GET", "/v1/b", 2, "fetch")]


def test_a_constant_may_use_one_declared_further_down_and_a_cycle_stays_unresolved():
    # `url` was read before `BASE` existed, so its path lost the prefix.
    source = (
        "function load() { const url = BASE + '/items'; return fetch(url); }\n"
        "const BASE = '/v1';\n"
        "const A = B + '/a';\n"
        "const B = A + '/b';\n"
        "fetch(`${A}/x`);\n"
    )
    assert calls(source) == [("GET", "/v1/items", 1, "fetch"), ("GET", "?`${A}/x`", 5, "fetch")]


def test_a_computed_key_in_the_options_counts_or_hides_the_method():
    # Computed keys were ignored, so `['method']: 'POST'` read as GET.
    source = (
        "axios({ url: '/v1/a', method: 'GET', ['method']: 'POST' });\n"
        "fetch('/v1/b', { [key]: value });\n"
        "fetch('/v1/c', { [key]: value, method: 'PUT' });\n"
    )
    assert calls(source) == [
        ("POST", "/v1/a", 1, "object"),
        ("?", "/v1/b", 2, "fetch"),
        ("PUT", "/v1/c", 3, "fetch"),
    ]


def test_a_let_or_var_of_the_same_name_hides_a_constant_even_without_a_string():
    # `let url = getUrl()` shadowed `const url` but kept its value.
    source = (
        "const url = '/outer';\n"
        "function load() { let url = getUrl(); return fetch(url); }\n"
        "let api = getBase();\n"
        "fetch(`${api}/items`);\n"
    )
    assert calls(source) == [("GET", "?url", 2, "fetch"), ("GET", "/items", 4, "fetch")]


def test_calls_in_a_template_keep_their_source_lines():
    # Lines were counted in the decoded, stripped parts, not the source.
    source = (
        "const a = `${\n"
        "  await fetch('/v1/a')}`;\n"
        "const b = `x\\ny ${fetch('/v1/b')}`;\n"
        "const c = `one\n"
        "two ${fetch('/v1/c')} ${\n"
        "\n"
        "fetch('/v1/d')}`;\n"
    )
    assert calls(source) == [
        ("GET", "/v1/a", 2, "fetch"),
        ("GET", "/v1/b", 3, "fetch"),
        ("GET", "/v1/c", 5, "fetch"),
        ("GET", "/v1/d", 7, "fetch"),
    ]


def test_a_constant_with_a_type_annotation_is_resolved():
    source = (
        "const BASE: string = '/v1';\n"
        "const MAP: Record<string, () => void> = {};\n"
        "const ITEMS: `/${string}` = `${BASE}/items`;\n"
        "fetch(`${BASE}/orders`);\n"
        "fetch(ITEMS);\n"
    )
    assert calls(source) == [("GET", "/v1/orders", 4, "fetch"), ("GET", "/v1/items", 5, "fetch")]


def test_an_apostrophe_in_jsx_text_does_not_open_a_string():
    source = (
        "const x = <div>Don't <button onClick={() => fetch('/v1/a')}>Go</button></div>;\n"
        "const y = isOn?'/v1/b':'/v1/c';\n"
        "fetch(y);\n"
    )
    assert calls(source) == [("GET", "/v1/a", 1, "fetch"), ("GET", "?y", 3, "fetch")]


def test_type_arguments_may_hold_parentheses_and_function_types():
    source = (
        "apiClient.get<(User | null)[]>('/v1/a');\n"
        "apiClient.post<{ done: () => void }>('/v1/b', body);\n"
        "if (api.get < limit(x)) { go('/v1/c'); }\n"
    )
    assert calls(source) == [
        ("GET", "/v1/a", 1, "client"),
        ("POST", "/v1/b", 2, "client"),
    ]


def test_an_object_call_with_a_spread_and_no_method_after_it_has_an_unknown_method():
    source = (
        "axios({ ...opts, url: '/v1/a' });\n"
        "axios({ url: '/v1/b', headers });\n"
    )
    assert calls(source) == [
        ("?", "/v1/a", 1, "object"),
        ("GET", "/v1/b", 2, "object"),
    ]


def test_a_shorthand_url_property_is_a_call_site():
    source = (
        "const url = '/v1/items';\n"
        "useCustom({ url, method: 'POST' });\n"
        "useCustom({ method: 'PUT', url });\n"
    )
    assert calls(source) == [
        ("POST", "/v1/items", 2, "object"),
        ("PUT", "/v1/items", 3, "object"),
    ]



def test_a_language_pack_reads_the_suffixes_it_offers_and_its_output_is_checked(tmp_path):
    from codeterrain.languages import Language

    def reader(name, version, read_calls, suffixes=(".ts",)):
        return Language(name, version, (), None, suffixes, read_calls)

    def good(source, suffix):
        return {"calls": [{"method": "PUT", "path": "/pack" + suffix, "line": 1,
                           "style": "fetch", "resolved": True, "raw": ""}], "error": None}

    root = tmp_path / "web"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.ts").write_text("fetch('/v1/a');\n", encoding="utf-8")
    (root / "src" / "b.js").write_text("fetch('/v1/b');\n", encoding="utf-8")
    config = load_config(root)

    def paths(*readers):
        record, _ = build(Cache(root, tmp_path / "cache"), config, readers=readers)
        return [(c["file"], c["method"], c["path"]) for c in record.data["calls"]], record.data

    # the pack reads .ts, the built-in reader the rest
    assert paths(reader("one", "1", good))[0] == [
        ("src/a.ts", "PUT", "/pack.ts"), ("src/b.js", "GET", "/v1/b"),
    ]
    # the same text under two suffixes is read by each one's reader, not cached as one
    (root / "src" / "a.js").write_text("fetch('/v1/a');\n", encoding="utf-8")
    assert paths(reader("one", "1", good))[0] == [
        ("src/a.js", "GET", "/v1/a"), ("src/a.ts", "PUT", "/pack.ts"),
        ("src/b.js", "GET", "/v1/b"),
    ]
    (root / "src" / "a.js").unlink()
    # removing the pack, or a new version of it, re-reads the files
    assert paths()[0] == [("src/a.ts", "GET", "/v1/a"), ("src/b.js", "GET", "/v1/b")]
    newer = reader("one", "2", lambda s, x: {**good(s, x), "calls": []})
    assert paths(newer)[0] == [("src/b.js", "GET", "/v1/b")]
    # a pack that raises or returns anything malformed gives a file error
    for broken, error in [
        (lambda s, x: 1 / 0, "ZeroDivisionError: division by zero"),
        (lambda s, x: {"calls": [{"method": "GET"}], "error": None}, "malformed calls"),
        (lambda s, x: {**good(s, x), "calls": [{**good(s, x)["calls"][0], "method": "BREW"}]},
         "malformed calls"),
        (lambda s, x: {**good(s, x), "calls": [{**good(s, x)["calls"][0], "resolved": False}]},
         "malformed calls"),
    ]:
        found, data = paths(reader("bad", repr(error), broken))
        assert found == [("src/b.js", "GET", "/v1/b")]
        assert data["errors"][0]["file"] == "src/a.ts" and error in data["errors"][0]["error"]
