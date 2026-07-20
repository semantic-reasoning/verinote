# SPDX-License-Identifier: MPL-2.0
"""Regression lock on the Ask verdict rendering (#220).

The Ask page hung its result styling off `ask-{{ result.route }}`, but `route`
only ever takes two values (`engine` and `fallback`) while the page has three
verdicts to tell apart: a verified answer, a verified *negative* ("no confirmed
facts match"), and an unverified fallback. `route` collapses the first two --
and `.ask-engine` / `.ask-fallback` had no rule anywhere in app.css, so all
three rendered identically. The discriminator for the two engine verdicts is
`status`.

Locking "the template emits three classes" would be too weak, so these tests pin
the whole path, and pin *how* the three are told apart:

1. The three verdicts render three distinct hook classes.
2. Every hook the template can emit is styled, and app.css carries no hook the
   template never emits -- the dead-hook bug this issue is about.
3. The tones still differ once the colour is stripped out of their declarations.
   Colour alone is what reopened #226; a border style survives greyscale and
   colour-vision deficiency.
4. The three glyphs differ, and each is announced as nothing.
5. Every label `pipeline/ask.py` can produce is covered, so a verdict added
   upstream cannot silently inherit another verdict's rendering.

CSS is compared as *parsed declaration sets*, not as "does a rule exist": two
rules with identical bodies are still the bug. Rendering goes through a jinja2
overlay rather than the app, so a stub `base.html` keeps this off the real one.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import pytest
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader

from verinote.pipeline.ask import AskResult

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "verinote" / "web" / "templates"
CSS = ROOT / "verinote" / "web" / "static" / "app.css"
ASK_PY = ROOT / "verinote" / "pipeline" / "ask.py"

TONES = ("verified", "verified-negative", "unverified")

# One AskResult shape per verdict, mirroring what ask_question actually builds.
# Guard 5 checks these labels against the literals in pipeline/ask.py.
CASES = {
    "verified": ("engine", "translated", "VERIFIED — engine"),
    "verified-negative": ("engine", "no_answer", "VERIFIED — engine (negative)"),
    "unverified": ("fallback", "fallback", "UNVERIFIED — source exploration"),
}

# The other (route, status) pair that reaches the fallback verdict: the empty question.
EMPTY_QUESTION = ("fallback", "empty", "UNVERIFIED — source exploration")

# base.html belongs to another lane (#225), so the render runs against a stub that
# provides nothing but the content block.
STUB_BASE = "<html><body>{% block content %}{% endblock %}</body></html>"

HOOK = re.compile(r"ask-verdict-([a-z]+(?:-[a-z]+)*)")
RULE = re.compile(r"(?P<selector>[^{}]+)\{(?P<body>[^{}]*)\}")

# A declaration is "colour" if its property names a colour or its value reaches for a
# palette token. Stripping these is the greyscale simulation guard 3 runs.
COLOUR_PROPERTY = re.compile(r"^(color|background|border(-[a-z]+)*-color|outline-color)$")
# A palette reference *inside* a value. Only the reference is removed, not the whole
# declaration: a `box-shadow`'s offsets are a shape channel even though its colour is not,
# and dropping the declaration wholesale would reject a shadow-only design as
# "colour alone" when its geometry is what carries the distinction.
COLOUR_VAR = re.compile(
    r"var\(\s*--(?:ok|warn|danger|accent|line|muted|panel|fg|bg|term)[a-z-]*\s*\)"
)

# Below here the tests stop trusting that a declaration *exists* and start asking whether
# it draws. Counting declarations lets a rule fake a difference with something inert:
# `font-style: normal` is the inherited default on a <pre>, and `border-style: hidden`
# paints no line, yet both read as "this tone declares something the others do not".
BORDER_STYLE = re.compile(r"^(border|outline)(-[a-z]+)*-style$")
BORDER_WIDTH = re.compile(r"^(border|outline)(-[a-z]+)*-width$")

# The border/outline keywords that actually paint. `none` and `hidden` are the two that
# do not, which is what let solid/hidden/none pass as three distinct channels.
DRAWN_BORDER_STYLES = frozenset(
    {"solid", "dashed", "dotted", "double", "groove", "ridge", "inset", "outset"}
)

# Values that leave a property drawing nothing, whatever the property is.
INERT_VALUES = frozenset({"none", "normal", "hidden", "auto", "initial", "unset", "revert"})

ZERO_LENGTH = re.compile(r"^0[a-z%]*$")
# The painted half of `content: "x" / "alt"`; the half after the slash is the accessible
# name and is never rendered, so it cannot stand in for a glyph.
DRAWN_CONTENT = re.compile(r'^"([^"]*)"')

# CSS writes non-ASCII as `\200b`, optionally followed by one whitespace terminator. The
# stylesheet is read as text, so the escape has to be resolved before the glyph can be
# judged -- otherwise `\200b` reads as five printable characters instead of one invisible
# one, and a zero-width glyph passes for a drawn one.
CSS_ESCAPE = re.compile(r"\\([0-9a-fA-F]{1,6})\s?")

# Unicode categories that occupy the line without marking it: spaces and separators (Zs,
# Zl, Zp) and the control/format characters (Cc, Cf) that include the zero-width family.
INVISIBLE_CATEGORIES = frozenset({"Zs", "Zl", "Zp", "Cc", "Cf"})

# The properties that put a *mark* on the page, as against ones that only move a mark
# around. The verdict has to ride on one of these: `margin-left` and `letter-spacing` do
# change rendering in general, but restating the value the base rule already sets changes
# nothing, and a guard comparing whole declaration sets cannot tell the two apart. Asking
# instead which *signal* differs makes the comparison immune to any declaration bolted on
# beside it, inert or not -- which is the point, since enumerating inert values has no end.
SIGNAL_PROPERTY = re.compile(
    r"^((border|outline)(-[a-z]+)*-(style|width)|box-shadow|content|background-image"
    r"|text-decoration(-[a-z]+)?|font-weight|font-style|text-transform)$"
)


def _result(route: str, status: str, label: str) -> AskResult:
    return AskResult(
        route=route,
        label=label,
        question="q",
        status=status,
        answer="a",
        query_dl=None,
        engine_answers=(),
        reason="r",
    )


def _render(result: AskResult) -> str:
    env = Environment(
        loader=ChoiceLoader([DictLoader({"base.html": STUB_BASE}), FileSystemLoader(TEMPLATES)]),
        autoescape=True,
    )
    return env.get_template("ask.html").render(question="q", error=None, result=result)


def _section_classes(html: str) -> frozenset[str]:
    match = re.search(r'<section class="([^"]*)"', html)
    assert match, f"the result section vanished from the render: {html[:400]}"
    return frozenset(match.group(1).split())


def _rules() -> list[tuple[str, str]]:
    """(selector, declaration body) for every rule in app.css, comments stripped."""
    css = re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.S)
    return [(m.group("selector").strip(), m.group("body")) for m in RULE.finditer(css)]


def _declarations(body: str) -> set[tuple[str, str]]:
    pairs = set()
    for chunk in body.split(";"):
        prop, sep, value = chunk.partition(":")
        if sep:
            pairs.add((prop.strip(), " ".join(value.split())))
    return pairs


def _tones_in(selector: str) -> set[str]:
    return set(HOOK.findall(selector))


def _tone_declarations(tone: str) -> set[tuple[str, str]]:
    """Every declaration that applies to `tone`, tagged by its selector suffix.

    The suffix keeps `::before { content }` from colliding with the label rule's own
    declarations, and keeps the answer-box rule separate from the banner rule.

    Grouped selectors are split on the comma first, so a rule two verdicts *share*
    contributes the same (scope, property, value) to both. Matching the whole grouped
    selector instead would hand each tone a different suffix for identical declarations,
    and guard 3 would then read a shared rule as a difference.
    """
    declarations: set[tuple[str, str]] = set()
    for selector, body in _rules():
        for part in (p.strip() for p in selector.split(",")):
            if _tones_in(part) != {tone}:
                continue
            scope = part.split(f"ask-verdict-{tone}", 1)[1].strip()
            declarations |= {(f"{scope}|{prop}", value) for prop, value in _declarations(body)}
    assert declarations, f"app.css styles nothing for the {tone!r} verdict"
    return declarations


def _without_colour(declarations: set[tuple[str, str]]) -> set[tuple[str, str]]:
    """The greyscale simulation: colour properties dropped, colour references erased.

    Erasing the reference rather than the declaration keeps whatever shape the value
    still carries. `box-shadow: inset 3px 0 0 0 var(--warn)` becomes `inset 3px 0 0 0`,
    which is a real channel; only a value that was *nothing but* colour disappears.
    """
    kept = set()
    for prop, value in declarations:
        if COLOUR_PROPERTY.match(prop.split("|")[-1]):
            continue
        stripped = " ".join(COLOUR_VAR.sub(" ", value).split())
        if stripped:
            kept.add((prop, stripped))
    return kept


def _drawn(prop: str, value: str) -> str | None:
    """The value's drawn form, or None if this declaration paints nothing.

    What it actually rejects, and nothing beyond it:

    - the CSS-wide keywords in INERT_VALUES, on any property;
    - a border/outline *style* that is not one of the painting keywords;
    - a zero length **on a border/outline width only** -- `margin-left: 0` is not judged
      here, because whether it changes anything depends on what the base rule already
      set, which this cannot see;
    - a `content` whose painted half is empty or made entirely of invisible characters.

    Two limits worth naming. It reads declarations, not a rendered box, so a property
    neutralised by its context -- `border-right-style` where no right-hand width is set --
    still counts. And it cannot resolve the cascade, so a declaration that merely restates
    an inherited value reads as a change. Nor are values normalised: `3.0px` and `3px`
    compute identically but compare as two.

    The trust boundary compares signal channels rather than declaration sets to blunt the
    second limit, and the scope of that is worth being exact about, because it is easy to
    claim too much. It removes the dependence entirely for *non-signal* properties: no
    `margin-left: 0` or `letter-spacing: 0` can manufacture a difference, whatever its
    value, because the guard never looks. It does not remove it for the signal properties
    themselves, where a context-inert value still reads as a change -- `font-weight: 400`
    on a <pre> that already inherits 400 is the plain case. So the remaining preimage is
    "a signal property carrying a value that happens to be inert here", which is finite
    and enumerable, rather than "any CSS property at all". Reaching it also takes two
    steps: the verdict's own channel has to be collapsed first, and that alone is red.
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
        # The glyph is whatever precedes the `/ "alt"`. Resolve CSS escapes first, then
        # require at least one character that marks the page: a space or a zero-width
        # joiner occupies the slot and renders nothing, which is a lost glyph, not a glyph.
        glyph = CSS_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), match.group(1))
        return glyph if _paints(glyph) else None
    return value


