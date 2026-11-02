"""Output budget: text tables cut at about N tokens, estimated as one token per four characters."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

DEFAULT_BUDGET = 2000
CHARS_PER_TOKEN = 4
MAX_COLUMN_WIDTH = 60


#: C0 controls other than tab and newline, DEL, C1 controls, the bidi marks, overrides and
#: isolates, the Unicode line and paragraph separators, and lone surrogates (an undecodable file
#: name).
_UNSAFE = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069\u200e\u200f\u061c\u2028\u2029\ud800-\udfff]"
)


def terminal_safe(text: str) -> str:
    """Escape characters that a terminal would act on, so repository text cannot drive it.

    Fixture names, paths and error messages come from the scanned repository; an escape
    sequence in one could clear the screen or forge rows. Each unsafe character is shown as
    ``\\xNN`` or ``\\uNNNN``. Tab and newline stay, since the text output is line-based.
    """
    return _UNSAFE.sub(
        lambda m: f"\\x{ord(m[0]):02x}" if ord(m[0]) < 0x100 else f"\\u{ord(m[0]):04x}", text
    )


#: Everything ``_UNSAFE`` covers, plus tab, newline and carriage return.
_UNSAFE_IN_FIELD = re.compile(
    "[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069\u200e\u200f\u061c\u2028\u2029\ud800-\udfff]"
)


def field_safe(text: str) -> str:
    """Escape one value taken from the repository before it is placed in a line of output.

    ``terminal_safe`` runs on the finished output and has to keep the renderer's own newlines and
    tabs; a newline inside a fixture name or a route path would still start a forged row. A field
    therefore has every control escaped, layout characters included.
    """
    return _UNSAFE_IN_FIELD.sub(
        lambda m: f"\\x{ord(m[0]):02x}" if ord(m[0]) < 0x100 else f"\\u{ord(m[0]):04x}", text
    )


def estimate_tokens(text: str) -> int:
    return -(-len(text) // CHARS_PER_TOKEN)


def posix_path(path: str) -> str:
    """Output paths are repo-relative POSIX; Windows backslashes are normalised."""
    return path.replace("\\", "/")


def cut_lines(
    lines: Sequence[str],
    budget: int,
    hint: str,
    keep: int = 1,
    omitted: Callable[[int], str] | None = None,
    more: int = 0,
) -> str:
    """Join ``lines`` within ``budget`` tokens; the first ``keep`` lines (headers) always stay.

    When rows are dropped the last line reads ``+N more; narrow with <hint>``, or ``+N more:
    <omitted(shown)>; narrow with <hint>`` to say what was left out (``shown`` rows were kept).
    ``more`` rows follow ``lines`` that were never formatted, and are always dropped. A budget of
    0 or less means no limit.
    """
    text = "\n".join(lines)
    limit = budget * CHARS_PER_TOKEN
    if budget <= 0 or (not more and len(text) + 1 <= limit):
        return text
    # The detail of every row is the longest the detail of the rows left out can be.
    detail = f": {omitted(0)}" if omitted else ""
    total_rows = len(lines) - keep + more
    used = sum(len(line) + 1 for line in lines[:keep])
    shown = 0
    for line in lines[keep:]:
        remaining = total_rows - shown - 1
        footer = f"+{remaining} more{detail}; narrow with {hint}"
        if used + len(line) + 1 + (len(footer) + 1 if remaining else 0) > limit:
            break
        used += len(line) + 1
        shown += 1
    dropped = total_rows - shown
    kept = list(lines[: keep + shown])
    if dropped:
        detail = f": {omitted(shown)}" if omitted else ""
        kept.append(f"+{dropped} more{detail}; narrow with {hint}")
    return "\n".join(kept)


def render_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    budget: int,
    hint: str,
    omitted: Callable[[int], str] | None = None,
) -> str:
    """Column-aligned text table (last column unpadded), cut to the budget."""
    if budget > 0:
        # Every row costs at least its newline (trailing empty cells are stripped), so no row
        # past this many can fit: those are counted, never formatted. A table cut this way is
        # aligned on the rows that could be shown.
        fits = budget * CHARS_PER_TOKEN
        if len(rows) > fits:
            cut = render_table(headers, rows[:fits], 0, hint)
            return cut_lines(
                cut.split("\n"), budget, hint, omitted=omitted, more=len(rows) - fits
            )
    cells = [[field_safe(posix_path(str(c))) for c in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in cells:
        for i, cell in enumerate(row[:-1]):
            widths[i] = max(widths[i], min(len(cell), MAX_COLUMN_WIDTH))

    def fmt(row: Sequence[str]) -> str:
        parts = [cell.ljust(widths[i]) for i, cell in enumerate(row[:-1])]
        parts.append(row[-1])
        return "  ".join(parts).rstrip()

    return cut_lines([fmt(headers), *(fmt(r) for r in cells)], budget, hint, omitted=omitted)
