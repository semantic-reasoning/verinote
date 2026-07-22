# SPDX-License-Identifier: MPL-2.0
"""Regression lock on the status banners' non-colour channel (#226).

`.ok-note`, `.warn` and `.error` are the three boxes the app drops a status message
into -- a saved-prompt confirmation, a missing-backend warning, a halted-KB alert.
All three were the same box at the same radius and padding, differing only in which
palette token they reached for (`--ok` / `--warn` / `--danger` and the matching tinted
background). Hue was the whole signal, so in greyscale, on a monochrome print, or to a
red-green deficiency the three collapsed into one.

The Ask verdict (#220) had the same bug and is already fixed; its banner carries a glyph
*and* a border style. It could also lean on its label text, which spells "VERIFIED" out
in words. These three cannot: the banner wraps whatever message the caller passed, and
nothing obliges that message to name its own severity.

What is pinned, and how:

1. **Each banner carries a signal that is its own** -- something that draws, that is not
   colour, and that the other two do not also have. The shared box (radius, padding,
   1px border) is subtracted first, because a property all three set cannot tell them
   apart, and leaving it in would let "the banners are styled" pass for "the banners
   are distinguishable".
2. **The three signals are pairwise distinct.**
3. **A glyph delivered through `content` is not announced** -- the painted half has to
   paint, and the alt half has to be empty.
4. **The markup still emits the three classes**, or the rules are dead style.
5. **No template hardcodes its own glyph** into a banner's text. settings.html did
   ("✓ {{ test_result }}"), which renders as two check marks once the CSS supplies one.

The comparison runs on *resolved signal values*, not on declaration sets or on "a rule
exists". Every source-text assertion has an ineffective preimage: a rule can be present
and paint nothing, a `content` can be `"" / "alt"`, a border can be `0`/`hidden`. Four
lanes on this repo were broken exactly that way, and #220 was re-broken four rounds
running while its fix kept growing a list of no-op values to reject -- a list with no
end. So this asks the finite question instead: what does each banner actually draw on a
channel that is not colour? A declaration that renders nothing resolves to None and
falls out, whatever it is, and any number of extra declarations bolted on beside the
signal cannot manufacture a difference.

One structural fact does most of the work and is worth stating on its own: a
`::before` with no `content` property is never generated, so every other declaration on
it is dead. Without that check, `border-left-style: solid / dashed / double` across the
three banners reads as three distinct drawn values -- it passes any per-declaration test
of "is this value inert" -- while painting nothing whatsoever. That hole was found by
probing this guard rather than by reasoning about it, and it is the shape of hole that
enumerating no-op *values* can never close, because the values are not the problem.

Two limits worth naming rather than glossing. This reads declarations, not a rendered
box, so it cannot resolve the cascade -- a signal property restating an inherited value
on a pseudo-element that *does* exist still reads as a change. And an implementation
hiding a glyph inside the `background` shorthand would be missed, since the shorthand is
treated as colour wholesale; use `background-image`, which is read.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "verinote" / "web"
CSS = WEB / "static" / "app.css"
TEMPLATES = WEB / "templates"

# The three status banners, by class. The `-inline` variants are out of scope on purpose:
# they tag a fragment of a sentence the template writes ("3 failed", "2 pending"), so the
# word beside them already carries the meaning.
BANNERS = ("ok-note", "warn", "error")

RULE = re.compile(r"(?P<selector>[^{}]+)\{(?P<body>[^{}]*)\}")

# A declaration is colour if its property names one or its value reaches for a palette
# token. Dropping these is the greyscale simulation.
COLOUR_PROPERTY = re.compile(r"^(color|background|border(-[a-z]+)*-color|outline-color)$")
# A palette reference inside a value. Only the reference goes, not the declaration: a
# `box-shadow`'s offsets are a shape channel even though its colour is not, and dropping
# it wholesale would reject a shadow-only design as "colour alone" when its geometry is
# what distinguishes it.
COLOUR_VAR = re.compile(
    r"var\(\s*--(?:ok|warn|danger|accent|line|muted|panel|fg|bg|term)[a-z-]*\s*\)"
)

BORDER_STYLE = re.compile(r"^(border|outline)(-[a-z]+)*-style$")
BORDER_WIDTH = re.compile(r"^(border|outline)(-[a-z]+)*-width$")

# The border/outline keywords that paint a line. `none` and `hidden` do not, which is what
# lets solid/hidden/none read as three channels while rendering as two.
DRAWN_BORDER_STYLES = frozenset(
    {"solid", "dashed", "dotted", "double", "groove", "ridge", "inset", "outset"}
)

# Values that leave a property drawing nothing, whatever the property is.
INERT_VALUES = frozenset({"none", "normal", "hidden", "auto", "initial", "unset", "revert"})

ZERO_LENGTH = re.compile(r"^0[a-z%]*$")
# The painted half of `content: "x" / "alt"`. The half after the slash is the accessible
# name and never reaches the screen, so it cannot stand in for a glyph.
DRAWN_CONTENT = re.compile(r'^"([^"]*)"')
CONTENT_ALT = re.compile(r'/\s*"([^"]*)"\s*$')

# CSS writes non-ASCII as `\2713`, optionally with one whitespace terminator. The sheet is
# read as text, so the escape has to be resolved before the glyph can be judged: otherwise
# `\200b` reads as five printable characters instead of one invisible one.
CSS_ESCAPE = re.compile(r"\\([0-9a-fA-F]{1,6})\s?")

# Categories that occupy the line without marking it: separators (Zs/Zl/Zp) and the
# control/format characters (Cc/Cf) that include the zero-width family.
INVISIBLE_CATEGORIES = frozenset({"Zs", "Zl", "Zp", "Cc", "Cf"})

# The properties that put a *mark* on the page, as against ones that only move a mark
# around. `margin-right` and `font-size` are real CSS, but restating a value the shared
# rule already sets changes no pixel, and a guard comparing whole declaration sets cannot
# tell those apart. Asking which signal differs makes this immune to whatever else is
# bolted on beside it, inert or not -- which is the point, since the inert values cannot
# be enumerated but the signal channels can.
SIGNAL_PROPERTY = re.compile(
    r"^((border|outline)(-[a-z]+)*|box-shadow|content|background-image"
    r"|text-decoration(-[a-z]+)?|font-weight|font-style|text-transform)$"
)


def _rules() -> list[tuple[str, str]]:
    """(selector, declaration body) for every rule in app.css, comments stripped."""
    css = re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.S)
    return [(m.group("selector").strip(), m.group("body")) for m in RULE.finditer(css)]


def _declarations(body: str) -> list[tuple[str, str]]:
    pairs = []
    for chunk in body.split(";"):
        prop, sep, value = chunk.partition(":")
        if sep:
            pairs.append((prop.strip(), " ".join(value.split())))
    return pairs


def _paints(text: str) -> bool:
    """Whether the text puts at least one visible mark on the page."""
    return any(unicodedata.category(char) not in INVISIBLE_CATEGORIES for char in text)


def _drawn(prop: str, value: str) -> str | None:
    """The value's drawn form, or None when this declaration paints nothing.

    What it rejects, and nothing beyond it: the CSS-wide keywords in INERT_VALUES on any
    property; a border/outline *style* outside the painting keywords; a zero length on a
    border/outline *width* only; and a `content` whose painted half is empty or made
    entirely of invisible characters.

    Lengths are not normalised, so `3.0px` and `3px` compute identically but compare as
    two. That direction is harmless here -- it can only invent a difference between two
    banners, and inventing one is not how this guard fails.
    """
    base = prop.split("|")[-1]
    if value in INERT_VALUES:
        return None
    if BORDER_STYLE.match(base):
        return value if value in DRAWN_BORDER_STYLES else None
    if BORDER_WIDTH.match(base):
        return None if ZERO_LENGTH.match(value) else value
    if base == "content":
        match = DRAWN_CONTENT.match(value)
        if not match:
            return None
        glyph = CSS_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), match.group(1))
        return glyph if _paints(glyph) else None
    return value


def _banner_declarations(banner: str) -> list[tuple[str, str]]:
    """Every declaration reaching `.<banner>` or `.<banner>::before`, tagged by scope.

    Grouped selectors are split on the comma first, so a rule several banners share
    contributes the same (scope, property, value) to each of them -- which is what lets
    the shared box cancel out below. Matching the whole grouped selector instead would
    hand each banner a distinct key for identical declarations, and the guard would read
    a shared rule as a difference.

    Only the exact class and its `::before` are collected. A descendant rule
    (`.error .foo`) styles something inside the banner, not the banner.
    """
    found = []
    for selector, body in _rules():
        for part in (p.strip() for p in selector.split(",")):
            if part not in (f".{banner}", f".{banner}::before"):
                continue
            scope = part.split(f".{banner}", 1)[1]
            found += [(f"{scope}|{prop}", value) for prop, value in _declarations(body)]
    assert found, f"app.css styles nothing for the .{banner} banner"
    return found


def _generates_pseudo(banner: str) -> bool:
    """Whether `.<banner>::before` is generated at all.

    A pseudo-element with no `content` property does not exist -- the browser creates no
    box, and every other declaration on it is dead, however much it looks like a channel.
    This is the structural version of the question, and it closes a hole that no amount
    of no-op-value rejection reaches: `border-left-style: solid/dashed/double` across the
    three banners is three drawn values by any per-declaration test, yet paints nothing
    when the ::before was never generated.

    An *empty* `content: ""` still generates the box, which is the standard icon idiom
    (`content: ""` plus a sized `background-image`), so presence is the test here, not
    paintedness. Whether the glyph itself paints is a separate question, asked below.
    """
    return any(
        prop.split("|")[0] == "::before" and prop.split("|")[-1] == "content"
        for prop, _ in _banner_declarations(banner)
    )


def _signals(banner: str) -> frozenset[tuple[str, str]]:
    """The banner's non-colour, actually-drawn signal channels."""
    signals = set()
    pseudo_exists = _generates_pseudo(banner)
    for prop, value in _banner_declarations(banner):
        scope, base = prop.split("|")[0], prop.split("|")[-1]
        if scope == "::before" and not pseudo_exists:
            continue
        if COLOUR_PROPERTY.match(base) or not SIGNAL_PROPERTY.match(base):
            continue
        stripped = " ".join(COLOUR_VAR.sub(" ", value).split())
        if not stripped:
            continue
        drawn = _drawn(prop, stripped)
        if drawn is not None:
            signals.add((prop, drawn))
    return frozenset(signals)


