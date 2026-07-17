# SPDX-License-Identifier: MPL-2.0
"""The term/string kind of each triple slot has to reach a screen reader (#278).

It used to ride on a hover-only ``title``, which a keyboard or screen-reader user
never gets. The fix puts the kind in the one channel that is unconditionally
announced for a plain ``<span>``: **a real text node**, kept out of the visual
layout by the ``.visually-hidden`` helper.

Why not ``aria-label``? A ``<span>`` has the implicit ARIA role ``generic``, and
``generic`` is *name-prohibited* -- ``aria-label``/``aria-labelledby`` on it are not
required to be honoured, so a conforming AT may ignore the label and announce only
the visible ``person("Ada")``, losing the kind. These tests therefore both forbid
that pattern and check the announced text.

What "announced" means here, honestly: there is no accessible-name library in this
project's dependencies. (``lxml`` is importable -- the ``test`` extra pulls
python-docx, which requires it -- but it is an *undeclared transitive* dep: nothing
here asks for lxml, so the day python-docx drops or swaps it these tests break for a
reason that has nothing to do with them. That, not any CI gap, is why stdlib is used
below.) So instead of a full accname computation, ``_TripleSlots`` parses the real DOM
and concatenates the
descendant *text nodes* of each slot -- which is exactly what an AT reads out for a
name-prohibited ``generic`` span in browse mode. It is an approximation in one
respect: the parser knows nothing about CSS, so it cannot tell that the kind text is
visually hidden but still in the accessibility tree. That half is pinned separately
by ``test_visually_hidden_helper_keeps_the_text_in_the_accessibility_tree``, which
holds the helper to a clip-based implementation instead of ``display: none``.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from verinote.config import Config  # noqa: E402
from verinote.store.fact_input import structural_term  # noqa: E402
from verinote.web import create_app  # noqa: E402

WEB = Path(__file__).resolve().parents[1] / "verinote" / "web"
CSS_PATH = WEB / "static" / "app.css"
TEMPLATES = WEB / "templates"

# The two templates that render a triple's slots.
HOOK_TEMPLATES = ("partials/fact_row.html", "provenance.html")

# The three triple slots, by the class each span carries.
SLOTS = ("subj", "rel", "obj")

HIDDEN_CLASS = "visually-hidden"


def _flatten(text: str) -> str:
    """Collapse runs of whitespace the way a rendering engine does.

    ``str.split()`` treats NBSP as whitespace too, so the separator the template
    uses between value and kind normalises to a single plain space here.
    """
    return " ".join(text.split())


class _TripleSlots(HTMLParser):
    """Pull each triple slot's announced text and visible text out of real markup.

    ``announced`` is every descendant text node of the slot span, which is what a
    screen reader reads for a ``generic`` span. ``visible`` is the same minus any
    subtree under ``.visually-hidden`` -- i.e. what a sighted reader sees. Keeping
    both lets a test say "the kind is announced but NOT drawn", which is the whole
    contract, and catches a fix that leaks a literal "(term)" onto the page.

    ``span_aria_labels`` records every ``<span aria-label=...>`` so the
    name-prohibited pattern can be forbidden outright.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.announced: dict[str, str] = {}
        self.visible: dict[str, str] = {}
        self.span_aria_labels: list[tuple[str, str]] = []
        self._slot: str | None = None
        self._open: list[bool] = []  # one hidden-flag per tag open inside the slot
        self._announced: list[str] = []
        self._visible: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag == "span" and "aria-label" in attributes:
            self.span_aria_labels.append(
                (attributes.get("class") or "", attributes.get("aria-label") or "")
            )
        if self._slot is None:
            slot = next((s for s in SLOTS if s in classes), None)
            if tag == "span" and slot is not None:
                self._slot = slot
                self._open = []
                self._announced = []
                self._visible = []
            return
        self._open.append(HIDDEN_CLASS in classes)

    def handle_endtag(self, tag: str) -> None:
        if self._slot is None:
            return
        if self._open:
            self._open.pop()
            return
        # Closes the slot span itself.
        self.announced[self._slot] = _flatten("".join(self._announced))
        self.visible[self._slot] = _flatten("".join(self._visible))
        self._slot = None

    def handle_data(self, data: str) -> None:
        if self._slot is None:
            return
        self._announced.append(data)
        if not any(self._open):
            self._visible.append(data)


def _parse(markup: str) -> _TripleSlots:
    parser = _TripleSlots()
    parser.feed(markup)
    return parser


def _client(tmp_path) -> TestClient:
    cfg = Config(
        root=tmp_path,
        db_path=tmp_path / "kb.sqlite",
        provider="anthropic",
        model="m",
        api_key=None,
        base_url=None,
    )
    app = create_app(cfg)
    return TestClient(app)


