# SPDX-License-Identifier: MPL-2.0
"""The review row's badges must sort into two tiers (#223).

A single row can emit up to eleven badges, and before this they all wore the same
`.badge` weight: "this fact contradicts another one" read exactly like "this fact
has two sources". The fix gives the reader one rule -- **colour is a verdict, grey
is context** -- with at most two coloured badges per row (the fact's status and one
trust verdict, chosen by the priority `conflicted > evidence_missing > unsupported
> single_source > accept recommended > corroborated`) and everything else demoted
to a grey chip. A higher caution demotes the lower finding without deleting it.

The CSS guard below is written the way this repo's UI guards have repeatedly *not*
been. Asserting that a rule exists, or that two rules' declaration sets differ, has
an ineffective preimage: `font-style: normal`, `border-width: 0` on a border-less
box or `content: "" / ""` all change the source text while painting nothing, and
four lanes (#294, #221, #225, #220) were broken that way. So nothing here asks
whether a declaration is *present*. Instead:

* a **family** of properties that could legitimately carry a visual tier is named,
  each with its CSS initial value;
* both tiers are *resolved* through `.badge` down to concrete values, so an absent
  declaration and a declaration restating the initial value are the same thing;
* the tiers must differ in at least one member of that family.

`font-style: normal` therefore resolves to `normal` on both sides and buys nothing,
while any real channel -- weight, size, case, opacity, border shape, a drawn glyph
-- passes. `test_the_guard_accepts_other_honest_implementations` runs that reverse
direction explicitly, so the guard cannot quietly harden into "must be font-weight".

Colour properties are excluded from the family on purpose: a tier readable by hue
alone is the defect #226 is about, so hue cannot be what satisfies this file.
"""

from __future__ import annotations

from collections import Counter
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

import verinote.web.app as webapp  # noqa: E402
from verinote.config import Config  # noqa: E402
from verinote.pipeline.trust import fact_trust_summary  # noqa: E402
from verinote.web import create_app  # noqa: E402

WEB = Path(__file__).resolve().parents[1] / "verinote" / "web"
CSS_PATH = WEB / "static" / "app.css"

BASE = ".badge"
VERDICT = ".badge.verdict"
CHIP = ".badge.chip"
RISK = ".badge.verdict.risk"
META_OVERFLOW = ".meta-overflow"
META_SUMMARY = ".meta-overflow > summary"
META_WEBKIT_MARKER = ".meta-overflow > summary::-webkit-details-marker"

# Properties that could honestly carry "verdict vs context", each mapped to the
# value it has when nobody declares it. Resolving to the initial value is what
# makes a no-op declaration (`font-style: normal`) worth nothing here: it lands on
# the same string the other tier already has.
#
# No colour property is a member. Hue is allowed to reinforce the tier -- it does --
# but it must not be the only thing separating the two, or the row stops sorting
# itself in greyscale and for a colour-blind reader (#226).
FAMILY_INITIALS = {
    "font-weight": "400",
    "font-size": "medium",
    "font-style": "normal",
    "font-variant": "normal",
    "text-transform": "none",
    "letter-spacing": "normal",
    "opacity": "1",
    "border-style": "none",
    "border-width": "medium",
    "border-radius": "0",
    "padding": "0",
    "text-decoration-line": "none",
    "filter": "none",
    # Not a property: the drawn half of a `::before` content, so a glyph marker is
    # an accepted implementation. Blank content resolves to "" -- `content: "" / ""`
    # paints nothing and so counts as nothing.
    "glyph": "",
}

# Keyword values of the `border` shorthand, used to split it into the longhands the
# family actually compares. Without this, `border: 1px solid var(--line)` would be
# one opaque string that a colour-only edit could make "differ".
_BORDER_STYLES = {
    "none", "hidden", "dotted", "dashed", "solid",
    "double", "groove", "ridge", "inset", "outset",
}  # fmt: skip

_FONT_WEIGHT_KEYWORDS = {"normal": "400", "bold": "700"}

# Colours a chip is allowed to resolve to: these read as "grey", i.e. not a verdict.
_UNCOLOURED = {"var(--muted)", "var(--line)", "inherit", "currentcolor"}


