from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

from codeterrain import calls_join
from codeterrain.calls_join import (
    best_route,
    join,
    load_clients,
    load_with_states,
    repo_state,
    score,
    segments,
    state_warning,
)
from codeterrain.config import ClientRepo


def route(method: str, path: str, handler: str = "h") -> dict:
    return {"method": method, "path": path, "app": "pkg.main:app", "handler": handler}


def call(method: str, path: str, resolved: bool = True) -> dict:
    return {"method": method, "path": path, "resolved": resolved, "raw": "", "line": 1,
            "file": "src/api.ts", "style": "client"}


def test_segments_drop_the_query_the_fragment_and_empty_parts():
    assert segments("/v1/orders/{}/?expand=a/b#top") == ["v1", "orders", "{}"]
    assert segments("/") == []
    assert segments("/v1//items/") == ["v1", "", "items"]


def test_an_empty_segment_inside_a_call_path_matches_no_literal_route():
    assert score("/v1//items", "/v1/items") is None
    assert score("/v1/items/", "/v1/items") is not None


def test_segment_counts_must_agree_unless_the_route_has_a_path_parameter():
    # The shortcut that skips the table: same answers as the full match.
    assert score("/v1/orders/{}/lines", "/v1/orders/{order_id}") is None
    assert score("/v1/orders", "/v1/orders/{order_id}") is None
    assert score("/v1/files/a/b/c", "/v1/files/{rest:path}") is not None
    assert score("/v1/files/a/b/raw", "/v1/files/{rest:path}/raw") is not None


def test_an_empty_segment_does_not_fill_a_route_parameter():
    # `/files//download` matched `/files/{name}/download`, which Starlette
    # does not route (a parameter takes at least one character).
    assert score("/files//download", "/files/{name}/download") is None
    assert score("/files/{}/download", "/files/{name}/download") is not None


@pytest.mark.parametrize(
    ("call_path", "route_path", "matches"),
    [
        ("/v1/orders", "/v1/orders", True),
        ("/v1/orders/", "/v1/orders", True),
        ("/v1/orders/{}", "/v1/orders/{order_id}", True),
        ("/v1/orders/abc", "/v1/orders/{order_id}", True),
        ("/v1/orders/{}/lines/mine:cancel", "/v1/orders/{id}/lines/mine:cancel", True),
        ("/v1/event-logs{}", "/v1/event-logs", True),
        ("/v1/files/{}.json", "/v1/files/{name}", True),
        ("/v1/files/a/b/c", "/v1/files/{rest:path}", True),
        ("/v1/files", "/v1/files/{rest:path}", False),
        ("/v1/files/a/b/download", "/v1/files/{rest:path}/download", True),
        ("/v1/files/a/download", "/v1/files/{rest:path}/download", True),
        ("/v1/files/a/delete", "/v1/files/{rest:path}/download", False),
        ("/v1/files/download", "/v1/files/{rest:path}/download", False),
        ("/v1/files/a/b/{}/x", "/v1/files/{rest:path}/{id}/x", True),
        ("/v1/orders", "/v1/orders/{order_id}", False),
        ("/v1/orders/{}/x", "/v1/orders/{order_id}", False),
        ("/v1/order", "/v1/orders", False),
        ("/v1/orders{}", "/v1/users", False),
        ("/v1/reports/annual.json", "/v1/reports/{name}.json", True),
        ("/v1/reports/{}.json", "/v1/reports/{name}.json", True),
        ("/v1/reports/{}", "/v1/reports/{name}.json", True),
        ("/v1/reports/v-2-x", "/v1/reports/v-{major}-{minor}", True),
        ("/v1/reports/annual.csv", "/v1/reports/{name}.json", False),
        ("/v1/reports/.json", "/v1/reports/{name}.json", False),
        ("/v1/reports/{}.csv", "/v1/reports/{name}.json", False),
    ],
)
def test_a_call_path_matches_a_route_segment_by_segment(call_path, route_path, matches):
    assert (score(call_path, route_path) is not None) is matches