def _add_structural(store, status: str = "candidate", **kwargs) -> int:
    return store.add_fact(
        structural_term('person("Ada")'),
        structural_term("has_role"),
        structural_term('role(person("Ada"), "PI")'),
        status=status,
        **kwargs,
    )


# The value each slot must keep, and the kind that must ride along with it.
STRUCTURAL_EXPECTED = {
    "subj": ('person("Ada")', "term"),
    "rel": ("has_role", "term"),
    "obj": ('role(person("Ada"), "PI")', "term"),
}


def _assert_slots_announce_value_and_kind(markup: str, expected: dict) -> None:
    parsed = _parse(markup)
    assert set(parsed.announced) == set(expected), (
        f"expected slots {sorted(expected)}, parsed {sorted(parsed.announced)}"
    )
    for slot, (value, kind) in expected.items():
        assert parsed.announced[slot] == f"{value} ({kind})", (
            f"slot {slot!r} announces {parsed.announced[slot]!r}; a screen reader has "
            f"to hear the value AND the kind, i.e. '{value} ({kind})'"
        )
        # The kind is for the ear only: it must not show up on the page.
        assert parsed.visible[slot] == value, (
            f"slot {slot!r} draws {parsed.visible[slot]!r} but should draw only "
            f"{value!r}; the kind text has to stay visually hidden"
        )


def test_review_slots_announce_value_and_kind(tmp_path) -> None:
    """Every slot of a structural triple reads as '<value> (term)' in the review."""
    c = _client(tmp_path)
    _add_structural(c.app.state.store)

    _assert_slots_announce_value_and_kind(c.get("/review").text, STRUCTURAL_EXPECTED)


def test_review_string_literal_slot_announces_its_own_kind(tmp_path) -> None:
    """A string literal is the other half of the vocabulary and must say so.

    Same triple shape, but the slots are plain strings: the kind announced has to
    flip to 'string', otherwise the distinction #222 drew visually is inaudible.
    """
    c = _client(tmp_path)
    c.app.state.store.add_fact(
        'person("Ada")', "has_role", 'role(person("Ada"), "PI")', status="candidate"
    )

    _assert_slots_announce_value_and_kind(
        c.get("/review").text,
        {
            "subj": ('"person(\\"Ada\\")"', "string"),
            "rel": ('"has_role"', "string"),
            "obj": ('"role(person(\\"Ada\\"), \\"PI\\")"', "string"),
        },
    )


def test_provenance_slots_announce_value_and_kind(tmp_path) -> None:
    """The provenance page renders its own triple and must carry the kind too."""
    c = _client(tmp_path)
    store = c.app.state.store
    sid = store.add_source("sources/x.txt", kind="text")
    fid = _add_structural(store, status="needs_review", source_id=sid)

    _assert_slots_announce_value_and_kind(
        c.get(f"/facts/{fid}/provenance").text, STRUCTURAL_EXPECTED
    )


@pytest.mark.parametrize("page", ["review", "provenance"])
def test_no_triple_span_leans_on_a_name_prohibited_aria_label(tmp_path, page) -> None:
    """`<span aria-label>` is the pattern this PR must not ship.

    A span's implicit role is ``generic``, which prohibits an author-provided
    accessible name, so an AT may drop the label and announce the visible text
    alone -- the kind would be silently lost. Forbidden on both render paths.
    """
    c = _client(tmp_path)
    store = c.app.state.store
    sid = store.add_source("sources/x.txt", kind="text")
    fid = _add_structural(store, status="needs_review", source_id=sid)

    markup = (
        c.get("/review").text
        if page == "review"
        else c.get(f"/facts/{fid}/provenance").text
    )

    labelled = _parse(markup).span_aria_labels
    assert labelled == [], (
        f"{page} renders <span aria-label=...>: {labelled}. A span is role=generic, "
        "which is name-prohibited -- put the text in a visually-hidden text node."
    )


@pytest.mark.parametrize("page", ["review", "provenance"])
def test_the_kind_no_longer_hides_in_a_hover_only_title(tmp_path, page) -> None:
    """The bug #278 reported: kind reachable only by hovering a mouse."""
    c = _client(tmp_path)
    store = c.app.state.store
    sid = store.add_source("sources/x.txt", kind="text")
    fid = _add_structural(store, status="needs_review", source_id=sid)

    markup = (
        c.get("/review").text
        if page == "review"
        else c.get(f"/facts/{fid}/provenance").text
    )

    assert 'title="term"' not in markup
    assert 'title="string"' not in markup