def _read_css() -> str:
    return CSS_PATH.read_text(encoding="utf-8")


def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


def _rules(css: str) -> list[tuple[str, dict[str, str]]]:
    """Every flat rule as `(normalised selector list, property -> value)`."""
    parsed = []
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", _strip_comments(css)):
        selector = " ".join(match.group(1).split())
        declarations: dict[str, str] = {}
        for chunk in match.group(2).split(";"):
            prop, sep, value = chunk.partition(":")
            if sep:
                declarations[" ".join(prop.split()).lower()] = " ".join(value.split())
        parsed.append((selector, declarations))
    return parsed


def _declarations(selector: str, css: str) -> dict[str, str]:
    """Merged declarations of every rule whose selector is exactly `selector`."""
    merged: dict[str, str] = {}
    for found, declarations in _rules(css):
        if found == selector:
            merged.update(declarations)
    return merged


def _glyph(content: str) -> str:
    """The drawn half of a `content` value, alt text stripped and unquoted.

    `content: "" / ""` has a truthy raw value and draws nothing; this collapses it
    to `""` so it cannot pass itself off as a visual channel.
    """
    drawn = content.split("/")[0] if "/" in content else content
    return drawn.strip().strip("\"'").strip()


def _apply(resolved: dict[str, str], declarations: dict[str, str]) -> None:
    """Fold one rule's declarations into a resolved family, expanding shorthands."""
    for prop, raw in declarations.items():
        value = raw.strip().lower()
        if prop == "border":
            for token in value.split():
                if token in _BORDER_STYLES:
                    resolved["border-style"] = token
                elif re.fullmatch(r"[\d.]+[a-z%]*", token):
                    resolved["border-width"] = token
            continue
        if prop == "text-decoration":
            resolved["text-decoration-line"] = value.split()[0] if value else "none"
            continue
        if prop == "font-weight":
            resolved["font-weight"] = _FONT_WEIGHT_KEYWORDS.get(value, value)
            continue
        if prop in FAMILY_INITIALS:
            resolved[prop] = value


def _resolve(tier: str, css: str) -> dict[str, str]:
    """Concrete family values for `.badge.<tier>`, inherited through `.badge`.

    Cascade order is `.badge` then the tier rule, which is what the browser does:
    both tier selectors are two classes and so outrank the single-class base.
    """
    resolved = dict(FAMILY_INITIALS)
    _apply(resolved, _declarations(BASE, css))
    _apply(resolved, _declarations(tier, css))
    resolved["glyph"] = _glyph(_declarations(f"{tier}::before", css).get("content", ""))
    return resolved


def _difference(css: str) -> set[str]:
    """Family members on which the two tiers actually resolve to different values."""
    verdict = _resolve(VERDICT, css)
    chip = _resolve(CHIP, css)
    return {prop for prop in FAMILY_INITIALS if verdict[prop] != chip[prop]}


def _risk_difference(css: str) -> set[str]:
    """Non-colour channels that distinguish a caution verdict from reassurance.

    The caution selector cascades after `.badge.verdict`, so it must be folded onto
    the resolved verdict values rather than inspected as an isolated declaration.
    That keeps `content: "" / ""` and other no-op declarations from satisfying the
    marker guard.
    """
    verdict = _resolve(VERDICT, css)
    risk = dict(verdict)
    _apply(risk, _declarations(RISK, css))
    content = _declarations(f"{RISK}::before", css).get("content")
    if content is not None:
        risk["glyph"] = _glyph(content)
    return {prop for prop in FAMILY_INITIALS if risk[prop] != verdict[prop]}


def _coloured_classes(css: str) -> set[str]:
    """Single-class selectors that paint a colour readable as a verdict.

    `.badge.chip`'s own `color: var(--muted)` is not one of them -- that is the grey
    the tier is defined by -- and neither is the base border line.
    """
    coloured = set()
    for selector, declarations in _rules(css):
        colours = {
            value.strip().lower()
            for prop, value in declarations.items()
            if prop in ("color", "border-color")
        }
        if not colours - _UNCOLOURED:
            continue
        for part in selector.split(","):
            part = part.strip()
            if re.fullmatch(r"\.[\w-]+", part):
                coloured.add(part[1:])
    return coloured