def _paints(text: str) -> bool:
    """Whether the text puts at least one visible mark on the page."""
    return any(unicodedata.category(char) not in INVISIBLE_CATEGORIES for char in text)


def _visual(declarations: set[tuple[str, str]]) -> set[tuple[str, str]]:
    """Colour stripped, then everything that draws nothing dropped.

    Values are normalised to their drawn form, so two tones cannot look different merely
    because their alt text differs.
    """
    drawn = ((prop, _drawn(prop, value)) for prop, value in _without_colour(declarations))
    return {(prop, value) for prop, value in drawn if value is not None}


def _banner_declaration(tone: str, prop: str) -> str | None:
    """The drawn value of one declaration off the tone's banner rule.

    Scoped to `.ask-verdict-<tone> .ask-verdict` on purpose: the answer body has its own
    rules, and a channel that survives there is no consolation if the banner has lost it.
    Returns None when the declaration is absent *or* inert, so a caller asserting on the
    value cannot be satisfied by a keyword that paints nothing.
    """
    value = dict(_tone_declarations(tone)).get(f".ask-verdict|{prop}")
    return None if value is None else _drawn(prop, value)


def _skeleton_declaration(prop: str) -> str | None:
    """The drawn value of one declaration off the shared `.ask-verdict` banner skeleton.

    The tone rules set only `border-left-style`; the width they paint at lives here, and
    it is not tone-scoped, so `_tone_declarations` never sees it. Zeroing it here would
    erase all three borders while the per-tone styles still read as three distinct values.
    """
    for selector, body in _rules():
        if any(part.strip() == ".ask-verdict" for part in selector.split(",")):
            value = dict(_declarations(body)).get(prop)
            return None if value is None else _drawn(prop, value)
    return None


