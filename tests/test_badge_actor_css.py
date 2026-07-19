# SPDX-License-Identifier: MPL-2.0
"""Regression lock on the accepted-vs-confirmed badge (#221).

`confirmed` and `accepted` are different events: `accept_fact()` records a person's
call (actor defaults to the user), `auto_accept_fact()` records a rule's
(`actor="rule"`, with a rule_name). Both land in `ENGINE_STATUSES`, so a
rule-accepted fact reasons with exactly the authority of a human-accepted one --
yet app.css styled them with a single shared rule and fact_row.html shows no actor,
so the review screen gave the reader no way to tell which had happened.

These tests pin the distinction the way #226 demands it be pinned:

1. **The two badges are genuinely different rules**, compared by parsed declaration
   set rather than "a rule exists" -- two rules with identical bodies would still be
   the bug.
2. **The difference is not colour alone.** Colour properties are stripped before the
   comparison, so "fix" the bug by re-tinting one badge and this stays red. #226 is
   about verdicts that depend on colour alone; this is the guard that stops #221's
   fix from reopening it.
3. **Both stay in the ok tone**, which is what the issue asked for -- the actor is
   the difference, not the verdict.
4. **The glyph carries empty alt text**, so it never leaks into a screen reader's
   reading of the badge (the badge text already says "accepted").
5. **`.badge-confirmed` carries no glyph.** That class doubles as a generic ok chip
   ("covered" on the dashboard, "OK" in the report), so an approval mark there would
   land on text that is not an accept decision at all.
6. **The template still emits `badge-{{ status }}`**, without which the CSS is dead.
"""

from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "verinote" / "web"
CSS_PATH = WEB / "static" / "app.css"
TEMPLATES = WEB / "templates"

# Properties that carry colour and nothing else. Stripped before the "are they really
# different" comparison so a colour-only distinction cannot satisfy it.
COLOUR_PROPERTIES = ("color", "border-color", "background")


def _read_css() -> str:
    return CSS_PATH.read_text(encoding="utf-8")


def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


def _declarations(selector: str, css: str) -> dict[str, str]:
    """Parsed `property -> value` for the flat rule whose selector is exactly `selector`.

    Returns `{}` when no such rule exists -- so the pre-fix state, where neither
    `.badge-confirmed` nor `.badge-accepted` appeared alone (both lived in one grouped
    selector), reads as two identical empty sets and fails the difference tests.
    """
    css = _strip_comments(css)
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        if " ".join(match.group(1).split()) != selector:
            continue
        declarations: dict[str, str] = {}
        for chunk in match.group(2).split(";"):
            prop, sep, value = chunk.partition(":")
            if sep:
                declarations[" ".join(prop.split())] = " ".join(value.split())
        return declarations
    return {}


def _without_colour(declarations: dict[str, str]) -> dict[str, str]:
    return {
        prop: value
        for prop, value in declarations.items()
        if not prop.startswith(COLOUR_PROPERTIES)
    }


def test_confirmed_and_accepted_are_not_the_same_rule() -> None:
    """The whole point of #221: a rule's pass must not look like a person's pass."""
    css = _read_css()
    confirmed = _declarations(".badge-confirmed", css)
    accepted = _declarations(".badge-accepted", css)

    assert accepted, (
        "app.css defines no standalone .badge-accepted rule; a rule-accepted fact "
        "renders identically to a human-confirmed one, which is bug #221"
    )
    assert confirmed != accepted, (
        f".badge-confirmed and .badge-accepted carry identical declarations "
        f"({confirmed}); the reviewer cannot tell who passed the fact"
    )


def test_the_difference_is_not_colour_alone() -> None:
    """Re-tinting one badge is not a fix -- #226 rejects verdicts that ride on colour.

    Strip the colour properties from both rules; something must still separate them,
    either a non-colour declaration or the `::before` glyph. Greyscale printouts and
    colour-blind readers get the distinction either way.
    """
    css = _read_css()
    confirmed = _without_colour(_declarations(".badge-confirmed", css))
    accepted = _without_colour(_declarations(".badge-accepted", css))
    glyph = _declarations(".badge-accepted::before", css)

    assert confirmed != accepted or "content" in glyph, (
        "with colour removed, .badge-confirmed and .badge-accepted are indistinguishable "
        f"({confirmed}) and .badge-accepted::before sets no content -- the actor would be "
        "readable by hue alone, which is exactly what #226 says must not happen"
    )


def test_both_stay_in_the_ok_tone() -> None:
    """Both statuses are passes; the issue asked for the same ok tone, actor aside.

    Also blocks the escape hatch of splitting them by moving `accepted` onto the warn
    or danger palette, which would read as a worse verdict rather than a different actor.
    """
    css = _read_css()
    for selector in (".badge-confirmed", ".badge-accepted"):
        declarations = _declarations(selector, css)
        assert declarations.get("color") == "var(--ok)", (
            f"{selector} left the ok tone (color={declarations.get('color')!r}); "
            "both statuses are passes, so the actor must be the only difference"
        )
        assert declarations.get("border-color") == "var(--ok)", (
            f"{selector} left the ok tone (border-color="
            f"{declarations.get('border-color')!r})"
        )


def test_accepted_glyph_has_empty_alt_text() -> None:
    """The glyph is decorative; the badge text already announces "accepted".

    `content: "..." / ""` gives the pseudo-element an empty accessible name, following
    `.term-term::before` (#222). Without the empty alt, screen readers would read a
    stray gear character into the middle of the status.
    """
    css = _read_css()
    content = _declarations(".badge-accepted::before", css).get("content")

    assert content, ".badge-accepted::before sets no content; the actor glyph is gone"
    assert re.search(r'/\s*""\s*$', content), (
        f".badge-accepted::before content is {content!r}, with no `/ \"\"` alt text; "
        "the decorative glyph would be announced as part of the status"
    )


def test_confirmed_carries_no_actor_glyph() -> None:
    """`.badge-confirmed` doubles as a generic ok chip, so a glyph there misfires.

    dashboard.html renders "covered" and report.html renders "OK" with this class.
    Neither is an accept decision, so an approval mark on them would claim something
    the data does not say.
    """
    css = _strip_comments(_read_css())

    assert not re.search(r"\.badge-confirmed\s*::?before\s*\{", css), (
        "app.css gives .badge-confirmed a ::before glyph, which would also stamp the "
        "dashboard's 'covered' chip and the report's 'OK' chip -- neither is an accept"
    )


def test_templates_still_emit_the_status_badge_class() -> None:
    """CSS is dead unless the markup keeps deriving the class from the status string."""
    html = (TEMPLATES / "partials" / "fact_row.html").read_text(encoding="utf-8")

    assert re.search(r"badge-\{\{\s*f\[['\"]status['\"]\]\s*\}\}", html), (
        "fact_row.html no longer emits badge-{{ f['status'] }}; .badge-accepted and "
        ".badge-confirmed would have nothing to style"
    )