def _distinguishing() -> dict[str, frozenset[tuple[str, str]]]:
    """Each banner's signals with the ones common to all three removed.

    The subtraction is the load-bearing part. All three wear `border-radius: 8px` and a
    1px border, so a plain "does this banner have a non-colour signal" check passes
    against the unfixed stylesheet. Only what one banner has and the others lack can
    tell the reader which banner they are looking at.
    """
    signals = {banner: _signals(banner) for banner in BANNERS}
    common = frozenset.intersection(*signals.values())
    return {banner: found - common for banner, found in signals.items()}


def _banner_bodies(banner: str) -> list[str]:
    """The text each template puts inside a `<... class="<banner>">` element."""
    bodies = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        html = path.read_text(encoding="utf-8")
        for match in re.finditer(rf'class="{re.escape(banner)}"[^>]*>', html):
            bodies.append(html[match.end() :].lstrip())
    return bodies


@pytest.mark.parametrize("banner", BANNERS)
def test_each_banner_draws_something_no_other_banner_draws(banner: str) -> None:
    """Guard 1: the fix for #226 itself.

    Against the pre-fix stylesheet every banner's distinguishing set is empty: the three
    boxes are identical once the palette tokens are stripped out.
    """
    distinguishing = _distinguishing()[banner]

    assert distinguishing, (
        f"the .{banner} banner is carried by colour alone -- once the palette tokens are "
        "stripped it draws nothing the other two banners do not also draw, so a "
        "greyscale print or a red-green deficiency erases which banner it is"
    )