def _glyphs() -> dict[str, str]:
    glyphs = {}
    for selector, body in _rules():
        if not selector.endswith("::before"):
            continue
        tones = _tones_in(selector)
        if len(tones) != 1:
            continue
        content = dict(_declarations(body)).get("content")
        if content is not None:
            glyphs[tones.pop()] = content
    return glyphs


def test_three_verdicts_render_three_distinct_hook_classes() -> None:
    """Guard 1: including the two that share `route == "engine"`."""
    classes = {tone: _section_classes(_render(_result(*CASES[tone]))) for tone in TONES}

    assert len(set(classes.values())) == len(TONES), (
        f"the verdicts share a hook class ({ {k: sorted(v) for k, v in classes.items()} }); "
        "a verified answer and a verified negative are both route='engine', so the "
        "template has to key off status"
    )
    for tone, rendered in classes.items():
        assert f"ask-verdict-{tone}" in rendered, f"{tone} rendered {sorted(rendered)}"


def test_the_empty_question_reaches_the_unverified_verdict() -> None:
    """The other (route, status) pair ask_question can emit for the fallback label."""
    assert "ask-verdict-unverified" in _section_classes(_render(_result(*EMPTY_QUESTION)))


def test_every_ask_result_verdict_has_a_css_rule() -> None:
    """Guard 2: the emitted hooks and the styled hooks agree as sets.

    This is the guard aimed at the original bug -- `ask-engine` / `ask-fallback` were
    emitted for years with no rule behind either.
    """
    emitted = {
        tone
        for case in (*CASES.values(), EMPTY_QUESTION)
        for cls in _section_classes(_render(_result(*case)))
        for tone in HOOK.findall(cls)
    }
    styled = {tone for selector, _ in _rules() for tone in _tones_in(selector)}

    assert emitted == set(TONES), f"the template emits {sorted(emitted)}, expected {list(TONES)}"
    assert styled == set(TONES), (
        f"app.css styles {sorted(styled)}, expected {list(TONES)}; a hook the template "
        "never emits is dead style, and one it emits without a rule renders as nothing"
    )


