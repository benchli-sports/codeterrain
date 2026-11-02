"""Draw the charts in docs/img from the measured ratios in docs/why-codeterrain.md and the README.

Run from the repository root: ``uv run python docs/charts/make_charts.py``. Each chart is written
twice, ``<name>-light.svg`` and ``<name>-dark.svg``; the docs pick one with ``<picture>``. Only
ratios and shares are plotted, never absolute counts.
"""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path(__file__).resolve().parent.parent / "img"
FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'
THEMES = {
    "light": {
        "surface": "#fcfcfb", "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#898781",
        "grid": "#e1e0d9", "axis": "#c3c2b7", "s1": "#2a78d6", "s2": "#eb6834",
    },
    "dark": {
        "surface": "#1a1a19", "ink": "#f0efec", "ink2": "#c3c2b7", "muted": "#898781",
        "grid": "#2c2c2a", "axis": "#383835", "s1": "#3987e5", "s2": "#d95926",
    },
}
WIDTH = 760
LABEL_W = 250  # row labels
RIGHT_PAD = 120  # room for value labels at the bar tips
BAR = 14  # bar thickness, under the 24px cap
GAP = 2  # surface gap between touching bars


def bar(x0: float, y: float, length: float, color: str) -> str:
    """A horizontal bar from the baseline: square at x0, 4px rounded at the data end."""
    x1, y1 = x0 + length, y + BAR
    r = min(4.0, length / 2, BAR / 2)
    return (
        f'<path d="M{x0:.1f} {y:.1f} H{x1 - r:.1f} Q{x1:.1f} {y:.1f} {x1:.1f} {y + r:.1f} '
        f'V{y1 - r:.1f} Q{x1:.1f} {y1:.1f} {x1 - r:.1f} {y1:.1f} H{x0:.1f} Z" fill="{color}"/>'
    )


def text(x: float, y: float, s: str, fill: str, size: int = 13, anchor: str = "start",
         weight: int = 400) -> str:
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" fill="{fill}" font-size="{size}" font-weight="{weight}" '
        f'text-anchor="{anchor}" dominant-baseline="middle">{escape(s)}</text>'
    )


def frame(title: str, subtitle: str, height: float, body: list[str], t: dict[str, str],
          desc: str) -> str:
    return "\n".join(
        [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height:.0f}" '
            f'viewBox="0 0 {WIDTH} {height:.0f}" font-family=\'{FONT}\' role="img">',
            f"<title>{escape(title)}</title>",
            f"<desc>{escape(desc)}</desc>",
            f'<rect width="{WIDTH}" height="{height:.0f}" rx="8" fill="{t["surface"]}"/>',
            text(24, 30, title, t["ink"], 16, weight=600),
            text(24, 52, subtitle, t["ink2"], 13),
            *body,
            "</svg>",
            "",
        ]
    )


def axis(x0: float, scale: float, ticks: list[float], top: float, bottom: float,
         t: dict[str, str], fmt) -> list[str]:
    out = []
    for v in ticks:
        x = x0 + v * scale
        color = t["axis"] if v == 0 else t["grid"]
        out.append(f'<line x1="{x:.1f}" y1="{top:.1f}" x2="{x:.1f}" y2="{bottom:.1f}" '
                   f'stroke="{color}" stroke-width="1"/>')
        out.append(text(x, bottom + 14, fmt(v), t["muted"], 11, "middle"))
    return out


def legend(items: list[tuple[str, str]], y: float, t: dict[str, str]) -> list[str]:
    out, x = [], 24.0
    for label, key in items:
        out.append(f'<rect x="{x:.1f}" y="{y - 5:.1f}" width="10" height="10" rx="2" '
                   f'fill="{t[key]}"/>')
        out.append(text(x + 16, y, label, t["ink2"], 12))
        x += 16 + len(label) * 6.6 + 24
    return out


# --- 1. characters an agent reads ---------------------------------------------------------------

TOKENS = [
    # (row label, x fewer characters with terrain)
    ("Backend: autouse fixtures of a test", 4.4),
    ("Backend: a route's handler and access", 6.9),
    ("Backend: who uses the main fixture", 1.5),
    ("Analytics: who uses the main fixture", 8.8),
    ("Analytics: fixtures of a test", 3.5),
    ("Small suite: fixtures of a test", 0.8),
]