def test_the_three_banners_do_not_share_one_signal() -> None:
    """Guard 2: three non-empty signals are not three *distinct* signals.

    Guard 1 stays green if two banners are given the same glyph and the third a
    different one -- both still differ from the common set. This is the half that says
    a warning cannot look like an error.
    """
    distinguishing = _distinguishing()

    assert len(set(distinguishing.values())) == len(BANNERS), (
        "two banners render identically once the colour is stripped: "
        f"{ {k: sorted(v) for k, v in distinguishing.items()} }"
    )


@pytest.mark.parametrize("banner", BANNERS)
def test_a_banner_glyph_is_drawn_and_silent(banner: str) -> None:
    """Guard 3: the accessibility trap in the `content` shorthand.

    Conditional on `content` being how the banner does it. An implementation using a
    border style on the banner box, or the `content: ""` + `background-image` icon
    idiom, is left alone -- over-constraining this was a real finding in #220.

    Two halves, each with its own escape. A text glyph has to paint: `content: "" /
    "error"` satisfies "a content declaration exists" while drawing nothing at all. But
    an empty `content` is legitimate when it is generating a box for an image, so the
    paint requirement is discharged by any drawn signal on the pseudo-element, not by
    the string alone. And a glyph that *does* paint has to be silent: `content: "✕"`
    without the alt half makes a screen reader read the decoration out ahead of a
    message whose role="alert"/"status" already conveyed the severity.
    """
    for prop, value in _banner_declarations(banner):
        if prop.split("|")[0] != "::before" or prop.split("|")[-1] != "content":
            continue
        glyph = _drawn(prop, value)
        others = {p for p, _ in _signals(banner) if p.split("|")[0] == "::before"}
        assert glyph is not None or others - {"::before|content"}, (
            f".{banner}'s ::before draws nothing (content {value!r} and no other drawn "
            "channel on the pseudo-element); the painted half of the glyph is empty or "
            "invisible, so only the alt text is left and nothing reaches the eye"
        )
        if glyph is None:
            continue
        alt = CONTENT_ALT.search(value)
        assert alt is not None and not alt.group(1), (
            f".{banner}'s glyph is announced (content {value!r}); it needs the `/ \"\"` "
            "alt half, or a screen reader reads the decoration out ahead of a message "
            'whose role="alert"/"status" has already conveyed the severity'
        )


@pytest.mark.parametrize("banner", BANNERS)
def test_the_markup_still_emits_the_banner_class(banner: str) -> None:
    """Guard 4: a rule no template reaches is dead style, which is the #220 bug."""
    assert _banner_bodies(banner), (
        f"no template emits class=\"{banner}\"; its rules in app.css style nothing"
    )


@pytest.mark.parametrize("banner", BANNERS)
def test_no_template_hardcodes_its_own_banner_glyph(banner: str) -> None:
    """Guard 5: settings.html rendered "✓ ✓ Connected ..." the moment the CSS supplied one.

    Asserted as "a banner's text does not open with a Unicode symbol" rather than as a
    list of glyphs to ban -- prose, a digit and a `{{ ... }}` expression are all fine,
    and enumerating decorations has the same no-end problem as enumerating no-ops.
    """
    for body in _banner_bodies(banner):
        assert not unicodedata.category(body[0]).startswith("S"), (
            f'a template opens its .{banner} banner with {body[0]!r} ({body[:40]!r}); the '
            "CSS already draws a glyph there, so the reader sees it twice"
        )