def test_verdict_rules_differ_beyond_colour() -> None:
    """Guard 3: strip the colour and everything inert; the three must still differ (#226).

    Comparing raw declarations here would let a tone manufacture a difference out of a
    no-op -- `font-style: normal` changes nothing on a <pre> but reads as a declaration
    the others lack -- so the comparison runs on drawn values only.
    """
    stripped = {tone: frozenset(_visual(_tone_declarations(tone))) for tone in TONES}

    for tone in TONES:
        assert stripped[tone], (
            f"the {tone!r} verdict is carried by colour alone -- nothing survives the "
            "greyscale strip, so a greyscale print or a red-green deficiency erases it"
        )
    assert len(set(stripped.values())) == len(TONES), (
        "two verdicts are identical once the colour is removed: "
        f"{ {k: sorted(v) for k, v in stripped.items()} }"
    )


def test_each_verdict_carries_a_distinct_border_style() -> None:
    """Guard 4a: the banner's shape channel, asserted on its own.

    Guard 3 only demands that *something* non-colour differ, so it stays green when the
    three banners collapse to one border style and the glyphs alone carry the load. That
    is a real loss: the design promises three channels, and unifying `border-left-style`
    quietly drops it to two -- one line, no test. This is the symmetric partner of the
    glyph guard below, so that killing either channel goes red on its own.

    The values are checked for being *drawn*, not merely present: `solid`/`hidden`/`none`
    is three strings but two renderings, since neither `hidden` nor `none` paints a line.
    """
    styles = {tone: _banner_declaration(tone, "border-left-style") for tone in TONES}

    assert _skeleton_declaration("border-left-width"), (
        "the shared `.ask-verdict` rule paints no border width, so none of the three "
        "styles below renders however distinct they read"
    )
    for tone in TONES:
        override = dict(_tone_declarations(tone)).get(".ask-verdict|border-left-width")
        assert override is None or _drawn("border-left-width", override), (
            f"`.ask-verdict-{tone} .ask-verdict` overrides the skeleton width with "
            f"{override!r}, so that one banner paints nothing while its style still reads "
            "as distinct -- the skeleton check above only sees the shared rule"
        )
    for tone, style in styles.items():
        assert style, (
            f"`.ask-verdict-{tone} .ask-verdict` has no border style that paints "
            f"(got {style!r}); `none` and `hidden` draw nothing"
        )
    assert len(set(styles.values())) == len(TONES), (
        f"the verdict banners share a border style ({styles}); the shape channel is the "
        "one that survives greyscale, so collapsing it leaves only the glyph and colour"
    )