def tokens_chart(t: dict[str, str]) -> str:
    x0, top, row = LABEL_W, 96.0, 2 * BAR + GAP + 18
    scale = (WIDTH - LABEL_W - RIGHT_PAD) / 125
    bottom = top + row * len(TOKENS) - 10
    body = legend([("grep and read, today (= 100)", "s2"), ("terrain", "s1")], 74, t)
    body += axis(x0, scale, [0, 25, 50, 75, 100, 125], top - 6, bottom, t, lambda v: f"{v:.0f}")
    for i, (label, ratio) in enumerate(TOKENS):
        y = top + i * row
        after = 100 / ratio
        body.append(text(24, y + BAR + GAP / 2, label, t["ink"], 13))
        body.append(bar(x0, y, 100 * scale, t["s2"]))
        body.append(text(x0 + 100 * scale + 6, y + BAR / 2, "100", t["ink2"], 11))
        body.append(bar(x0, y + BAR + GAP, after * scale, t["s1"]))
        note = f"{after:.0f}  ({ratio:g}x fewer)" if ratio >= 1 else f"{after:.0f}  (longer)"
        body.append(text(x0 + after * scale + 6, y + BAR + GAP + BAR / 2, note, t["ink"], 11,
                         weight=600))
    desc = "; ".join(f"{label}: grep and read 100, terrain {100 / r:.0f}" for label, r in TOKENS)
    return frame(
        "Characters an agent reads to get the answer",
        "Indexed: today's grep-and-read = 100. Lower is better.",
        bottom + 40, body, t, desc,
    )


# --- 2. accuracy ---------------------------------------------------------------------------------

ACCURACY = [
    # (row label, share in %, value label)
    ("Setup order equal to pytest --setup-plan", 100, "11 of 11 nodes"),
    ("Routes joined to the access manifest", 100, "every route"),
    ("Calls matched to a route, large backend", 97, "97%"),
    ("Calls matched to a route, smaller service", 96, "96%"),
    ("Calls found by refs, against grep, monorepo", 99, "99%"),
]


def accuracy_chart(t: dict[str, str]) -> str:
    label_w = 330
    x0, top, row = label_w, 80.0, BAR + 20
    scale = (WIDTH - label_w - RIGHT_PAD) / 100
    bottom = top + row * len(ACCURACY) - 10
    body = axis(x0, scale, [0, 25, 50, 75, 100], top - 6, bottom, t, lambda v: f"{v:.0f}%")
    for i, (label, share, value) in enumerate(ACCURACY):
        y = top + i * row
        body.append(text(24, y + BAR / 2, label, t["ink"], 13))
        body.append(bar(x0, y, share * scale, t["s1"]))
        body.append(text(x0 + share * scale + 6, y + BAR / 2, value, t["ink"], 11, weight=600))
    desc = "; ".join(f"{label}: {value}" for label, _, value in ACCURACY)
    return frame(
        "Accuracy against the framework's own answer",
        "Each figure checked against pytest, the access manifest, or by hand. Higher is better.",
        bottom + 40, body, t, desc,
    )


# --- 4. field use ---------------------------------------------------------------------------------

FIELD = [
    # (row label, share of logged lookups in %)
    ("Answered", 79),
    ("Partly answered", 12),
    ("No help", 2),
    ("Error (mostly the wrong command)", 7),
    ("Wrong answer", 0),
]


def field_chart(t: dict[str, str]) -> str:
    x0, top, row = LABEL_W, 80.0, BAR + 20
    scale = (WIDTH - LABEL_W - RIGHT_PAD) / 100
    bottom = top + row * len(FIELD) - 10
    body = axis(x0, scale, [0, 25, 50, 75, 100], top - 6, bottom, t, lambda v: f"{v:.0f}%")
    for i, (label, share) in enumerate(FIELD):
        y = top + i * row
        body.append(text(24, y + BAR / 2, label, t["ink"], 13))
        if share:
            body.append(bar(x0, y, share * scale, t["s1"]))
        body.append(text(x0 + share * scale + 6, y + BAR / 2, f"{share}%", t["ink"], 11,
                         weight=600))
    desc = "; ".join(f"{label}: {share}%" for label, share in FIELD)
    return frame(
        "Coding agents using terrain on real work",
        "Share of lookups the agents logged, by outcome, over their first days with it.",
        bottom + 40, body, t, desc,
    )


# --- 3. speed ------------------------------------------------------------------------------------

SPEED = [
    # (row label, warm seconds, Python start-up included)
    ("terrain fixtures", 0.35),
    ("terrain routes", 0.35),
    ("terrain refs", 0.36),
    ("terrain def", 0.45),
    ("terrain setup-order", 0.49),
    ("terrain impact", 1.17),
]
SPEED_MAX = 1.5


def speed_chart(t: dict[str, str]) -> str:
    x0, top, row = LABEL_W, 80.0, BAR + 20
    scale = (WIDTH - LABEL_W - RIGHT_PAD) / SPEED_MAX
    bottom = top + row * len(SPEED) - 10
    body = axis(x0, scale, [0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5], top - 6, bottom, t,
                lambda v: f"{v:g} s")
    goal = x0 + 1.0 * scale
    body.append(f'<line x1="{goal:.1f}" y1="{top - 6:.1f}" x2="{goal:.1f}" y2="{bottom:.1f}" '
                f'stroke="{t["ink2"]}" stroke-width="1"/>')
    body.append(text(goal + 6, top + 2, "goal: under 1 s", t["ink2"], 11))
    for i, (label, seconds) in enumerate(SPEED):
        y = top + i * row
        body.append(text(24, y + BAR / 2, label, t["ink"], 13))
        body.append(bar(x0, y, seconds * scale, t["s1"]))
        body.append(text(x0 + seconds * scale + 6, y + BAR / 2, f"{seconds:.2f} s", t["ink"], 11,
                         weight=600))
    desc = "; ".join(f"{label}: {s} s" for label, s in SPEED)
    return frame(
        "Warm answers, Python start-up included",
        "Medians of 5 runs of the real command on the backend. Lower is better.",
        bottom + 40, body, t, desc,
    )


