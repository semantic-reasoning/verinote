# SPDX-License-Identifier: MPL-2.0
"""Regression lock on the theme token layer (#216).

The stylesheet used to hardcode colours in rule bodies (`#06101f` as the
on-accent foreground, plus three tinted backgrounds) and shipped a single dark
palette. A hardcoded colour cannot follow a palette, so a light theme would have
left dark-mode text sitting on a light-mode chip. The fix promotes those colours
to custom properties and adds a `prefers-color-scheme: light` palette.

Locking only the *token definitions* would be too weak: a token can be defined
in both palettes and then never referenced, which is precisely the bug #216
reported (a button that gets *brighter* on hover in light mode). So these tests
pin three separate things:

1. **No raw colour literals in rule bodies.** Any hex / rgb() / hsl() / named
   colour outside the two palette blocks fails -- not just the four literals
   this change happened to remove.
2. **Both palettes agree on the token set**, by set difference off the parsed
   declarations rather than a hardcoded list, and each declares its own
   `color-scheme` (which `var()` cannot express, and which drives form controls
   and scrollbars).
3. **Rule bodies actually reference the tokens.** `.btn:hover` must go through
   `--btn-hover-filter`; the three banner rules must go through their `*-bg`
   tokens, and must agree on their box metrics *by value* -- asserting merely
   that each declares a `padding` would let one banner drift to a different
   shape while the test still read as "identical".

Note on parsing: the light palette is a `@media` block whose *preamble* contains
the text `prefers-color-scheme: light`. Matching `color-scheme:\\s*light` against
the whole block would self-match that preamble and pass even if the declaration
were deleted or flipped to `dark`. Everything below inspects the media block's
inner body only.
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

CUSTOM_PROPERTY = re.compile(r"(--[A-Za-z0-9_-]+)\s*:")

# Colour literals of any shape. Rule bodies must go through var(); only the palette
# blocks may name a colour outright.
HEX_COLOR = re.compile(r"#[0-9a-fA-F]{3,8}\b")
FUNCTIONAL_COLOR = re.compile(r"\b(?:rgba?|hsla?|color-mix)\s*\(")
# Bare keywords. The lookarounds keep `white-space` and `-webkit-box` from matching.
NAMED_COLOR = re.compile(
    r"(?<![\w-])(?:white|black|red|green|blue|yellow|orange|purple|pink|brown|gray|grey"
    r"|silver|navy|teal|olive|maroon|lime|aqua|cyan|magenta|fuchsia|gold|beige|ivory)(?![\w-])",
    re.IGNORECASE,
)
COLOR_LITERAL_PATTERNS = {
    "hex": HEX_COLOR,
    "rgb()/hsl()": FUNCTIONAL_COLOR,
    "named colour": NAMED_COLOR,
}
# `transparent` and `currentColor` carry no palette of their own, so they are palette-safe.

# The status banners: selector -> the token stem each one pulls its colours from.
BANNERS = {".error": "danger", ".ok-note": "ok", ".warn": "warn"}
# Properties the banners must agree on *by value* -- they are one component in three colours.
BANNER_BOX_METRICS = ("border-radius", "padding", "margin")

# Inline elements that must never carry a banner class (it has padding/margin of its own).
# Not an exhaustive HTML inline-element list: see the guard's docstring.
INLINE_TAGS = ("span", "code", "em", "strong", "a", "small", "b", "i", "label")


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
    """The whole `@media (prefers-color-scheme: light) { ... }` block, preamble included."""
    match = re.search(r"@media[^{]*prefers-color-scheme\s*:\s*light[^{]*\{", css)
    assert match, "app.css defines no (prefers-color-scheme: light) palette"
    start, end = _block_at(css, match.start())
    return css[start:end]


def _light_palette_declarations(css: str) -> str:
    """*Inside* the light media block: the declarations only, never the @media preamble.

    The preamble literally reads `prefers-color-scheme: light`, so any regex run over
    the whole block matches it by accident and can never fail. This strips it.
    """
    block = _light_media_block(css)
    inner = block[block.index("{") + 1 : block.rindex("}")]
    assert "prefers-color-scheme" not in inner, "the @media preamble leaked into the body"
    return inner


def _strip_comments(css: str) -> str:
    """Drop /* ... */ comments.

    Comments contain no braces, so a comment sitting above a rule would otherwise be
    glued onto that rule's selector by `_rules()` -- and a colour mentioned in prose
    would look like a hardcoded literal.
    """
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


def _rule_bodies(css: str) -> str:
    """app.css with both palette blocks cut out -- everything that must use tokens."""
    for block in (_light_media_block(css), _dark_root_block(css)):
        css = css.replace(block, "", 1)
    return _strip_comments(css)


def _rules(css: str) -> dict[str, str]:
    """Map `selector -> declaration body` for the flat (non-@media) rules."""
    rules: dict[str, str] = {}
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        selector = " ".join(match.group(1).split())
        rules[selector] = match.group(2)
    return rules


def _declarations(body: str) -> dict[str, str]:
    """Map `property -> value` for one rule body, with whitespace collapsed.

    Comparing values (not just "is the property mentioned") is the whole point: a banner
    can declare `padding` and still be the wrong shape. A repeated property keeps its
    last value, which is what the cascade does too.
    """
    declarations: dict[str, str] = {}
    for chunk in body.split(";"):
        prop, sep, value = chunk.partition(":")
        if sep:
            declarations[" ".join(prop.split())] = " ".join(value.split())
    return declarations


# --- WCAG contrast (#218) --------------------------------------------------
#
# The banners paint status *text* over a translucent status *tint*, and the tint
# is composited over --bg. So the effective contrast is not text-vs-tint-colour;
# it is text-vs-(tint alpha-composited over --bg). The light palette shipped tints
# and text at ratios of 4.03-4.44:1 -- under the 4.5:1 WCAG AA floor for the 15px
# body text these banners carry. These checks resolve every colour (through var()
# and rgba()), composite the alpha, and pin all three banners over 4.5:1 in *both*
# palettes so neither can regress.

WCAG_AA_NORMAL = 4.5

# banner selector -> (tint token, text token), both resolved within one palette.
BANNER_CONTRAST = {selector: (f"--{stem}-bg", f"--{stem}") for selector, stem in BANNERS.items()}

TOKEN_DECL = re.compile(r"(--[A-Za-z0-9_-]+)\s*:\s*([^;{}]+?)\s*;")
RGBA_CALL = re.compile(r"rgba?\(([^)]*)\)")
COLOR_MIX = re.compile(r"color-mix\(\s*in\s+srgb\s*,(.*)\)\s*$", re.IGNORECASE | re.DOTALL)


def _palette_tokens(block: str) -> dict[str, str]:
    """Map `--token -> value` for one palette block, ignoring selectors and preambles.

    Regex-scoped to `--name: value;` so the `@media (prefers-color-scheme: light)`
    preamble and the `:root {` selector text contribute no entries.
    """
    return {m.group(1): m.group(2).strip() for m in TOKEN_DECL.finditer(block)}


def _hex_rgb(value: str) -> tuple[float, float, float]:
    h = value.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(float(int(h[i : i + 2], 16)) for i in (0, 2, 4))  # type: ignore[return-value]


def _resolve_rgba(value: str, tokens: dict[str, str]) -> tuple[float, float, float, float]:
    """Resolve a CSS colour to (r, g, b, alpha), following var(), rgba() and color-mix()."""
    value = value.strip()
    if value.startswith("var("):
        name = value[value.index("(") + 1 : value.index(")")].strip()
        assert name in tokens, f"var({name}) has no definition in this palette"
        return _resolve_rgba(tokens[name], tokens)
    if value.startswith("#"):
        return (*_hex_rgb(value), 1.0)
    mix = COLOR_MIX.match(value)
    if mix:
        return _resolve_color_mix(mix.group(1), tokens)
    call = RGBA_CALL.match(value)
    if call:
        parts = [p.strip() for p in call.group(1).split(",")]
        assert len(parts) in (3, 4), f"unparseable rgb()/rgba(): {value!r}"
        r, g, b = (float(p) for p in parts[:3])
        alpha = float(parts[3]) if len(parts) == 4 else 1.0
        return (r, g, b, alpha)
    raise AssertionError(f"cannot resolve colour {value!r}; extend _resolve_rgba for it")


def _resolve_color_mix(args: str, tokens: dict[str, str]) -> tuple[float, float, float, float]:
    """Resolve `color-mix(in srgb, C1 [p1%], C2 [p2%])` to (r, g, b, alpha).

    Not exercised by the shipped stylesheet (the tints are rgba()); it exists so a
    future tint expressed as color-mix is composited correctly rather than crashing.
    """
    halves = [h.strip() for h in args.split(",")]
    assert len(halves) == 2, f"color-mix must name two colours: {args!r}"
    colors: list[tuple[float, float, float, float]] = []
    weights: list[float | None] = []
    for half in halves:
        pct = re.search(r"([0-9.]+)%\s*$", half)
        weights.append(float(pct.group(1)) / 100 if pct else None)
        colors.append(_resolve_rgba(half[: pct.start()].strip() if pct else half, tokens))
    w0, w1 = weights
    if w0 is None and w1 is None:
        w0 = w1 = 0.5
    elif w0 is None:
        w0 = 1 - w1  # type: ignore[operator]
    elif w1 is None:
        w1 = 1 - w0
    total = w0 + w1
    w0, w1 = w0 / total, w1 / total
    return tuple(colors[0][i] * w0 + colors[1][i] * w1 for i in range(4))  # type: ignore[return-value]


def _composite(fg: tuple[float, float, float, float], bg: tuple[float, float, float]) -> tuple[float, float, float]:
    """Alpha-composite `fg` (with alpha) over an opaque `bg`."""
    a = fg[3]
    return tuple(fg[i] * a + bg[i] * (1 - a) for i in range(3))  # type: ignore[return-value]


def _relative_luminance(rgb: tuple[float, float, float]) -> float:
    """WCAG 2.x relative luminance from 8-bit sRGB channels."""
    chan = []
    for c in rgb:
        c /= 255.0
        chan.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = chan
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast_ratio(fg: tuple[float, float, float], bg: tuple[float, float, float]) -> float:
    lo, hi = sorted((_relative_luminance(fg), _relative_luminance(bg)))
    return (hi + 0.05) / (lo + 0.05)


def _banner_contrast(palette_block: str, selector: str) -> tuple[float, tuple[float, float, float]]:
    """Effective (text, composited-background) contrast for one banner in one palette."""
    tokens = _palette_tokens(palette_block)
    tint_token, text_token = BANNER_CONTRAST[selector]
    base = _resolve_rgba(tokens["--bg"], tokens)[:3]
    banner_bg = _composite(_resolve_rgba(tokens[tint_token], tokens), base)
    text = _composite(_resolve_rgba(tokens[text_token], tokens), banner_bg)
    return _contrast_ratio(text, banner_bg), banner_bg


def _palette_blocks() -> dict[str, str]:
    css = _read_css()
    return {"dark": _dark_root_block(css), "light": _light_media_block(css)}


def test_rule_bodies_are_not_empty_after_cutting_palettes() -> None:
    """Sanity guard: the cut must not swallow the stylesheet.

    Without this, a cutter bug that returns "" would make the
    no-hardcoded-colours test pass vacuously.
    """
    bodies = _rule_bodies(_read_css())
    assert ".btn" in bodies
    assert ".error" in bodies
    assert len(bodies.strip()) > 2000, "palette cut removed far too much of app.css"


def test_palette_blocks_are_actually_cut() -> None:
    """The counterpart guard: the cut really does remove the palettes."""
    bodies = _rule_bodies(_read_css())
    assert ":root" not in bodies
    assert "prefers-color-scheme" not in bodies


@pytest.mark.parametrize("kind", sorted(COLOR_LITERAL_PATTERNS))
def test_no_color_literals_outside_palette_blocks(kind: str) -> None:
    """Every colour in a rule body must come from a token, so a palette can override it."""
    pattern = COLOR_LITERAL_PATTERNS[kind]
    hits = [line.strip() for line in _rule_bodies(_read_css()).splitlines() if pattern.search(line)]
    assert not hits, (
        f"app.css rule bodies hardcode a colour ({kind}): {hits}. "
        "Promote it to a custom property so the light palette can override it."
    )


def test_light_palette_redefines_every_dark_token() -> None:
    css = _read_css()
    dark = set(CUSTOM_PROPERTY.findall(_dark_root_block(css)))
    light = set(CUSTOM_PROPERTY.findall(_light_palette_declarations(css)))

    assert dark, "the dark :root block declares no custom properties"
    missing = dark - light
    assert not missing, (
        f"the light palette never overrides {sorted(missing)}; those tokens would keep "
        "their dark values under (prefers-color-scheme: light)."
    )


def test_each_palette_declares_its_own_color_scheme() -> None:
    """`color-scheme` drives form controls and scrollbars, which var() cannot reach.

    Checked against the media block's *inner* declarations: matching the whole block
    would hit the `prefers-color-scheme: light` preamble and pass unconditionally.
    """
    css = _read_css()
    assert re.search(r"color-scheme\s*:\s*dark", _dark_root_block(css)), (
        ":root does not declare color-scheme: dark"
    )
    light = _light_palette_declarations(css)
    assert re.search(r"color-scheme\s*:\s*light", light), (
        "the light palette does not declare color-scheme: light"
    )
    assert not re.search(r"color-scheme\s*:\s*dark", light), (
        "the light palette declares color-scheme: dark, which would keep dark form "
        "controls and scrollbars on a light page"
    )


def test_button_filters_reference_their_tokens() -> None:
    """A token that nothing references is not a theme -- it is dead weight.

    #216's concrete symptom: `filter: brightness(1.08)` *brightens* an already-light
    button, so hover reads as no feedback at all. Defining --btn-hover-filter without
    referencing it here would leave that bug in place with the token test still green.
    """
    rules = _rules(_rule_bodies(_read_css()))

    hover = rules.get(".btn:hover:not(:disabled)")
    assert hover, "app.css no longer has a .btn:hover:not(:disabled) rule"
    assert "var(--btn-hover-filter)" in hover, (
        f".btn:hover must filter through var(--btn-hover-filter), got: {hover.strip()!r}"
    )

    active = rules.get(".btn:active:not(:disabled)")
    assert active, "app.css no longer has a .btn:active:not(:disabled) rule"
    assert "var(--btn-active-filter)" in active, (
        f".btn:active must filter through var(--btn-active-filter), got: {active.strip()!r}"
    )

    for name, body in (("hover", hover), ("active", active)):
        assert "brightness(" not in body, (
            f".btn:{name} hardcodes brightness(); the light palette cannot flip its direction"
        )


@pytest.mark.parametrize("selector", sorted(BANNERS))
def test_banner_rules_reference_their_status_token(selector: str) -> None:
    """Each status banner takes tint, outline and text colour from its own token stem.

    Anything less (e.g. .warn reduced to a bare `color:`) makes a warning read as weak
    inline text. This test pins *which tokens* a banner dereferences; the shared box
    metrics are pinned by test_banner_box_metrics_are_identical below.
    """
    stem = BANNERS[selector]
    body = _rules(_rule_bodies(_read_css())).get(selector)
    assert body, f"app.css no longer has a `{selector}` rule"
    declarations = _declarations(body)

    assert declarations.get("background") == f"var(--{stem}-bg)", (
        f"{selector} must take its tint from var(--{stem}-bg), got: {declarations!r}"
    )
    assert declarations.get("border") == f"1px solid var(--{stem})", (
        f"{selector} must be outlined with var(--{stem}), got: {declarations!r}"
    )
    assert declarations.get("color") == f"var(--{stem})", (
        f"{selector} must take its text colour from var(--{stem}), got: {declarations!r}"
    )


@pytest.mark.parametrize("prop", BANNER_BOX_METRICS)
def test_banner_box_metrics_are_identical(prop: str) -> None:
    """The three banners must agree on their *values*, not merely declare the property.

    Asserting only `"padding:" in body` would let .warn drift to `padding: 3rem` while
    .error stayed at `.6rem .9rem` -- three status banners in three different shapes,
    with a test still named "identical". The colour tokens legitimately differ per
    banner, so only the box metrics are compared: collapse them to a set, demand one
    value.
    """
    rules = _rules(_rule_bodies(_read_css()))

    values: dict[str, str] = {}
    for selector in BANNERS:
        body = rules.get(selector)
        assert body, f"app.css no longer has a `{selector}` rule"
        declarations = _declarations(body)
        assert prop in declarations, (
            f"{selector} declares no `{prop}`; the three banners must share box metrics"
        )
        values[selector] = declarations[prop]

    assert len(set(values.values())) == 1, (
        f"the status banners disagree on `{prop}`: {values}. "
        "They are the same component in three colours and must share their box metrics."
    )


def test_warn_has_an_inline_variant_and_no_tag_qualified_banner() -> None:
    """`.warn` is the banner; `.warn-inline` is the inline variant -- same split as .error.

    Tag-qualified selectors (`p.warn`, `div.warn`) were rejected: rewriting a template's
    <div> to a <section> would silently drop the banner.
    """
    rules = _rules(_rule_bodies(_read_css()))

    inline = rules.get(".warn-inline")
    assert inline, "app.css defines no .warn-inline (the inline counterpart to the .warn banner)"
    assert "color: var(--warn)" in " ".join(inline.split())
    assert "background" not in inline, ".warn-inline must stay plain text, not a chip"

    for selector in rules:
        assert not re.search(r"\b(?:p|div|span|section)\.warn\b", selector), (
            f"tag-qualified warn selector {selector!r}: use .warn / .warn-inline instead, "
            "or the banner disappears when the markup's tag changes"
        )


def test_templates_do_not_put_the_warn_banner_on_an_inline_element() -> None:
    """Best-effort lint: the `.warn` banner (padding/margin) landing on an inline tag.

    Deliberately *not* a general HTML parse. It catches an opening tag from INLINE_TAGS
    whose `class` attribute sits on the same line (either quote style) and contains the
    bare `warn` class -- the shape all three current `.warn` call sites take. It would
    miss a `class=` split across lines, or an inline tag outside the list. Those are
    accepted blind spots, not oversights: `.warn` has exactly three call sites repo-wide,
    and a full HTML parser here would be more machinery than the risk warrants. The name
    and this docstring are scoped to what it actually checks.
    """
    tags = "|".join(INLINE_TAGS)
    # `(?<![\w-])warn(?![\w-])` so `class="warn-inline"` is not mistaken for the banner.
    pattern = re.compile(
        rf"""<(?:{tags})\b[^>]*\bclass\s*=\s*["'][^"']*(?<![\w-])warn(?![\w-])""",
        re.IGNORECASE,
    )
    offenders = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert not offenders, (
        f"the .warn banner class is applied to an inline element: {offenders}. "
        "Use .warn-inline there."
    )


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