def test_both_templates_carry_the_visually_hidden_kind_text() -> None:
    """Neither render path may be left behind.

    fact_row.html and provenance.html each render a triple; a fix applied to one
    only would leave the other silently on the old behaviour, and the end-to-end
    tests above would not say which.
    """
    for name in HOOK_TEMPLATES:
        html = (TEMPLATES / name).read_text(encoding="utf-8")
        hidden = re.findall(
            rf'<span class="{HIDDEN_CLASS}">[^<]*\{{\{{\s*f\[[\'"](\w+)_kind[\'"]\]\s*\}}\}}',
            html,
        )
        assert sorted(hidden) == ["object", "relation", "subject"], (
            f"{name} emits visually-hidden kind text for {sorted(hidden)}; all three "
            "slots (subject, relation, object) need it"
        )


def _rules(css: str) -> list[tuple[str, str, int]]:
    """Every rule in the sheet as ``(selector, body, nesting_depth)``.

    Depth 0 is the top level; a rule inside ``@media`` and friends sits deeper.
    Written as a brace scanner rather than a regex because a regex can only find *a*
    rule, and what matters here is finding *all* of them plus where each one sits.
    """
    rules: list[tuple[str, str, int]] = []
    stack: list[tuple[str, int, int]] = []
    depth = 0
    start = 0
    for i, ch in enumerate(css):
        if ch == "{":
            stack.append((" ".join(css[start:i].split()), depth, i + 1))
            depth += 1
            start = i + 1
        elif ch == "}":
            depth -= 1
            if stack:
                selector, rule_depth, body_start = stack.pop()
                rules.append((selector, css[body_start:i], rule_depth))
            start = i + 1
    return rules


def test_visually_hidden_helper_keeps_the_text_in_the_accessibility_tree() -> None:
    """The helper must hide from eyes only -- not from assistive tech.

    This is the half the DOM parser cannot see. ``display: none`` and
    ``visibility: hidden`` prune the node from the accessibility tree as well, so a
    helper written that way would render this whole PR a no-op while every markup
    assertion above still passed. Pin the clip-based idiom instead.

    Checked across the *whole* sheet, not just the first matching rule. CSS cascades:
    a later rule of equal specificity wins, so reading one rule and stopping would
    wave through a `display: none` appended at the bottom of app.css -- precisely the
    failure this test exists to catch -- and app.css grows by appending. Sitting at
    the top level matters for the same reason: tucked inside `@media print` the helper
    would not apply on screen at all.
    """
    css = re.sub(r"/\*.*?\*/", "", CSS_PATH.read_text(encoding="utf-8"), flags=re.DOTALL)

    matching = [rule for rule in _rules(css) if rule[0] == f".{HIDDEN_CLASS}"]
    assert len(matching) == 1, (
        f"app.css has {len(matching)} `.{HIDDEN_CLASS}` rules; expected exactly 1. "
        "Zero means the helper is gone. Two or more means the cascade decides, and a "
        "later one silently overrides the hiding this test checks below."
    )
    _, body, depth = matching[0]
    assert depth == 0, (
        f"the `.{HIDDEN_CLASS}` rule is nested {depth} level(s) deep (inside an "
        "at-rule such as @media); it would not apply on screen and the kind text "
        "would be painted onto the page"
    )

    declarations = {}
    for chunk in body.split(";"):
        prop, sep, value = chunk.partition(":")
        if sep:
            declarations[" ".join(prop.split())] = " ".join(value.split())

    assert declarations.get("display") != "none", (
        ".visually-hidden uses display:none, which removes the kind from the "
        "accessibility tree too -- screen readers would never announce it"
    )
    assert declarations.get("visibility") != "hidden", (
        ".visually-hidden uses visibility:hidden, which is also pruned from the "
        "accessibility tree"
    )
    # The clipped-1px-box idiom: rendered (so AT keep it) but not drawn.
    assert declarations.get("position") == "absolute", (
        ".visually-hidden must be taken out of flow, or it shifts the triple layout"
    )
    assert declarations.get("overflow") == "hidden", (
        ".visually-hidden needs overflow:hidden or the text spills out of its 1px box"
    )
    # Values, not just presence. Asserting `"clip-path" in declarations` would wave
    # through `clip-path: inset(0)`, which clips away nothing, and a box with no size
    # cap, which leaves the kind text painted over the page: hidden from nobody, while
    # every markup assertion above still passes. The three below are what actually make
    # the text unpaintable.
    assert (declarations.get("width"), declarations.get("height")) == ("1px", "1px"), (
        ".visually-hidden must collapse to a 1px box; without a size cap the kind text "
        f"renders on top of the page (got width={declarations.get('width')!r}, "
        f"height={declarations.get('height')!r})"
    )
    assert declarations.get("clip-path") == "inset(50%)", (
        ".visually-hidden must clip the text away with inset(50%); e.g. inset(0) clips "
        f"nothing and leaves the kind visible (got {declarations.get('clip-path')!r})"
    )
