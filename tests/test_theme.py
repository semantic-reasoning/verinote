# SPDX-License-Identifier: MPL-2.0
"""Regression lock on the theme token layer (#216).

The stylesheet used to hardcode a handful of colours in rule bodies (`#06101f`
as the on-accent foreground, plus three tinted backgrounds). A hardcoded colour
cannot follow a palette, so a light palette would silently keep dark-mode text
on a dark-mode chip. The fix promotes those colours to custom properties, and
these tests keep them promoted:

* `test_no_hardcoded_colors_outside_palette_blocks` — rule bodies reference
  tokens, never raw literals. Colour literals live only in the two palette
  blocks (the dark `:root` and the light `prefers-color-scheme` override).
* `test_light_palette_redefines_every_dark_token` — the light palette overrides
  *every* token the dark one defines. Derived by set difference rather than a
  hardcoded token list, so adding a token to `:root` and forgetting the light
  side fails here.
* `test_stylesheet_cache_busters_agree` — all three templates that link
  `app.css` ship the same cache-buster, so a palette change cannot reach one
  page while another serves a stale sheet from cache.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "verinote" / "web"
CSS_PATH = WEB / "static" / "app.css"
TEMPLATES = WEB / "templates"

# Templates that link app.css directly (base.html covers every page that extends it).
LINKING_TEMPLATES = ("base.html", "kb_select.html", "policy_halted.html")

# The literals that #216 promoted to tokens. Whitespace inside rgba() is free-form
# in CSS, so match tolerantly: a re-introduced `rgba(91,157,255,.06)` must still fail.
FORBIDDEN_LITERALS = {
    "#06101f": re.compile(r"#06101f", re.IGNORECASE),
    "rgba(91, 157, 255": re.compile(r"rgba\(\s*91\s*,\s*157\s*,\s*255"),
    "rgba(63, 185, 80": re.compile(r"rgba\(\s*63\s*,\s*185\s*,\s*80"),
    "rgba(240, 82, 109": re.compile(r"rgba\(\s*240\s*,\s*82\s*,\s*109"),
}

CUSTOM_PROPERTY = re.compile(r"(--[A-Za-z0-9_-]+)\s*:")


def _block_at(css: str, start: int) -> tuple[int, int]:
    """Return the [start, end) span of the brace-balanced block opening at/after `start`."""
    open_idx = css.index("{", start)
    depth = 0
    for i in range(open_idx, len(css)):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                return start, i + 1
    raise AssertionError(f"unbalanced braces in {CSS_PATH.name} from offset {start}")


def _read_css() -> str:
    return CSS_PATH.read_text(encoding="utf-8")


def _dark_root_block(css: str) -> str:
    """The top-level `:root { ... }` palette block."""
    match = re.search(r"^:root\s*\{", css, re.MULTILINE)
    assert match, "app.css defines no top-level :root palette block"
    start, end = _block_at(css, match.start())
    return css[start:end]


def _light_media_block(css: str) -> str:
    """The whole `@media (prefers-color-scheme: light) { ... }` block, braces included."""
    match = re.search(r"@media[^{]*prefers-color-scheme\s*:\s*light[^{]*\{", css)
    assert match, "app.css defines no (prefers-color-scheme: light) palette"
    start, end = _block_at(css, match.start())
    return css[start:end]


def _rule_bodies(css: str) -> str:
    """app.css with both palette blocks cut out — i.e. everything that must use tokens."""
    for block in (_light_media_block(css), _dark_root_block(css)):
        css = css.replace(block, "", 1)
    return css


def test_rule_bodies_are_not_empty_after_cutting_palettes() -> None:
    """Sanity guard: the cut must not swallow the stylesheet.

    Without this, a cutter bug that returns "" would make the
    no-hardcoded-colours test pass vacuously.
    """
    bodies = _rule_bodies(_read_css())
    assert ".btn" in bodies
    assert ".error" in bodies
    # The palette blocks are a small fraction of the sheet; the rest must survive.
    assert len(bodies.strip()) > 2000, "palette cut removed far too much of app.css"


def test_palette_blocks_are_actually_cut() -> None:
    """The counterpart guard: the cut really does remove the palettes.

    Otherwise the literals *are* still in `bodies` and the next test could only
    ever fail — which is its own kind of broken.
    """
    bodies = _rule_bodies(_read_css())
    assert ":root" not in bodies
    assert "prefers-color-scheme" not in bodies


@pytest.mark.parametrize("literal", sorted(FORBIDDEN_LITERALS))
def test_no_hardcoded_colors_outside_palette_blocks(literal: str) -> None:
    bodies = _rule_bodies(_read_css())
    pattern = FORBIDDEN_LITERALS[literal]
    hits = [line.strip() for line in bodies.splitlines() if pattern.search(line)]
    assert not hits, (
        f"{literal} is hardcoded in an app.css rule body: {hits}. "
        "Promote it to a custom property so the light palette can override it."
    )


def test_light_palette_redefines_every_dark_token() -> None:
    css = _read_css()
    dark = set(CUSTOM_PROPERTY.findall(_dark_root_block(css)))
    light = set(CUSTOM_PROPERTY.findall(_light_media_block(css)))

    assert dark, "the dark :root block declares no custom properties"
    missing = dark - light
    assert not missing, (
        f"the light palette never overrides {sorted(missing)}; those tokens would keep "
        "their dark values under (prefers-color-scheme: light)."
    )


def test_light_palette_declares_color_scheme() -> None:
    """`color-scheme` drives form controls and scrollbars, which var() cannot reach."""
    css = _read_css()
    assert re.search(r"color-scheme\s*:\s*dark", _dark_root_block(css))
    assert re.search(r"color-scheme\s*:\s*light", _light_media_block(css))


def test_stylesheet_cache_busters_agree() -> None:
    link = re.compile(r"""href=["']/static/app\.css(?P<query>[^"']*)["']""")

    queries: dict[str, str] = {}
    for name in LINKING_TEMPLATES:
        html = (TEMPLATES / name).read_text(encoding="utf-8")
        match = link.search(html)
        assert match, f"{name} does not link /static/app.css"
        queries[name] = match.group("query")

    distinct = set(queries.values())
    assert len(distinct) == 1, (
        f"templates link app.css with differing cache-busters: {queries}. "
        "A stale sheet on one page and a fresh one on another is exactly the bug."
    )
    # An empty query on all three would be self-consistent but unbustable.
    assert distinct.pop().startswith("?v="), (
        f"app.css is linked without a ?v= cache-buster: {queries}"
    )