@pytest.mark.parametrize(
    ("call_path", "route_path", "matches"),
    [
        ("/v1/{}a{}b", "/v1/xaab", True),
        ("/v1/a{}a", "/v1/a", False),
        ("/v1/a{}a", "/v1/aa", True),
        ("/v1/{}.{}", "/v1/x.y.z", True),
        ("/v1/x-y-z", "/v1/{a}-{b}", True),
        ("/v1/x-", "/v1/{a}-{b}", False),
        ("/v1/-y", "/v1/{a}-{b}", False),
        ("/v1/xy", "/v1/{a}{b}", True),
        ("/v1/x", "/v1/{a}{b}", False),
        # Wildcards on both sides, in different shapes.
        ("/reports/report-{}.json", "/reports/{name}.json", True),
        ("/reports/{}.csv", "/reports/{name}.json", False),
        ("/reports/a{}b", "/reports/{x}-{y}", True),
        ("/reports/{}", "/reports/v{major}", True),
        ("/reports/x{}", "/reports/v{major}", False),
        ("/reports/{}" + "a{}" * 40, "/reports/" + "{p}b" * 40, False),  # past MAX_OVERLAP
    ],
)
def test_wildcards_inside_a_segment(call_path, route_path, matches):
    assert (score(call_path, route_path) is not None) is matches


def test_many_wildcards_in_one_segment_never_backtrack():
    # `{}a{}a...` became a `.*a.*a...` regex that backtracked for
    # seconds against a long segment; the same held for parameters inside a route segment.
    import time

    started = time.perf_counter()
    assert score("/" + "{}a" * 15 + "z", "/" + "a" * 40 + "b") is None
    assert score("/" + "{}a" * 400 + "z", "/" + "a" * 1500 + "b") is None
    assert score("/" + "a" * 1500 + "b", "/" + "{p}a" * 300 + "z") is None
    assert time.perf_counter() - started < 1
    assert score("/" + "x" * 3000, "/{rest:path}") is None  # past MAX_PATH: no match at all


def test_a_route_of_many_path_parameters_is_matched_in_bounded_time():
    # Each `{x:path}` tried every split of the rest, so a route
    # of a dozen of them took seconds per non-matching call; the table makes it calls x route.
    import time

    route_path = "/" + "/".join(f"{{p{i}:path}}" for i in range(40)) + "/end"
    started = time.perf_counter()
    assert score("/" + "/".join(["x"] * 120), route_path) is None
    assert score("/" + "/".join(["x"] * 120) + "/end", route_path) is not None
    assert time.perf_counter() - started < 2
    assert score("/x" * 300, "/{rest:path}") is None  # past MAX_SEGMENTS: no match at all


def test_the_most_literal_route_wins_then_registration_order():
    routes = [
        route("GET", "/v1/orders/{order_id}", "one"),
        route("GET", "/v1/orders/mine", "mine"),
        route("GET", "/v1/{anything}/{other}", "catch_all"),
    ]
    assert best_route(call("GET", "/v1/orders/mine"), routes)["handler"] == "mine"
    assert best_route(call("GET", "/v1/orders/{}"), routes)["handler"] == "one"
    assert best_route(call("GET", "/v1/users/{}"), routes)["handler"] == "catch_all"
    first, second = route("GET", "/v1/x/{a}", "first"), route("GET", "/v1/x/{b}", "second")
    assert best_route(call("GET", "/v1/x/{}"), [first, second])["handler"] == "first"


def test_the_method_must_agree_unless_the_call_did_not_say():
    routes = [route("POST", "/v1/orders")]
    assert best_route(call("GET", "/v1/orders"), routes) is None
    assert best_route(call("?", "/v1/orders"), routes) is routes[0]
    assert best_route(call("POST", "", resolved=False), routes) is None


def test_a_head_call_hits_a_get_route_only_when_that_route_answers_head():
    plain, both = route("GET", "/v1/a"), {**route("GET", "/v1/b"), "also_head": True}
    assert best_route(call("HEAD", "/v1/a"), [plain]) is None
    assert best_route(call("HEAD", "/v1/b"), [both]) is both
    assert best_route(call("GET", "/v1/b"), [both]) is both