def test_each_verdict_carries_a_distinct_glyph() -> None:
    """Guard 4: three glyphs that are actually drawn, each announced as nothing.

    Distinctness is compared on the painted half of `content` alone. Comparing the whole
    declaration would count `"" / "a"` and `"" / "b"` as two glyphs when both render
    empty, and would let a tone lose its glyph while the guard stayed green.
    """
    glyphs = _glyphs()

    assert set(glyphs) == set(TONES), f"glyphs found for {sorted(glyphs)}, expected {list(TONES)}"
    drawn = {tone: _drawn("content", content) for tone, content in glyphs.items()}

    for tone, glyph in drawn.items():
        assert glyph, (
            f"the {tone} verdict draws no glyph (content {glyphs[tone]!r}); the painted "
            "half is empty, so only the alt text is left and nothing reaches the eye"
        )
    assert len(set(drawn.values())) == len(TONES), f"the verdicts share a glyph ({drawn})"
    for tone, content in glyphs.items():
        assert content.endswith('/ ""'), (
            f"the {tone} glyph has no `/ \"\"` alt text ({content!r}); it would be announced "
            "ahead of a label that already spells the verdict out"
        )


def test_ask_pipeline_labels_are_the_set_the_ui_styles() -> None:
    """Guard 5: a new label upstream cannot inherit another verdict's rendering.

    Caveat for whoever adds the fourth verdict (`UNVERIFIED — engine unavailable`):
    this reads `label="..."` literals out of the source. All five construction sites in
    ask_question pass a literal today, so the sweep is complete -- but a label assembled
    from an f-string or handed in through a variable would slip past it silently, and
    this guard would keep passing while the new verdict rendered as one of the other
    three. Add it as a literal, or replace the regex with an AST walk.
    """
    labels = set(re.findall(r'label="([^"]+)"', ASK_PY.read_text(encoding="utf-8")))
    covered = {label for _, _, label in (*CASES.values(), EMPTY_QUESTION)}

    assert labels == covered, (
        f"pipeline/ask.py emits {sorted(labels)} but the UI covers {sorted(covered)}; "
        "give the new label a verdict tone before it renders as one of the others"
    )

    # The other half of the coverage claim: each of those labels has to actually come
    # out of the template as its own verdict. Checking only the label set would pass
    # against a template that routes every one of them to the same tone.
    for tone, case in (*CASES.items(), ("unverified", EMPTY_QUESTION)):
        hooks = {h for cls in _section_classes(_render(_result(*case))) for h in HOOK.findall(cls)}
        assert hooks == {tone}, f"{case[2]!r} ({case[1]}) rendered {sorted(hooks)}, wanted {tone!r}"


def _answer_body(tone: str) -> frozenset[tuple[str, str]]:
    """The signal channels the tone puts on the answer body, as drawn values.

    Restricted to SIGNAL_PROPERTY rather than every surviving declaration. Comparing
    whole sets meant any extra declaration read as a difference, so a rule could claim a
    distinction it did not render by restating a value the base rule already set
    (`margin-left: 0`, `letter-spacing: 0`). Reading the channels instead makes this
    immune to whatever else is bolted on: only a change to a mark counts.
    """
    return frozenset(
        (prop, value)
        for prop, value in _visual(_tone_declarations(tone))
        if prop.split("|")[0] == ".answer-box pre" and SIGNAL_PROPERTY.match(prop.split("|")[-1])
    )


@pytest.mark.parametrize("tone", TONES)
def test_the_answer_body_is_toned_too(tone: str) -> None:
    """The answer block used to carry a verdict-blind grey rule regardless of verdict."""
    scopes = {prop.split("|")[0] for prop, _ in _tone_declarations(tone)}

    assert any("answer-box" in scope for scope in scopes), (
        f"the {tone!r} verdict tones its label but not its answer body; the verdict "
        "disappears as soon as the label scrolls off"
    )


def test_the_answer_body_separates_verified_from_unverified() -> None:
    """The answer body must hold the trust boundary on its own, without colour.

    "The answer body is toned" is too weak a claim: the three tones can all point at
    one shared rule and still satisfy it, at which case an LLM-written fallback answer
    renders in exactly the engine's tone. Guard 3 does not catch that either -- a rule
    all three share cancels out of its comparison, leaving the banner to carry the
    difference. So the trust boundary is asserted here directly.

    The two engine verdicts are *meant* to share this rule, so no difference is demanded
    between them; telling positive from negative is the banner's job.
    """
    assert _answer_body("verified"), "no answer-box rule reaches the verified verdict"
    assert _answer_body("verified") == _answer_body("verified-negative"), (
        "the two engine verdicts are meant to share the answer body; the banner "
        "tells positive from negative"
    )
    assert _answer_body("verified") != _answer_body("unverified"), (
        "an unverified answer body renders like a verified one once the colour is "
        "stripped -- the LLM's excerpt-built answer wears the engine's tone"
    )