# --- 5. how it works ----------------------------------------------------------------------------
# A drawing, not a chart: PyPI renders no Mermaid, so the README's diagram is an SVG like the rest.


def box(x: float, y: float, w: float, h: float, lines: list[str], t: dict[str, str],
        stroke: str, rx: float = 6, weight: int = 400) -> list[str]:
    out = [f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{rx:.1f}" '
           f'fill="{t["surface"]}" stroke="{stroke}" stroke-width="1.5"/>']
    first = y + h / 2 - (len(lines) - 1) * 8
    for i, line in enumerate(lines):
        out.append(text(x + w / 2, first + i * 16, line, t["ink"] if i == 0 else t["ink2"],
                        13 if i == 0 else 11, "middle", weight if i == 0 else 400))
    return out


def arrow(x1: float, y1: float, x2: float, y2: float, t: dict[str, str]) -> str:
    return (f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{t["ink2"]}" '
            f'stroke-width="1.5" marker-end="url(#head)"/>')


def group(x: float, y: float, w: float, title: str, items: list[list[str]],
          t: dict[str, str], key: str) -> tuple[list[str], float]:
    """A tier: a titled frame holding one box per item. Returns the parts and its height."""
    h = 40 + len(items) * 50
    out = [f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="8" '
           f'fill="none" stroke="{t[key]}" stroke-width="1.5" stroke-dasharray="5 4"/>',
           text(x + 14, y + 20, title, t[key], 12, weight=600)]
    for i, lines in enumerate(items):
        out += box(x + 14, y + 36 + i * 50, w - 28, 40, lines, t, t["axis"])
    return out, h


def architecture_chart(t: dict[str, str]) -> str:
    row, w, h = 84.0, 210.0, 46.0
    body = [f'<defs><marker id="head" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
            f'markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" '
            f'fill="{t["ink2"]}"/></marker></defs>']
    body += box(24, row, w, h, ["terrain refs NAME", "an agent, or you"], t, t["ink2"], 23, 600)
    body += box(275, row, w, h, ["cache per repository", "keyed on content"], t, t["s1"], 6, 600)
    body += box(526, row, w, h, ["answer", "in under a second"], t, t["ink2"], 23, 600)
    body.append(arrow(24 + w, row + h / 2, 275, row + h / 2, t))
    body.append(arrow(275 + w, row + h / 2, 526, row + h / 2, t))
    body.append(text(505, row + h / 2 - 10, "warm", t["ink2"], 11, "middle"))
    top = row + h + 70
    static, hs = group(24, top, 350, "Static tier: reads files, never runs them", [
        ["fixtures and tests", "Python's ast"],
        ["code index", "ast, SQLite"],
        ["front-end HTTP calls", "tree-sitter, or the JS/TS tokenizer"],
    ], t, "s1")
    sandboxed, hi = group(386, top, 350, "Import tier: a sandboxed subprocess", [
        ["setup order", "pytest --collect-only"],
        ["routes", "imports the apps"],
    ], t, "s2")
    body += static + sandboxed
    body.append(arrow(199, top, 340, row + h, t))
    body.append(text(205, top - 30, "changed files only", t["ink2"], 11, "end"))
    body.append(arrow(561, top, 420, row + h, t))
    body.append(text(555, top - 30, "on refresh; served stamped stale", t["ink2"], 11))
    desc = ("A terrain question is answered from a cache per repository, keyed on content: in "
            "under a second when warm. The static tier fills it from changed files only, reading "
            "fixtures, tests and the code index with Python's ast and front-end calls with "
            "tree-sitter or a tokenizer; it never runs the code. The import tier, a sandboxed "
            "subprocess, fills it on refresh with the setup order and the routes.")
    return frame("How it works", "Two tiers fill one cache; every answer is read from it.",
                 top + max(hs, hi) + 24, body, t, desc)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, draw in (("tokens", tokens_chart), ("accuracy", accuracy_chart),
                       ("speed", speed_chart), ("field", field_chart),
                       ("architecture", architecture_chart)):
        for mode, theme in THEMES.items():
            path = OUT / f"{name}-{mode}.svg"
            path.write_bytes(draw(theme).encode("utf-8"))
            print(path.relative_to(OUT.parent.parent).as_posix())


if __name__ == "__main__":
    main()
