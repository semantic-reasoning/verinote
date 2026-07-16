# SPDX-License-Identifier: MPL-2.0
"""Regression lock on term-kind styling (#222).

fact_row.html and provenance.html tag every triple span with `term-{{ kind }}`,
where `kind` is whatever `term_input_kind` returns -- either `string` (a StringLit)
or `term` (anything else). Issue #51 shipped those hooks but left app.css with no
matching rules, so `.term-term` and `.term-string` rendered identically: the reader
could not tell a structural Datalog term from a bare string literal.

These tests pin three things, each written so reverting the CSS turns them red:

1. **The two kinds are styled differently**, compared by parsed declaration set
   rather than "does a rule exist" -- two rules with identical bodies would still be
   the bug. Concretely `.term-term` must carry a `background` chip and `.term-string`
   must not, which is the at-a-glance distinction the issue asked for.
2. **The rules are unscoped.** Scoping them under `.facts` would silently drop the
   provenance page's `<p class="triple">` (it lives under `.trust-block`, not
   `.facts`), reintroducing the bug on exactly one of the two call sites.
3. **The templates still emit the hooks**, and `term_input_kind`'s vocabulary is
   exactly `{string, term}` -- so the two classes the CSS styles are the complete set
   the markup can produce, not a coincidental pair.
"""

from __future__ import annotations

import re
from pathlib import Path

from verinote.engine.terms import Atom, Compound, NumberLit, StringLit, Var
from verinote.store.fact_input import term_input_kind

WEB = Path(__file__).resolve().parents[1] / "verinote" / "web"
CSS_PATH = WEB / "static" / "app.css"
TEMPLATES = WEB / "templates"

# The templates that render triple spans with a term-kind class.
HOOK_TEMPLATES = ("partials/fact_row.html", "provenance.html")


def _read_css() -> str:
    return CSS_PATH.read_text(encoding="utf-8")


def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


def _declarations(selector: str, css: str) -> dict[str, str]:
    """Parsed `property -> value` for the flat rule whose selector is exactly `selector`.

    Returns `{}` when no such rule exists, so a *missing* `.term-string` rule reads as
    the empty declaration set -- which is still legitimately different from `.term-term`
    and correctly carries no `background`.
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


def test_term_and_string_kinds_are_styled_differently() -> None:
    """The whole point of #222: the two kinds must not render the same.

    Compared by declaration set, so two rules with identical bodies (the pre-fix
    state, where both were unstyled) fail rather than pass on "a rule exists".
    """
    css = _read_css()
    term = _declarations(".term-term", css)
    string = _declarations(".term-string", css)

    assert term, "app.css defines no .term-term rule; structural terms render unstyled"
    assert term != string, (
        f".term-term and .term-string carry identical declarations ({term}); a "
        "structural term and a string literal would look the same, which is bug #222."
    )


def test_only_the_term_kind_wears_a_chip() -> None:
    """The at-a-glance distinction: a structural term is a chip, a string is plain.

    `.term-term` must set a `background`; `.term-string` must not (a string literal
    reads as plain prose). Reverting either half of the CSS flips one assertion.
    """
    css = _read_css()
    term = _declarations(".term-term", css)
    string = _declarations(".term-string", css)

    assert "background" in term, ".term-term must carry a chip background"
    assert "background" not in string, (
        ".term-string must stay plain prose (no background), or it blurs into a term chip"
    )


def test_term_rules_are_unscoped() -> None:
    """A `.facts`-scoped rule would miss provenance.html's `<p class="triple">`.

    The provenance page renders the triple outside `.facts`, so the term-kind rules
    must match the bare class. Guard against a descendant-scoped selector sneaking in.
    """
    css = _strip_comments(_read_css())
    for selector in (".term-term", ".term-string"):
        assert re.search(rf"(?m)^\s*{re.escape(selector)}\s*(::before)?\s*\{{", css), (
            f"app.css has no unscoped `{selector}` rule; a scoped one would leave the "
            "provenance page's <p class='triple'> spans untagged"
        )
        assert not re.search(rf"\.facts[^{{}}]*{re.escape(selector)}\b", css), (
            f"`{selector}` is scoped under .facts; the provenance triple would go untagged"
        )


def test_templates_still_emit_the_term_kind_hooks() -> None:
    """CSS is dead unless the markup keeps emitting `term-{{ kind }}` classes."""
    for name in HOOK_TEMPLATES:
        html = (TEMPLATES / name).read_text(encoding="utf-8")
        assert re.search(r"term-\{\{\s*f\[['\"]\w+_kind['\"]\]\s*\}\}", html), (
            f"{name} no longer emits a term-{{{{ ..._kind }}}} class; the .term-* rules "
            "would have nothing to style"
        )


def test_term_input_kind_vocabulary_is_exactly_string_and_term() -> None:
    """The CSS styles two classes; this proves those two are the complete vocabulary.

    `term_input_kind` maps every Term variant to one of two words. If a third ever
    appeared, `.term-<that>` would render unstyled and this test would catch it before
    the CSS silently fell short.
    """
    samples = (
        StringLit("plain text"),
        Atom("subject_of"),
        NumberLit(7),
        Compound("addr", (Atom("home"),)),
        Var("X"),
    )
    kinds = {term_input_kind(term) for term in samples}
    assert kinds == {"string", "term"}, (
        f"term_input_kind produced {sorted(kinds)}; app.css only styles "
        ".term-string and .term-term, so any other value renders unstyled"
    )
    assert term_input_kind(StringLit("x")) == "string"
    assert term_input_kind(Atom("x")) == "term"