@pytest.mark.parametrize("palette", sorted(_palette_blocks()))
@pytest.mark.parametrize("selector", sorted(BANNER_CONTRAST))
def test_banner_text_meets_wcag_aa_contrast(palette: str, selector: str) -> None:
    """Each banner's status text must clear 4.5:1 over its *composited* tint, per palette.

    Contrast is computed against the tint alpha-composited over --bg, not the tint
    colour named in the token -- that is the distinction the light palette failed
    (text-vs-token read ~7:1 while text-vs-composited read 4.03-4.44:1).
    """
    ratio, bg = _banner_contrast(_palette_blocks()[palette], selector)
    assert ratio >= WCAG_AA_NORMAL, (
        f"{selector} in the {palette} palette: status text on its composited tint "
        f"{tuple(round(c) for c in bg)} is {ratio:.3f}:1, under the WCAG AA {WCAG_AA_NORMAL}:1 "
        "floor for 15px body text. Darken the text token or thin the tint."
    )


@pytest.mark.parametrize("palette", sorted(_palette_blocks()))
def test_banner_tints_are_translucent_so_the_composite_matters(palette: str) -> None:
    """Guard against a vacuous contrast test: the tints must actually carry alpha < 1.

    If a tint were opaque, `_banner_contrast` would reduce to text-vs-token and the
    whole point (compositing over --bg) would go untested while still passing.
    """
    tokens = _palette_tokens(_palette_blocks()[palette])
    for selector, (tint_token, _text_token) in BANNER_CONTRAST.items():
        alpha = _resolve_rgba(tokens[tint_token], tokens)[3]
        assert alpha < 1.0, (
            f"{selector}'s tint {tint_token} in the {palette} palette is opaque "
            f"(alpha {alpha}); the contrast test would never exercise compositing."
        )
