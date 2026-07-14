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
   tokens and stay structurally identical.

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

# Banner rules that must stay structurally identical: selector -> token stem.
BANNERS = {".error": "danger", ".ok-note": "ok", ".warn": "warn"}


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
def test_banner_rules_are_structurally_identical(selector: str) -> None:
    """.error / .ok-note / .warn are the three status banners and must look alike.

    They differ only in which token stem they pull from. Anything less (e.g. .warn
    reduced to a bare `color:`) makes a warning read as weak inline text.
    """
    stem = BANNERS[selector]
    rules = _rules(_rule_bodies(_read_css()))

    body = rules.get(selector)
    assert body, f"app.css no longer has a `{selector}` rule"
    normalized = " ".join(body.split())

    assert f"background: var(--{stem}-bg)" in normalized, (
        f"{selector} must take its tint from var(--{stem}-bg), got: {normalized!r}"
    )
    assert f"border: 1px solid var(--{stem})" in normalized, (
        f"{selector} must be outlined with var(--{stem}), got: {normalized!r}"
    )
    assert f"color: var(--{stem})" in normalized, (
        f"{selector} must take its text colour from var(--{stem}), got: {normalized!r}"
    )
    for prop in ("border-radius", "padding", "margin"):
        assert f"{prop}:" in normalized, f"{selector} is missing `{prop}` -- banners must match"


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


def test_templates_use_the_warn_banner_only_on_block_elements() -> None:
    """The banner class carries padding/margin, so it must not land on an inline <span>."""
    offenders = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            # `(?![\w-])` so that `class="warn-inline"` is not mistaken for the banner class.
            if re.search(r"<span[^>]*\bclass=\"[^\"]*(?<![\w-])warn(?![\w-])", line):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert not offenders, (
        f"the .warn banner class is applied to an inline <span>: {offenders}. "
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