def test_a_call_of_unknown_method_never_hits_a_websocket_route():
    socket, http = route("WEBSOCKET", "/v1/events"), route("GET", "/v1/events")
    assert best_route(call("?", "/v1/events"), [socket]) is None
    assert best_route(call("?", "/v1/events"), [socket, http]) is http


def test_a_route_for_any_method_matches_every_call_method():
    routes = [route("ANY", "/v1/orders")]
    assert best_route(call("GET", "/v1/orders"), routes) is routes[0]
    assert best_route(call("DELETE", "/v1/orders"), routes) is routes[0]


def test_a_parameter_inside_a_segment_ties_a_bare_one_for_a_literal_call():
    routes = [
        route("GET", "/v1/reports/{name}", "bare"),
        route("GET", "/v1/reports/{name}.json", "json"),
    ]
    assert best_route(call("GET", "/v1/reports/annual.json"), routes)["handler"] == "bare"
    assert best_route(call("GET", "/v1/reports/{}.json"), routes)["handler"] == "json"


def test_clients_are_read_with_their_own_config_and_prefix(tmp_path):
    web = tmp_path / "web"
    (web / "src").mkdir(parents=True)
    (web / "src" / "api.ts").write_text(
        "apiClient.get(`/orders/${id}`);\napi.post(endpoint);\n", encoding="utf-8"
    )
    (web / "codeterrain.toml").write_text('path_prefix = "/v1"\n', encoding="utf-8")
    repos = (ClientRepo(web), ClientRepo(tmp_path / "missing"))
    clients = load_clients(repos, str(tmp_path / "cache"))
    assert [c.error for c in clients] == [None, "not a directory"]
    rows = join(clients, [route("GET", "/v1/orders/{order_id}")])
    assert [(r["repo"], r["path"], r["route"] and r["route"]["path"]) for r in rows] == [
        ("web", "/v1/orders/{}", "/v1/orders/{order_id}"),
        ("web", "", None),
    ]


def test_a_broken_client_config_is_reported_not_raised(tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "codeterrain.toml").write_text("bogus = 1\n", encoding="utf-8")
    (client,) = load_clients((ClientRepo(web),), str(tmp_path / "cache"))
    assert "unknown config key" in client.error and client.calls == []


def test_a_client_repos_entry_overrides_the_front_end_config(tmp_path):
    # A read-only checkout gets its prefix and scan paths from the backend side.
    web = tmp_path / "web"
    (web / "src").mkdir(parents=True)
    (web / "scripts").mkdir()
    (web / "src" / "api.ts").write_text("apiClient.get('/orders');\n", encoding="utf-8")
    (web / "scripts" / "seed.js").write_text("fetch('/seed');\n", encoding="utf-8")
    (web / "codeterrain.toml").write_text('path_prefix = "/v2"\n', encoding="utf-8")
    repo = ClientRepo(web, path_prefix="/v1", client_paths=("src",))
    (client,) = load_clients((repo,), str(tmp_path / "cache"))
    assert [c["path"] for c in client.calls] == ["/v1/orders"]
    (outside,) = load_clients(
        (ClientRepo(web, client_paths=("../elsewhere",)),), str(tmp_path / "cache")
    )
    assert "resolves outside" in outside.error



def test_a_join_splits_each_path_once_and_keeps_none_of_them(monkeypatch):
    # A process-wide cache of path splits kept every call path
    # of a front-end checkout, up to 65,536 of them, before the segment limit rejected any.
    split = []
    real = calls_join.segments

    def counting(path):
        split.append(path)
        return real(path)

    monkeypatch.setattr(calls_join, "segments", counting)
    calls = [
        {"method": "GET", "path": f"/v1/orders/{n}", "resolved": True} for n in range(3)
    ] + [{"method": "GET", "path": "/x" * 300, "resolved": True}]
    routes = [route("GET", "/v1/orders/{order_id}"), route("GET", "/v1/items")]
    rows = join([calls_join.Client(Path("web"), "web", calls=calls)], routes)
    assert [r["route"] and r["route"]["path"] for r in rows] == [
        "/v1/orders/{order_id}", "/v1/orders/{order_id}", "/v1/orders/{order_id}", None,
    ]
    assert len(split) == len(calls) + len(routes)
    assert not hasattr(calls_join._split, "cache_info")


