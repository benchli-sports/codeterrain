from __future__ import annotations

import re

from codeterrain.budget import (
    CHARS_PER_TOKEN,
    cut_lines,
    estimate_tokens,
    field_safe,
    posix_path,
    render_table,
    terminal_safe,
)


def _rows(n):
    return [[f"name{i}", f"tests/file{i}.py:{i}"] for i in range(n)]


def test_token_estimate_is_one_per_four_chars():
    assert CHARS_PER_TOKEN == 4
    assert estimate_tokens("a" * 8) == 2
    assert estimate_tokens("a" * 9) == 3


def test_short_table_is_not_cut():
    out = render_table(["name", "where"], _rows(3), 2000, "a filter")
    assert "more; narrow with" not in out
    assert len(out.splitlines()) == 4


def test_long_table_is_cut_within_budget_with_exact_footer():
    rows = _rows(200)
    out = render_table(["name", "where"], rows, 100, "a name substring")
    lines = out.splitlines()
    assert len(out) <= 100 * CHARS_PER_TOKEN
    assert lines[0].startswith("name")
    match = re.fullmatch(r"\+(\d+) more; narrow with a name substring", lines[-1])
    assert match
    shown = len(lines) - 2
    assert shown > 0
    assert shown + int(match.group(1)) == 200


def test_rows_past_what_any_budget_can_show_are_counted_not_formatted():
    # More rows than the budget has characters: the ones past it are never formatted, and the
    # footer still counts every row, with the detail of what was left out.
    class Cell(str):
        formatted = 0

        def __str__(self):
            Cell.formatted += 1
            return str.__str__(self)

    rows = [[Cell(f"n{i}"), Cell("x")] for i in range(5000)]
    out = render_table(["name", "w"], rows, 10, "a filter", omitted=lambda shown: "rows")
    lines = out.splitlines()
    match = re.fullmatch(r"\+(\d+) more: rows; narrow with a filter", lines[-1])
    assert match and len(lines) - 2 + int(match.group(1)) == 5000
    assert Cell.formatted <= 2 * 10 * CHARS_PER_TOKEN
    small = render_table(["name", "w"], rows[:3], 10, "a filter")
    assert small == "name  w\nn0    x\nn1    x\nn2    x"


def test_zero_budget_means_no_limit():
    out = render_table(["name", "where"], _rows(500), 0, "x")
    assert len(out.splitlines()) == 501


def test_header_survives_a_budget_too_small_for_any_row():
    out = cut_lines(["h" * 40, "r" * 40, "r" * 40], 10, "x")
    assert out.splitlines() == ["h" * 40, "+2 more; narrow with x"]


def test_a_cut_footer_says_what_it_left_out():
    rows = [["a" * 30]] * 5 + [["b" * 30]] * 20
    seen = []

    def omitted(shown):
        seen.append(shown)
        return f"{25 - shown} rows"

    out = render_table(["h"], rows, 40, "x", omitted=omitted)
    lines = out.splitlines()
    assert len(out) <= 40 * CHARS_PER_TOKEN
    shown = len(lines) - 2
    assert lines[-1] == f"+{25 - shown} more: {25 - shown} rows; narrow with x"
    assert seen[-1] == shown


def test_windows_backslashes_become_posix():
    assert posix_path("tests\\api\\conftest.py") == "tests/api/conftest.py"
    out = render_table(["f"], [["tests\\a.py:1"]], 100, "x")
    assert "\\" not in out


def test_terminal_controls_are_escaped_and_tabs_and_newlines_stay():
    hostile = "a\x1b[2J\x1b[Hb\x07\x7f\x9b\u202e\u2066c\td\ne"
    assert terminal_safe(hostile) == (
        "a\\x1b[2J\\x1b[Hb\\x07\\x7f\\x9b\\u202e\\u2066c\td\ne"
    )
    assert terminal_safe("plain ascii, ünïcode") == "plain ascii, ünïcode"


def test_bidi_marks_line_separators_and_lone_surrogates_are_escaped():
    # Security review r2 P3-5 and P3-6: RLM, ALM and U+2028 printed raw, and a lone surrogate
    # from an undecodable file name crashed the UTF-8 writer on every run.
    hostile = "a\u200e\u200f\u061c\u2028\u2029\udc80b"
    expected = "a\\u200e\\u200f\\u061c\\u2028\\u2029\\udc80b"
    assert terminal_safe(hostile) == field_safe(hostile) == expected


def test_a_field_has_its_newlines_and_tabs_escaped_too():
    # A newline inside a field started a forged row.
    assert field_safe("safe\nFORGED\trow\r\x1b") == "safe\\x0aFORGED\\x09row\\x0d\\x1b"
    out = render_table(["name", "file"], [["a\nFORGED  x", "f.py:1"]], 100, "x")
    assert len(out.splitlines()) == 2 and "a\\x0aFORGED" in out
