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
COLOUR_TOKEN = re.compile(r"--(ok|warn|danger|accent|line|muted|panel|fg|bg|term)")


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
    return {
        (prop, value)
        for prop, value in declarations
        if not COLOUR_PROPERTY.match(prop.split("|")[-1]) and not COLOUR_TOKEN.search(value)
    }


def _banner_declaration(tone: str, prop: str) -> str | None:
    """One declaration off the tone's banner rule (`.ask-verdict-<tone> .ask-verdict`).

    Scoped to the banner on purpose: the answer body has its own rules, and a channel
    that survives there is no consolation if the banner has lost it.
    """
    return dict(_tone_declarations(tone)).get(f".ask-verdict|{prop}")


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
    """Guard 3: strip every colour declaration; the three must still differ (#226)."""
    stripped = {tone: frozenset(_without_colour(_tone_declarations(tone))) for tone in TONES}

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
    """
    styles = {tone: _banner_declaration(tone, "border-left-style") for tone in TONES}

    for tone, style in styles.items():
        assert style, f"`.ask-verdict-{tone} .ask-verdict` declares no border-left-style"
    assert len(set(styles.values())) == len(TONES), (
        f"the verdict banners share a border style ({styles}); the shape channel is the "
        "one that survives greyscale, so collapsing it leaves only the glyph and colour"
    )


def test_each_verdict_carries_a_distinct_glyph() -> None:
    """Guard 4: three glyphs, each with the `/ ""` alt text of the .term-term rule."""
    glyphs = _glyphs()

    assert set(glyphs) == set(TONES), f"glyphs found for {sorted(glyphs)}, expected {list(TONES)}"
    assert len(set(glyphs.values())) == len(TONES), f"the verdicts share a glyph ({glyphs})"
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


def _answer_body(tone: str) -> set[tuple[str, str]]:
    """The tone's answer-box declarations, colour stripped."""
    return {d for d in _without_colour(_tone_declarations(tone)) if "answer-box" in d[0]}


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