def test_the_checkouts_states_are_asked_side_by_side(tmp_path, monkeypatch):
    # `git status` was run in one checkout after another and made most of a warm `callers`.
    backend, web, admin = tmp_path / "backend", tmp_path / "web", tmp_path / "admin"
    for root in (backend, web, admin):
        root.mkdir()
    together = threading.Barrier(3, timeout=10)

    def state(root):
        together.wait()  # breaks unless all three are asked at once
        return {"root": root.name}

    monkeypatch.setattr(calls_join, "repo_state", state)
    repos = (ClientRepo(web), ClientRepo(admin))
    clients, backend_state = load_with_states(repos, backend, str(tmp_path / "cache"))
    assert backend_state == {"root": "backend"}
    assert [c.state for c in clients] == [{"root": "web"}, {"root": "admin"}]


def test_a_missing_checkout_has_no_state(tmp_path):
    assert repo_state(tmp_path / "missing") is None


def git(*args, cwd):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd, check=True, capture_output=True,
    )


def _behind_and_dirty(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    git("init", "-q", "-b", "main", cwd=origin)
    (origin / "a.ts").write_text("apiClient.get('/a');\n", encoding="utf-8")
    git("add", ".", cwd=origin)
    git("commit", "-q", "-m", "one", cwd=origin)
    git("clone", "-q", str(origin), "web", cwd=tmp_path)
    (origin / "b.ts").write_text("apiClient.get('/b');\n", encoding="utf-8")
    git("add", ".", cwd=origin)
    git("commit", "-q", "-m", "two", cwd=origin)
    web = tmp_path / "web"
    git("fetch", "-q", cwd=web)
    (web / "a.ts").write_text("apiClient.get('/changed');\n", encoding="utf-8")
    (web / "wip.ts").write_text("apiClient.get('/wip');\n", encoding="utf-8")
    return web


def test_repo_state_reports_behind_and_uncommitted_without_fetching(tmp_path):
    web = _behind_and_dirty(tmp_path)
    state = repo_state(web)
    assert state == {
        "branch": "main", "upstream": "origin/main", "ahead": 0, "behind": 1,
        "default": "origin/main", "behind_default": 1, "dirty": 2,
    }
    assert state_warning("web", state) == (
        "web: 1 commits behind origin/main (as of its last fetch) and 2 uncommitted changes; "
        "a gap may be the checkout, not the code"
    )
    assert state_warning("web", {**state, "behind": 0, "dirty": 0}) is None
    plain = tmp_path / "plain"
    plain.mkdir()
    assert repo_state(plain) is None and state_warning("plain", None) is None


def test_a_feature_branch_level_with_its_upstream_is_still_behind_the_default_branch(tmp_path):
    # The false gap in the field: the front end was on a feature branch that matched its own
    # upstream, while the deployed default branch had moved on.
    web = _behind_and_dirty(tmp_path)
    git("checkout", "-q", "-b", "feature", cwd=web)
    git("push", "-q", "-u", "origin", "feature", cwd=web)
    state = repo_state(web)
    assert state["upstream"] == "origin/feature" and state["behind"] == 0
    assert state["default"] in ("origin/HEAD", "origin/main") and state["behind_default"] == 1
    assert "on feature, 1 commits behind origin/" in state_warning("web", state)


def test_repo_state_does_not_run_the_checkouts_fsmonitor_command(tmp_path):
    # core.fsmonitor in a checkout's own .git/config names a command `git status` would run; the
    # join reads checkouts the user listed, but never runs their configured commands.
    web = _behind_and_dirty(tmp_path)
    marker = tmp_path / "fsmonitor-ran"
    git("config", "core.fsmonitor", f"echo ran > {marker.as_posix()}", cwd=web)
    assert repo_state(web)["dirty"] == 2
    assert not marker.exists()