class _Badges(HTMLParser):
    """Every element carrying the `badge` class, with its classes and its text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.badges: list[tuple[frozenset[str], list[str]]] = []
        self._depth = 0

    def handle_starttag(self, tag, attrs) -> None:
        classes = set((dict(attrs).get("class") or "").split())
        if "badge" in classes:
            self.badges.append((frozenset(classes), []))
            self._depth = 1
        elif self._depth:
            self._depth += 1

    def handle_endtag(self, tag) -> None:
        if self._depth:
            self._depth -= 1

    def handle_data(self, data) -> None:
        if self._depth and data.strip():
            self.badges[-1][1].append(data.strip())


def _client(tmp_path) -> TestClient:
    cfg = Config(
        root=tmp_path,
        db_path=tmp_path / "kb.sqlite",
        provider="anthropic",
        model="m",
        api_key=None,
        base_url=None,
    )
    return TestClient(create_app(cfg))


def _busiest_fixture(tmp_path) -> tuple[TestClient, int]:
    """The row the issue asks for: contradicted, thinly supported, alias-expanded.

    Two engine-tier rivals put the subject/relation into single-valued conflict; the
    reviewed fact is stored under the raw label `설립`, so its canonical relation
    differs too and the alias badge fires. That is eight badges on one row -- enough
    that an unsorted row is unreadable and a sorted one is not.
    """
    client = _client(tmp_path)
    store = client.app.state.store
    a = store.add_source("sources/a.txt", kind="text")
    b = store.add_source("sources/b.txt", kind="text")
    store.add_fact("회사", "established_on", "2020", status="accepted", source_id=a)
    store.add_fact("회사", "established_on", "2021", status="accepted", source_id=b)
    fact_id = store.add_fact("회사", "설립", "2020", status="needs_review", source_id=a)
    return client, fact_id


def _busiest_row(tmp_path) -> list[tuple[frozenset[str], list[str]]]:
    client, fact_id = _busiest_fixture(tmp_path)

    body = client.get("/review").text
    row = re.search(rf'<tr id="fact-{fact_id}".*?</tr>', body, re.DOTALL)
    assert row is not None, f"/review did not render fact {fact_id}"
    parser = _Badges()
    parser.feed(row.group(0))
    return parser.badges


def _signal_markup(response_text: str) -> str:
    cell = re.search(r'<td class="signals">(.*?)</td>', response_text, re.DOTALL)
    assert cell is not None, "the review row has no trust-signals cell"
    return cell.group(1)


def _signal_badges(client: TestClient, fact_id: int) -> list[tuple[frozenset[str], str]]:
    """Badges in the trust cell, excluding the row status verdict."""
    body = client.get("/review").text
    row = re.search(rf'<tr id="fact-{fact_id}".*?</tr>', body, re.DOTALL)
    assert row is not None, f"/review did not render fact {fact_id}"
    parser = _Badges()
    parser.feed(_signal_markup(row.group(0)))
    return [(classes, " ".join(text)) for classes, text in parser.badges]


def _trust_verdict(client: TestClient, fact_id: int) -> tuple[frozenset[str], str]:
    verdicts = [
        (classes, text)
        for classes, text in _signal_badges(client, fact_id)
        if "verdict" in classes
    ]
    assert len(verdicts) == 1, f"trust verdicts rendered: {verdicts}"
    return verdicts[0]


def _single_source_row(tmp_path, *, evidence: bool) -> tuple[TestClient, int]:
    """A review fact with exactly one engine-tier source supporting it."""
    client = _client(tmp_path)
    store = client.app.state.store
    source_id = store.add_source("sources/only.txt", kind="text")
    store.add_fact("Sample Company", "uses", "Sample Service", status="accepted", source_id=source_id)
    fact_id = store.add_fact(
        "Sample Company", "uses", "Sample Service", status="needs_review", source_id=source_id
    )
    if evidence:
        store.add_fact_evidence(
            fact_id=fact_id, source_id=source_id, snippet="Sample Company uses Sample Service."
        )
    return client, fact_id


def _unsupported_row(tmp_path) -> tuple[TestClient, int]:
    """An anchored review fact without any engine-tier support."""
    client = _client(tmp_path)
    store = client.app.state.store
    source_id = store.add_source("sources/only.txt", kind="text")
    fact_id = store.add_fact(
        "Sample Company", "uses", "Sample Service", status="needs_review", source_id=source_id
    )
    store.add_fact_evidence(
        fact_id=fact_id, source_id=source_id, snippet="Sample Company uses Sample Service."
    )
    return client, fact_id


def test_the_two_tiers_differ_in_a_channel_that_is_not_colour() -> None:
    """A verdict must outweigh a chip in something a greyscale printout keeps.

    The comparison is on resolved values, not on declarations, so restating an
    initial value (`font-style: normal`) leaves both tiers identical and fails --
    which is the mutant that has quietly disarmed four earlier guards.
    """
    differing = _difference(_read_css())

    assert differing, (
        f"{VERDICT} and {CHIP} resolve to identical values across every non-colour "
        f"channel {sorted(FAMILY_INITIALS)}; the two tiers would be told apart by hue "
        "alone, so the row does not sort itself in greyscale (#226)"
    )


def test_the_guard_accepts_other_honest_implementations() -> None:
    """The reverse check: any real channel counts, not just the one shipped.

    Each stanza below is a legitimate alternative way to draw the tier. If the guard
    above ever narrows to "must set font-weight", these go red and say so.
    """
    honest = {
        "case": ".badge.verdict { text-transform: uppercase; }",
        "scale": ".badge.chip { font-size: .6rem; }",
        "fade": ".badge.chip { opacity: .7; }",
        "shape": ".badge.chip { border-style: dotted; }",
        "glyph": '.badge.verdict::before { content: "●" / ""; }',
    }
    for name, rule in honest.items():
        assert _difference(f".badge {{ font-size: .78rem; }}\n{rule}"), (
            f"the {name} implementation of the verdict/chip tier draws a real visual "
            f"difference but this guard cannot see it; the family "
            f"{sorted(FAMILY_INITIALS)} is too narrow"
        )

    no_ops = {
        "restated initial": ".badge.verdict { font-style: normal; }",
        "blank glyph": '.badge.verdict::before { content: "" / ""; }',
        "colour only": ".badge.verdict { color: var(--danger); }",
    }
    for name, rule in no_ops.items():
        assert not _difference(f".badge {{ font-size: .78rem; }}\n{rule}"), (
            f"the {name!r} mutant paints no non-colour difference between the tiers "
            "yet this guard passes it; the family has a member that reads presence "
            "rather than effect"
        )


def test_a_caution_verdict_has_a_non_colour_marker() -> None:
    """Cautions and reassurances share the verdict slot, so hue cannot split them."""
    assert _risk_difference(_read_css()), (
        f"{RISK} and {VERDICT} resolve identically across non-colour channels; a "
        "single-source warning becomes an all-clear in greyscale"
    )


def test_the_caution_marker_guard_rejects_noop_content() -> None:
    """The marker guard resolves the cascade instead of accepting source text."""
    base = ".badge { font-size: .78rem; }\n.badge.verdict { font-weight: 700; }\n"
    honest = {
        "glyph": '.badge.verdict.risk::before { content: "!" / ""; }',
        "shape": ".badge.verdict.risk { border-style: double; }",
    }
    for name, rule in honest.items():
        assert _risk_difference(base + rule), f"the {name} marker is visually real"

    no_ops = {
        "blank glyph": '.badge.verdict.risk::before { content: "" / ""; }',
        "colour only": ".badge.verdict.risk { color: var(--warn); }",
        "initial value": ".badge.verdict.risk { font-style: normal; }",
    }
    for name, rule in no_ops.items():
        assert not _risk_difference(base + rule), (
            f"the {name} mutant adds no non-colour caution marker but the guard passed"
        )


def test_single_source_renders_as_a_warning_verdict_with_a_marker(tmp_path) -> None:
    """A valid anchor does not turn one-source support into ordinary context."""
    client, fact_id = _single_source_row(tmp_path, evidence=True)
    summary = fact_trust_summary(client.app.state.store, fact_id)
    assert summary is not None
    assert {"source_backed", "single_source"} <= set(summary.trust_labels)

    classes, text = _trust_verdict(client, fact_id)
    assert text == "single source"
    assert {"trust-single_source", "risk", "verdict"} <= classes
    assert "single source" not in [
        text for classes, text in _signal_badges(client, fact_id) if "chip" in classes
    ], "the promoted single-source finding must not be repeated as a chip"


def test_unsupported_renders_as_a_higher_warning_verdict(tmp_path) -> None:
    """An anchored fact with no engine support must not fall through to a grey chip."""
    client, fact_id = _unsupported_row(tmp_path)
    classes, text = _trust_verdict(client, fact_id)

    assert text == "unsupported"
    assert {"trust-unsupported", "risk", "verdict"} <= classes


def test_evidence_missing_demotes_single_source_once(tmp_path) -> None:
    """The absent anchor outranks thin support, while retaining it once as context."""
    client, fact_id = _single_source_row(tmp_path, evidence=False)
    summary = fact_trust_summary(client.app.state.store, fact_id)
    assert summary is not None
    assert {"evidence_missing", "single_source"} <= set(summary.trust_labels)

    classes, text = _trust_verdict(client, fact_id)
    chips = [text for classes, text in _signal_badges(client, fact_id) if "chip" in classes]
    assert text == "evidence missing"
    assert {"trust-evidence_missing", "risk", "verdict"} <= classes
    assert chips.count("single source") == 1, (
        f"the demoted caution must be retained exactly once, got chips {chips}"
    )


def test_meta_overflow_keeps_two_chips_visible_and_expands_the_rest_once(tmp_path) -> None:
    """The standalone HTMX row uses a native +N disclosure for crowded metadata."""
    client, fact_id = _busiest_fixture(tmp_path)

    response = client.get(f"/facts/{fact_id}/row")
    assert response.status_code == 200
    assert f'<tr id="fact-{fact_id}"' in response.text
    signals = _signal_markup(response.text)
    overflow = re.search(
        r'<details class="meta-overflow">(.*?)</details>', signals, re.DOTALL
    )
    assert overflow is not None, "a crowded row needs a native metadata disclosure"
    assert " open" not in overflow.group(0).split(">", 1)[0], (
        "the overflow must start folded so the +N count actually reduces the row"
    )

    before_overflow = signals[:overflow.start()]
    visible_parser = _Badges()
    visible_parser.feed(before_overflow)
    visible = [" ".join(text) for classes, text in visible_parser.badges if "chip" in classes]
    assert len(visible) == 2, f"the folded row shows {visible}, not exactly two metadata chips"

    summary = re.search(r'<summary class="badge chip">\+(\d+)', overflow.group(1))
    assert summary is not None, "the disclosure summary must expose its +N count as a chip"
    overflow_count = int(summary.group(1))
    assert f"Show {overflow_count} more trust signals" in overflow.group(1), (
        "the terse +N summary needs an accessible expansion name"
    )

    overflow_parser = _Badges()
    overflow_parser.feed(overflow.group(1))
    hidden = [
        " ".join(text)
        for classes, text in overflow_parser.badges
        if "chip" in classes and not " ".join(text).startswith("+")
    ]
    expanded = visible + hidden
    assert len(hidden) == overflow_count, (
        f"the +{overflow_count} control reveals {hidden}; its count is wrong"
    )
    assert len(expanded) == 2 + overflow_count
    assert all(count == 1 for count in Counter(expanded).values()), (
        f"expanding the metadata repeats a chip instead of showing each once: {expanded}"
    )

    parser = _Badges()
    parser.feed(response.text)
    verdicts = [classes for classes, _ in parser.badges if "verdict" in classes]
    assert len(verdicts) == 2, f"the fact row renders {len(verdicts)} verdicts: {verdicts}"


def test_meta_overflow_css_keeps_the_native_control_inline_and_operable() -> None:
    """The +N control must remain a compact summary, not a script-shaped button."""
    css = _read_css()
    assert _declarations(META_OVERFLOW, css).get("display") == "inline"
    summary = _declarations(META_SUMMARY, css)
    assert summary.get("list-style") == "none"
    assert summary.get("cursor") == "pointer"
    assert _declarations(META_WEBKIT_MARKER, css).get("display") == "none"


def test_single_row_partial_keeps_the_trust_unavailable_verdict_without_overflow(
    tmp_path, monkeypatch
) -> None:
    """A partial render may have no trust summary and still needs a valid trust verdict."""
    client = _client(tmp_path)
    fact_id = client.app.state.store.add_fact("Sample", "uses", "Service", status="needs_review")
    monkeypatch.setattr(webapp, "fact_trust_summary", lambda store, fact_id: None)

    response = client.get(f"/facts/{fact_id}/row")
    assert response.status_code == 200
    assert "trust unavailable" in response.text
    assert "meta-overflow" not in response.text
    parser = _Badges()
    parser.feed(_signal_markup(response.text))
    verdicts = [classes for classes, _ in parser.badges if "verdict" in classes]
    assert len(verdicts) == 1
    assert {"risk", "trust-evidence_missing", "verdict"} <= verdicts[0]


def test_the_busiest_row_spends_its_colour_on_at_most_two_badges(tmp_path) -> None:
    """The issue's own acceptance: contradiction + thin support + alias, one row.

    Two is the budget because two is what a reader can hold: the fact's status and
    the one trust verdict that outranks the rest.
    """
    coloured_classes = _coloured_classes(_read_css())
    badges = _busiest_row(tmp_path)
    assert len(badges) >= 6, (
        f"this fixture is meant to be a crowded row but rendered only {len(badges)} "
        "badges; it no longer exercises the thing #223 is about"
    )

    coloured = [
        (sorted(classes), text)
        for classes, text in badges
        if "chip" not in classes and classes & coloured_classes
    ]
    assert len(coloured) <= 2, (
        f"{len(coloured)} badges on one row carry a verdict colour: {coloured}. The "
        "row is allowed the fact's status and one trust verdict, no more"
    )


def test_every_badge_declares_which_tier_it_is_in(tmp_path) -> None:
    """No badge may sit outside the scheme, and no badge may claim both tiers.

    This is what keeps the rule true as badges are added: a twelfth signal dropped
    into the row with a bare `.badge` lands here rather than silently re-flattening
    the hierarchy.
    """
    for classes, text in _busiest_row(tmp_path):
        tiers = classes & {"verdict", "chip"}
        assert len(tiers) == 1, (
            f"badge {' '.join(text) or '(empty)'!r} is in tiers {sorted(tiers)}; every "
            "badge must be exactly one of verdict (a call about the fact) or chip "
            "(context), or the reader's one rule stops holding"
        )


def test_a_coloured_badge_is_never_a_chip(tmp_path) -> None:
    """"Grey means context" fails the moment a chip carries a verdict palette class."""
    coloured_classes = _coloured_classes(_read_css())

    for classes, text in _busiest_row(tmp_path):
        if "chip" not in classes:
            continue
        assert not classes & coloured_classes, (
            f"chip {' '.join(text)!r} also carries {sorted(classes & coloured_classes)}, "
            "a class app.css paints; context would be reading as a verdict"
        )


def test_the_conflict_finding_is_stated_once(tmp_path) -> None:
    """`trust.conflict` and the `conflicted` label were the same finding, twice.

    `_trust_labels` derives the label from that very summary, so the row used to show
    both a `conflicted` label badge and a `conflict` badge for one contradiction.
    """
    said = [
        " ".join(text)
        for classes, text in _busiest_row(tmp_path)
        if " ".join(text).startswith("conflict")
    ]

    assert said == ["conflicted"], (
        f"the row states the contradiction as {said}; one contradiction is one badge, "
        "and it is the verdict"
    )
