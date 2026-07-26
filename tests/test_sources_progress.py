# SPDX-License-Identifier: MPL-2.0
"""Chunk progress on the Sources page: a bar, and a poll that stops eating the page (#228).

Extraction is the only long-running task in the app, and the Sources page reported it
as `3/8 chunk(s)` -- a string you have to read one row at a time. Meanwhile the 2s poll
carried `hx-target="main" hx-select="main"`, so every tick replaced the heading, the
upload form and the table as one unit and took scroll position and focus with it.

Narrowing that to `#sources-table` was not enough, which is why this file has a second
poll section. The table holds Retry, Re-analyze, Accept all and Delete; replacing it
pulls whichever of them has keyboard focus out of the document. So the tests below do
not ask whether the swap avoids the page chrome -- they ask what set of live nodes a
tick destroys, computed from all three of htmx's removal channels at once, and require
that set to contain every progress bar, to leave the chrome alone, and to contain no
control htmx could not hand the focus back to.

That last clause is weaker than "no control at all", and deliberately: htmx 2.0.9,
which this repo vendors, restores focus across a swap by id (see
`test_a_tick_only_replaces_controls_htmx_can_hand_the_focus_back_to`). Excluding the
actions cell instead is not just unnecessary, it is its own bug -- the row's buttons
are conditional on the counts the tick delivers, so a cell left out of the swap goes
stale against the cell beside it. The last poll section pins that.

WHAT THESE TESTS ASSERT, AND WHY NOT THE OBVIOUS THING. Asserting "app.css contains a
`.progress` rule" or "sources.html mentions a bar" is worthless here: both stay green
under a one-line edit that changes the text and nothing a reader would see. So nothing
below reads the stylesheet. Every assertion is made against the *rendered page* of a
KB whose chunk counts are known, and it targets the channel the feature actually
travels on:

* the **machine-readable value** -- `aria-valuenow`/`aria-valuemax`, or a native
  `<progress value max>`; this is what a screen reader announces;
* the **proportional length** -- a percentage on any of `width` / `inline-size` /
  `flex-basis` / a custom property, or the value/max of a native `<progress>`, whose
  box the browser draws in proportion for you.

Both channels are checked against *two sources with different ratios in one render*
(3/8 and 1/4), so a hardcoded constant -- the cheapest way to fake either -- cannot
satisfy both at once. The family of accepted properties is deliberately wide: a bar
built from `inline-size`, from a `--progress` custom property, or from a native
`<progress>` element is a legitimate alternative implementation and stays green.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from verinote.config import Config  # noqa: E402
from verinote.web import create_app  # noqa: E402

CSS_PATH = Path(__file__).resolve().parents[1] / "verinote" / "web" / "static" / "app.css"

VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
     "param", "source", "track", "wbr"}
)

# Properties that can carry "how far along is it" as a length. `width` is what the
# shipped bar uses; the rest are the honest alternatives (logical property, flex
# sizing, or a custom property the stylesheet reads back). Custom properties are
# matched by shape rather than by name so the implementation is free to pick one.
PROPORTION_DECL = re.compile(
    r"(?:^|[;{\s])(?:width|inline-size|flex-basis|--[A-Za-z0-9_-]+)\s*:\s*"
    r"(\d+(?:\.\d+)?)\s*%"
)


class _Doc(HTMLParser):
    """A minimal element tree: every start tag with its attributes, ancestors and text.

    Enough to ask "is X inside Y", which is the only structural question here. Using
    a parser rather than a regex matters for the poll tests: proving that the swapped
    region does not contain the page heading -- or the Delete button -- is a
    containment question, and a regex over the source text can only guess at it.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.nodes: list[dict] = []
        self._open: list[int] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        index = len(self.nodes)
        self.nodes.append(
            {
                "tag": tag, "attrs": dict(attrs), "ancestors": list(self._open),
                "index": index, "text": [],
            }
        )
        if tag not in VOID_TAGS:
            self._open.append(index)

    def handle_data(self, data: str) -> None:
        if self._open:
            self.nodes[self._open[-1]]["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        for depth in range(len(self._open) - 1, -1, -1):
            if self.nodes[self._open[depth]]["tag"] == tag:
                del self._open[depth:]
                return

    def find(self, predicate) -> list[dict]:
        return [node for node in self.nodes if predicate(node)]

    def descendants(self, node: dict) -> list[dict]:
        return [other for other in self.nodes if node["index"] in other["ancestors"]]

    def contains(self, outer: dict, inner: dict) -> bool:
        return outer["index"] in inner["ancestors"]

    def subtree(self, node: dict) -> list[dict]:
        return [node, *self.descendants(node)]

    def text(self, node: dict) -> str:
        """The element's visible text, whitespace-collapsed.

        Used only to name a button by its label, where the text is a direct child, so
        the flattening of nested runs is not something any caller here depends on.
        """
        parts = [chunk for element in self.subtree(node) for chunk in element["text"]]
        return " ".join("".join(parts).split())


def _parse(html: str) -> _Doc:
    doc = _Doc()
    doc.feed(html)
    return doc


def _indicators(doc: _Doc) -> list[dict]:
    """Every progress indicator, however it is built.

    `role="progressbar"` and the native `<progress>` element are the two ways to say
    "this is a progress indicator" to an assistive technology. Anything that conveys
    progress without being one of them is not accessible, so the net is drawn here.
    """
    return doc.find(
        lambda node: node["attrs"].get("role") == "progressbar" or node["tag"] == "progress"
    )


def _reported_counts(node: dict) -> tuple[float, float]:
    """The (now, max) an indicator announces, from ARIA or from `<progress>`."""
    attrs = node["attrs"]
    now = attrs.get("aria-valuenow", attrs.get("value"))
    ceiling = attrs.get("aria-valuemax", attrs.get("max"))
    assert now is not None and ceiling is not None, (
        f"progress indicator {attrs!r} announces no value; a screen reader would hear "
        "an empty progress bar where the sighted reader sees a filled one"
    )
    return float(now), float(ceiling)


def _drawn_fractions(doc: _Doc, node: dict) -> dict[int, float]:
    """Map `element index -> percentage` for the proportional lengths this bar draws.

    Keyed by element so a caller can tell "one segment at 25%" from "two segments, one
    of them at 25%" -- which is the difference between drawing the failures and not.
    """
    if node["tag"] == "progress":
        now, ceiling = _reported_counts(node)
        # The browser draws a native <progress> in proportion; the attributes are the
        # geometry, so they count as the visual channel too.
        return {node["index"]: 100.0 * now / ceiling if ceiling else 0.0}
    fractions: dict[int, float] = {}
    for element in [node, *doc.descendants(node)]:
        match = PROPORTION_DECL.search(element["attrs"].get("style", ""))
        if match:
            fractions[element["index"]] = float(match.group(1))
    return fractions


# --- the KB under test ------------------------------------------------------
#
# Two analysed sources with deliberately different ratios, plus one whose job exists
# but has no chunks yet. The ratios (3/8, 1/4 done and 2/4 failed) share no value, so
# 37.5, 25.0 and 50.0 are all distinguishable and none of them is a round number a
# placeholder would land on by chance.

SOURCES = {
    # path -> (total chunks, chunks to mark done, chunks to mark failed, chunks left running)
    "running.txt": (8, 3, 0, 1),
    "partly-failed.txt": (4, 1, 2, 0),
}

# Every button the Sources row offers. Two of them are conditional -- Retry needs a
# failed chunk and Accept all needs an unresolved candidate -- so the fixture below
# arranges for all four to render, and `test_the_fixture_renders_every_row_action`
# fails loudly if that stops being true. Otherwise "no tick removes these controls"
# would pass on a page that has none of them.
ROW_ACTIONS = ("Retry", "Re-analyze", "Accept all", "Delete")


@pytest.fixture()
def page(tmp_path):
    """A rendered /sources page plus the store it was rendered from."""
    cfg = Config(
        root=tmp_path, db_path=tmp_path / "kb.sqlite",
        provider="anthropic", model="m", api_key=None, base_url=None,
    )
    app = create_app(cfg)
    client = TestClient(app)
    store = app.state.store

    jobs: dict[str, int] = {}
    for path, (total, done, failed, running) in SOURCES.items():
        source_id = store.add_source(path, "text")
        job_id = store.create_extraction_job(
            source_id=source_id, provider="anthropic", model="m", total_chunks=total
        )
        chunk_ids = store.add_source_chunks(
            job_id=job_id, source_id=source_id, chunks=[f"chunk {i}" for i in range(total)]
        )
        cursor = 0
        for chunk_id in chunk_ids[cursor : cursor + done]:
            store.mark_chunk_done(chunk_id)
        cursor += done
        for chunk_id in chunk_ids[cursor : cursor + failed]:
            store.mark_chunk_failed(chunk_id, "boom")
        cursor += failed
        for chunk_id in chunk_ids[cursor : cursor + running]:
            store.mark_chunk_running(chunk_id)
        # An unresolved candidate is what puts "Accept all" on the row; without one,
        # the poll tests would be checking a row that is missing an action.
        store.add_fact(f"{path} subject", "relates to", "object", source_id=source_id)
        jobs[path] = job_id

    response = client.get("/sources")
    assert response.status_code == 200, response.text
    return {
        "html": response.text,
        "doc": _parse(response.text),
        "store": store,
        "jobs": jobs,
    }


def _job_row(page, path: str):
    row = page["store"].get_extraction_job(page["jobs"][path])
    assert row is not None
    return row


def _indicator_for(page, path: str) -> dict:
    """The indicator belonging to one source, matched on the total it announces.

    The two sources have different chunk totals, so the total identifies the row
    without the test having to know how the cell is laid out.
    """
    total = float(_job_row(page, path)["total_chunks"])
    matches = [
        node for node in _indicators(page["doc"]) if _reported_counts(node)[1] == total
    ]
    assert len(matches) == 1, (
        f"expected exactly one progress indicator reporting a total of {total:g} chunks "
        f"for {path}, found {len(matches)}"
    )
    return matches[0]


def test_the_fixture_really_has_the_counts_it_claims() -> None:
    """Guard against a vacuous suite: the ratios must be distinct and non-round.

    If two sources shared a fraction, a hardcoded width would satisfy both and every
    proportionality test below would go quiet.
    """
    fractions = {
        100.0 * done / total for total, done, _failed, _running in SOURCES.values()
    }
    assert len(fractions) == len(SOURCES), (
        f"the fixture's completed fractions collide ({fractions}); a single hardcoded "
        "percentage would pass the proportionality tests"
    )


@pytest.mark.parametrize("path", sorted(SOURCES))
def test_every_analysed_source_gets_a_progress_indicator(page, path: str) -> None:
    """Bare text is the bug. Each analysed source must expose a real progress element."""
    node = _indicator_for(page, path)
    assert node["attrs"].get("role") == "progressbar" or node["tag"] == "progress"


@pytest.mark.parametrize("path", sorted(SOURCES))
def test_the_indicator_announces_the_chunks_it_has_processed(page, path: str) -> None:
    """The value channel: `aria-valuenow` is chunks *processed*, complete plus failed.

    That is the meaning #228 settled on, and it is a decision rather than a detail, so
    it is pinned here. The bar measures how far the job has got; a failed chunk is not
    retried by itself, so the job is finished with it. Counting only the successes
    would leave the announced value smaller than the drawn fill for any run with a
    failure -- see the consistency test below, which is the reason this reading wins.
    Success rate is a separate question and lives in `aria-valuetext`.

    Read back from the job row rather than from the fixture's constants, so a bar that
    drifts from the data it claims to show fails here rather than in review.
    """
    job = _job_row(page, path)
    processed = float(int(job["completed_chunks"]) + int(job["failed_chunks"]))
    now, ceiling = _reported_counts(_indicator_for(page, path))

    assert (now, ceiling) == (processed, float(job["total_chunks"])), (
        f"{path}: the indicator announces {now:g}/{ceiling:g} but the job row has "
        f"{job['completed_chunks']} complete + {job['failed_chunks']} failed of "
        f"{job['total_chunks']}"
    )


@pytest.mark.parametrize("path", sorted(SOURCES))
def test_the_announced_value_covers_exactly_as_much_track_as_the_bar_draws(
    page, path: str
) -> None:
    """The two channels must agree, or the bar lies to one audience.

    A sighted reader sees the completed and failed segments tile the track together;
    a screen reader hears `aria-valuenow` out of `aria-valuemax`. If those disagree --
    which they did while the value counted completed chunks only -- the same bar is
    75% full and 25% done at once.

    "What the bar draws" is the sum of the proportional lengths inside the indicator,
    which is what tiling means. A native `<progress>` reports one length equal to its
    own value and satisfies this for free; a single-fill bar does too. What cannot
    satisfy it is a bar that paints a segment it does not count.
    """
    node = _indicator_for(page, path)
    now, ceiling = _reported_counts(node)
    announced = 100.0 * now / ceiling
    drawn = sum(_drawn_fractions(page["doc"], node).values())

    assert abs(drawn - announced) < 0.01, (
        f"{path}: the bar covers {drawn:g}% of its track but announces {now:g}/{ceiling:g} "
        f"= {announced:g}%. Either the value or the fill is wrong about how far along "
        "the job is."
    )


@pytest.mark.parametrize("path", sorted(SOURCES))
def test_the_bar_is_drawn_in_proportion_to_the_completed_chunks(page, path: str) -> None:
    """The visual channel: some length in the bar is `completed/total`, to the percent.

    This is what makes it a *bar* rather than a decoration. The expected value is
    computed from the job row here, and the two sources sit at 37.5% and 25%, so a
    fixed width -- the one-line change that would keep a weaker test green while
    flattening the feature -- cannot satisfy both parametrisations.
    """
    job = _job_row(page, path)
    expected = 100.0 * int(job["completed_chunks"]) / int(job["total_chunks"])
    node = _indicator_for(page, path)
    drawn = _drawn_fractions(page["doc"], node)

    assert drawn, (
        f"{path}: the progress indicator draws no proportional length at all "
        f"(no width/inline-size/flex-basis/custom-property percentage, and it is not a "
        f"native <progress>); it announces a value but shows nothing"
    )
    assert any(abs(value - expected) < 0.01 for value in drawn.values()), (
        f"{path}: expected a segment at {expected:g}% of the track "
        f"({job['completed_chunks']}/{job['total_chunks']} chunks), found {sorted(drawn.values())}"
    )


def test_failed_chunks_are_drawn_as_their_own_segment(page) -> None:
    """A run that failed part-way must not read as a bar that merely stopped short.

    `completed_chunks` and `failed_chunks` are disjoint counts of the same chunk rows,
    so the failures occupy their own share of the track. Asserting the segment is a
    *different element* from the completed one is what stops the two from being folded
    into a single fill -- which would leave the reader unable to tell 1 done + 2 failed
    from 3 done.

    Scoped to this source on purpose: a native `<progress>` cannot express two
    segments, so this test is the one place the suite requires the richer markup. That
    is a product requirement from #228, not an accident of the implementation.
    """
    path = "partly-failed.txt"
    job = _job_row(page, path)
    total = int(job["total_chunks"])
    done_pct = 100.0 * int(job["completed_chunks"]) / total
    failed_pct = 100.0 * int(job["failed_chunks"]) / total
    assert failed_pct and abs(failed_pct - done_pct) > 0.01, "fixture must fail some chunks"

    drawn = _drawn_fractions(page["doc"], _indicator_for(page, path))
    done_elements = {i for i, value in drawn.items() if abs(value - done_pct) < 0.01}
    failed_elements = {i for i, value in drawn.items() if abs(value - failed_pct) < 0.01}

    assert failed_elements, (
        f"{path}: {job['failed_chunks']}/{total} chunks failed but nothing on the bar is "
        f"{failed_pct:g}% wide; found {sorted(drawn.values())}"
    )
    assert failed_elements - done_elements, (
        f"{path}: the failed share and the completed share are drawn by the same element, "
        "so 1 done + 2 failed is indistinguishable from 3 done"
    )


def test_a_job_with_no_chunks_yet_draws_no_bar(tmp_path) -> None:
    """`total_chunks == 0` is reachable: the job row is written before its chunks are.

    Without the guard the template divides by zero and the whole page 500s -- so this
    covers both halves of "must not divide by zero" and "must not draw a bar out of no
    data" against a KB whose *only* source is in that state.
    """
    cfg = Config(
        root=tmp_path, db_path=tmp_path / "kb.sqlite",
        provider="anthropic", model="m", api_key=None, base_url=None,
    )
    app = create_app(cfg)
    client = TestClient(app)
    store = app.state.store
    source_id = store.add_source("just-queued.txt", "text")
    store.create_extraction_job(
        source_id=source_id, provider="anthropic", model="m", total_chunks=0
    )

    response = client.get("/sources")

    assert response.status_code == 200, (
        f"/sources failed for a job with no chunks: {response.text[:400]}"
    )
    assert not _indicators(_parse(response.text)), (
        "a job with no chunks yet renders a progress bar; there is no fraction to draw"
    )


def test_the_bar_markup_is_not_styled_by_a_class_the_stylesheet_never_defines(page) -> None:
    """The one thing the rendered HTML cannot show: whether the bar is visible at all.

    Height, track and fill colour live in app.css, and a bar with no stylesheet behind
    it is two zero-height spans -- every assertion above still green, nothing on screen.
    A test process has no layout engine, so this cannot be measured; what it *can* do is
    catch the regression that would cause it, which is the markup and the stylesheet
    drifting apart (the block deleted, or a class renamed on one side only).

    Be clear about the limit: this proves the class is mentioned in a selector, not that
    the rule draws anything. It is a companion to the rendered-output tests above, never
    a substitute -- on its own, `.progress {}` would satisfy it. Implementations that
    style by attribute or element selector carry no classes here and are simply not
    constrained by it.
    """
    css = CSS_PATH.read_text(encoding="utf-8")
    selectors = " ".join(
        match.group(1) for match in re.finditer(r"([^{}]+)\{[^{}]*\}", css)
    )

    doc = page["doc"]
    classes: set[str] = set()
    for indicator in _indicators(doc):
        for element in [indicator, *doc.descendants(indicator)]:
            classes.update(element["attrs"].get("class", "").split())

    orphans = sorted(
        name for name in classes if not re.search(rf"\.{re.escape(name)}(?![\w-])", selectors)
    )
    assert not orphans, (
        f"the progress markup carries classes no app.css selector mentions: {orphans}. "
        "The bar would render as zero-height spans -- present in the DOM, invisible on screen."
    )


# --- the poll (#228, second half) -------------------------------------------


def _pollers(doc: _Doc) -> list[dict]:
    """Every element that issues a request on a timer.

    A list rather than the one element, so an implementation that gives each row its
    own poller is judged by the same rules instead of failing on a head count.
    """
    polling = doc.find(
        lambda node: "hx-get" in node["attrs"] and "every" in node["attrs"].get("hx-trigger", "")
    )
    assert polling, (
        "nothing on /sources polls while a job is live; every containment assertion "
        "about the swap would be vacuously true"
    )
    return polling


def _inherited(doc: _Doc, node: dict, attribute: str) -> str | None:
    """The attribute's value on `node`, or failing that on its nearest ancestor.

    Every htmx attribute this file reads -- `hx-swap`, `hx-target`, `hx-select`,
    `hx-select-oob` -- is resolved by `getClosestAttributeValue` (`ne()` in the
    vendored 2.0.9), which walks the parent chain. Reading only the poller's own
    attributes leaves a blind spot the width of the table: an out-of-band list hung on
    the <tbody>, or an `hx-swap="outerHTML"` on a wrapper, removes nodes exactly the
    same and would go unmodelled.
    """
    for index in [node["index"], *reversed(node["ancestors"])]:
        value = doc.nodes[index]["attrs"].get(attribute)
        if value is not None:
            return value
    return None


def _swap_region(doc: _Doc, poller: dict, attribute: str) -> dict:
    """Resolve an hx-target/hx-select value to the element it names.

    Accepts the forms htmx offers for naming a narrow region: `this`, `closest <sel>`,
    and an `#id`. A bare tag selector is resolved too, which is how `main` -- the bug --
    still resolves to a node and gets caught by the caller.
    """
    value = " ".join((_inherited(doc, poller, attribute) or "").split())
    if value == "this":
        return poller
    selector = value.split(" ", 1)[1] if value.startswith("closest ") else value
    if selector.startswith("#"):
        wanted = selector[1:]
        matches = doc.find(lambda node: node["attrs"].get("id") == wanted)
        assert len(matches) == 1, (
            f"{attribute}={value!r} names #{wanted}, which matches {len(matches)} elements "
            "in the rendered page; htmx would have nothing (or too much) to swap"
        )
        return matches[0]
    matches = doc.find(lambda node: node["tag"] == selector)
    assert matches, f"{attribute}={value!r} matches no element in the rendered page"
    return matches[0]


def _oob_targets(doc: _Doc, poller: dict) -> list[tuple[dict, str]]:
    """The `(element, swap style)` pairs `hx-select-oob` names, against the live page.

    htmx resolves out-of-band swaps by id and by nothing else (it strips a leading `#`
    and looks the rest up), so the value is a list of ids each with an optional
    `:strategy` that defaults to `true`, htmx's spelling of `outerHTML`. An id that
    names nothing is a silently dead entry in htmx, which is worth failing on here
    rather than shipping.
    """
    targets: list[tuple[dict, str]] = []
    for entry in (_inherited(doc, poller, "hx-select-oob") or "").split(","):
        name, _, style = entry.partition(":")
        wanted = name.strip().lstrip("#")
        if not wanted:
            continue
        matches = doc.find(lambda node, wanted=wanted: node["attrs"].get("id") == wanted)
        assert len(matches) == 1, (
            f"hx-select-oob names #{wanted}, which matches {len(matches)} elements in the "
            "rendered page; htmx would swap nothing (or the wrong thing) for it"
        )
        targets.append((matches[0], style.strip() or "true"))
    return targets


# What each htmx swap style does to the nodes already in the page. `true` is how an
# out-of-band swap spells `outerHTML`. The positional styles insert next to the target
# and remove nothing, which is why they are absent from both sets rather than lumped
# in with `innerHTML`.
STYLES_REMOVING_TARGET = frozenset({"outerHTML", "delete", "true"})
STYLES_REMOVING_CHILDREN = frozenset({"innerHTML", "textContent"})


def _response_oob_swaps(doc: _Doc) -> list[tuple[dict, str]]:
    """The `(element, swap style)` pairs htmx's *other* out-of-band channel produces.

    `hx-select-oob` is a property of the poller; `hx-swap-oob` is a property of the
    **response**, carried on the elements themselves, and the poller has no attribute
    that mentions it. `oobSwap()` (`_e()` in the vendored htmx 2.0.9) scans the whole
    response for `[hx-swap-oob]` and swaps each match over the live node with the same
    id -- and it runs *before* `hx-select` narrows anything, so a narrow `hx-select`
    does not contain it.

    The poll fetches /sources, so the response is this page: the rendered document is
    a faithful stand-in for what comes back. Without this, one attribute on the actions
    cell reinstates the focus bug with every assertion in this file still green.

    Nested elements count, because htmx 2.0.9 ships `allowNestedOobSwaps: true` -- a
    `<td>` inside a `<tr>` is swapped rather than stripped of the attribute.
    """
    swaps: list[tuple[dict, str]] = []
    for node in doc.find(lambda node: "hx-swap-oob" in node["attrs"]):
        # The value is a swap style, optionally `style:#other-target`; an empty value
        # (a bare `hx-swap-oob` attribute) means htmx's default for the channel.
        value = (node["attrs"].get("hx-swap-oob") or "true").strip()
        swaps.append((node, value.split(":", 1)[0].strip() or "true"))
    return swaps


def _removed_by(doc: _Doc, target: dict, style: str) -> list[dict]:
    if style in STYLES_REMOVING_TARGET:
        return doc.subtree(target)
    if style in STYLES_REMOVING_CHILDREN:
        return doc.descendants(target)
    return []


def _removed_nodes(doc: _Doc, poller: dict) -> list[dict]:
    """Every node a tick tears out of the live DOM.

    This is the question the whole poll section turns on, and it is deliberately
    computed from all three of htmx's removal channels at once rather than read off
    one of them: a guard that only inspected `hx-target` would miss content arriving
    out of band, one that only inspected `hx-select-oob` would miss the main swap, and
    one that inspected only the poller's attributes would miss `hx-swap-oob` in the
    response entirely -- see `_response_oob_swaps`. All three remove nodes.

    The distinction between removing the target and removing only its children is kept
    because one of the invariants below turns on it -- an `innerHTML` swap leaves the
    poller in place with its `hx-trigger` intact, so a page that stops it that way
    would poll forever. `none` swaps nothing into the target and leaves only the
    out-of-band lists.
    """
    style = (_inherited(doc, poller, "hx-swap") or "innerHTML").split()[0]
    removed = [
        node
        for target, oob_style in [*_oob_targets(doc, poller), *_response_oob_swaps(doc)]
        for node in _removed_by(doc, target, oob_style)
    ]
    if style != "none":
        has_target = _inherited(doc, poller, "hx-target") is not None
        target = _swap_region(doc, poller, "hx-target") if has_target else poller
        removed += _removed_by(doc, target, style)
    return removed


def _injected_regions(doc: _Doc, poller: dict) -> list[dict]:
    """Every region of the response a tick pastes into the page.

    The mirror of `_removed_nodes`, and the reason `hx-select` is not ignored:
    selecting a wide region into a narrow target does not remove the chrome, it
    duplicates it. With no `hx-select` at all htmx uses the whole response body, which
    is why that case resolves to <body> here instead of to nothing.
    """
    style = (_inherited(doc, poller, "hx-swap") or "innerHTML").split()[0]
    regions = [target for target, _style in _oob_targets(doc, poller)]
    regions += [target for target, _style in _response_oob_swaps(doc)]
    if style != "none":
        if _inherited(doc, poller, "hx-select") is not None:
            regions.append(_swap_region(doc, poller, "hx-select"))
        else:
            regions.extend(doc.find(lambda node: node["tag"] == "body"))
    return regions


def _swept_away(doc: _Doc, page_doc_pollers: list[dict]) -> set[int]:
    """Indices of every node any poller removes, across the whole page."""
    return {
        node["index"]
        for poller in page_doc_pollers
        for node in _removed_nodes(doc, poller)
    }


def _focusable(node: dict) -> bool:
    """Can this element hold keyboard focus?

    The list is the one the HTML spec makes focusable by default, plus the two
    attributes that make anything focusable. It is written as a property of the
    element rather than as a search for the four buttons this page happens to have,
    because the thing that must not be swapped is "a control the user is interacting
    with", and a future row that grows a link or a select is the same bug.
    """
    attrs = node["attrs"]
    tag = node["tag"]
    if "tabindex" in attrs:
        return True
    editable = attrs.get("contenteditable")
    if editable is not None and editable.lower() != "false":
        return True
    if tag == "input":
        return attrs.get("type", "text").lower() != "hidden"
    if tag in {"a", "area"}:
        return "href" in attrs
    return tag in {"button", "select", "textarea", "summary", "iframe", "object", "embed"}


def test_the_page_still_polls_while_a_job_is_live(page) -> None:
    """The counterpart guard: narrowing the swap must not be achieved by not polling.

    Deleting the poll would make every containment assertion below vacuously true.
    """
    for poller in _pollers(page["doc"]):
        assert poller["attrs"].get("hx-get") == "/sources"


def test_the_poll_still_refreshes_the_progress_it_exists_to_show(page) -> None:
    """The other half of that guard: the bars must actually be inside the swap.

    "Swap nothing" satisfies every safety assertion below and ships a progress bar
    that never moves. So every progress indicator on the page has to fall inside some
    region a tick replaces -- which is the narrowest statement of what the poll is
    for, and the thing the safety assertions are trading against.
    """
    doc = page["doc"]
    indicators = _indicators(doc)
    assert indicators, "no progress indicators on the page; this check is vacuous"
    refreshed = _swept_away(doc, _pollers(doc))

    for indicator in indicators:
        assert indicator["index"] in refreshed, (
            "a progress bar sits outside everything the poll swaps, so it will show the "
            "chunk counts from page load until the user reloads by hand"
        )


def test_the_poll_can_turn_itself_off(page) -> None:
    """A tick has to be able to remove the element that owns the timer.

    This page already shipped a bug where a superseded job left it polling every two
    seconds forever (see `is_live_extraction_job` in web/app.py). The template's half
    of that guarantee is structural: the poll stops because the server renders the
    polling element without its hx-* attributes once no job is live, and the swap
    installs that version -- which only works if the swap actually removes the old
    element. An `innerHTML` swap, or a `none` swap with the poller outside the
    out-of-band list, leaves the original `hx-trigger` in the document and the timer
    runs until the tab closes.
    """
    doc = page["doc"]
    for poller in _pollers(doc):
        removed = {node["index"] for node in _removed_nodes(doc, poller)}
        assert poller["index"] in removed, (
            f"the polling <{poller['tag']}> is never removed by its own swap, so its "
            "hx-trigger outlives the jobs and /sources is requested every 2s forever"
        )


def test_the_poll_leaves_the_page_chrome_alone(page) -> None:
    """No part of the swap may take the whole page with it.

    The original bug was `hx-target="main" hx-select="main"`: htmx narrowed the
    *response* and then replaced everything anyway. Both halves are checked, plus the
    out-of-band list, because getting one right and another wrong is worse than the
    bug -- selecting `main` into a narrow target nests the page chrome inside it.

    The assertion is containment, not a string comparison: no swapped region may
    contain the <h1>. That is what "scroll position survives" reduces to, and it holds
    for any narrowing (the table, its tbody, a wrapper) rather than pinning the one
    this change happened to pick.
    """
    doc = page["doc"]
    headings = doc.find(lambda node: node["tag"] == "h1")
    assert headings, "the sources page lost its <h1>; the containment check is vacuous"
    torn_out = _swept_away(doc, _pollers(doc))

    for heading in headings:
        assert heading["index"] not in torn_out, (
            "the poll tears out the page heading, so the 2s tick rebuilds the whole "
            "view and drops the scroll position"
        )
    for poller in _pollers(doc):
        for region in _injected_regions(doc, poller):
            assert region["tag"] not in {"main", "body", "html"}, (
                f"the poll pastes a whole <{region['tag']}> back into the page every tick"
            )
            for heading in headings:
                assert not doc.contains(region, heading), (
                    f"the poll selects a <{region['tag']}> containing the page heading and "
                    "pastes it into the swap target, duplicating the chrome every 2s"
                )


def test_a_tick_only_replaces_controls_htmx_can_hand_the_focus_back_to(page) -> None:
    """The requirement #228 was reopened for, stated the way htmx actually behaves.

    Narrowing the swap from <main> to `#sources-table` did not fix the focus loss, and
    an earlier version of this test drew the wrong conclusion from that -- it asserted
    that a tick may remove *no* focusable node, on the stated grounds that "ids do not
    survive `outerHTML`". That is false for the htmx this repo vendors. In 2.0.9's
    `swap()` (static/htmx.min.js) htmx records `document.activeElement` with its
    `selectionStart`/`selectionEnd` before swapping, and afterwards, if that element
    has left the document (`getRootNode({composed:true}) !== document`) and carried an
    `id`, it looks the id up with `document.getElementById` and focuses the result,
    restoring the selection range. Focus survives replacement -- by identity.

    Believing otherwise cost something real: it ruled out putting the actions cell in
    the swap set, and that exclusion is a bug of its own, because which buttons the row
    offers is derived from the counts the tick delivers (see
    `test_a_tick_cannot_deliver_a_failure_without_the_button_that_answers_it`).

    So the rule is not "replace nothing focusable" but "replace nothing focusable that
    htmx cannot find again": every focusable node inside the swap must carry an id, and
    that id must name exactly one element, since `getElementById` returns one node and
    a duplicate would hand the cursor to the wrong control. Stated over every focusable
    element rather than the four this row happens to have, because the requirement is
    about "a control the user is interacting with" and a future row that grows a link
    or a select is the same bug.

    An implementation that keeps its controls out of the swap entirely still passes,
    and correctly so -- it has nothing to restore. What holds *that* honest is the
    behavioural test named above, not this one.
    """
    doc = page["doc"]
    assert [node for node in doc.nodes if _focusable(node)], (
        "the rendered page has no focusable element at all; this check is vacuous"
    )

    for poller in _pollers(doc):
        replaced = [node for node in _removed_nodes(doc, poller) if _focusable(node)]
        anonymous = [node for node in replaced if not node["attrs"].get("id")]
        assert not anonymous, (
            f"a tick removes {[node['tag'] for node in anonymous]} from the document and "
            "they carry no id, so htmx has nothing to look up afterwards; keyboard focus "
            "on any of them is lost every 2 seconds while an analysis runs"
        )
        for node in replaced:
            name = node["attrs"]["id"]
            twins = doc.find(lambda other, name=name: other["attrs"].get("id") == name)
            assert len(twins) == 1, (
                f"{len(twins)} elements share id={name!r}; after a tick replaces the "
                "focused one, getElementById hands the keyboard cursor to whichever "
                "of them comes first"
            )


def test_the_fixture_renders_every_row_action(page) -> None:
    """Guard against a vacuous poll suite: all four controls must be on the page.

    Retry and Accept all are conditional, so a fixture drifting away from failed
    chunks or unresolved candidates would quietly reduce the test below to checking
    that a swap does not remove buttons that were never rendered.
    """
    doc = page["doc"]
    labels = {doc.text(node) for node in doc.find(lambda node: node["tag"] == "button")}
    missing = [label for label in ROW_ACTIONS if label not in labels]
    assert not missing, (
        f"the fixture renders no {missing} button; found {sorted(labels)}. The poll "
        "tests would be checking a row that has no actions to lose."
    )


@pytest.mark.parametrize("label", ROW_ACTIONS)
def test_the_named_row_actions_keep_their_identity_across_a_tick(page, label: str) -> None:
    """The same requirement, named: these four buttons, by the text on them.

    `test_a_tick_only_replaces_controls_htmx_can_hand_the_focus_back_to` states the
    rule; this states the instance, so a failure report says "Delete loses its focus"
    instead of "a <button> does". Both are kept because the general one can be
    satisfied by a page that has stopped rendering the buttons, and this one cannot.

    Scoped to the buttons a tick actually replaces: a control the swap leaves alone
    needs no id, and requiring one anyway would fail an implementation that keeps its
    actions outside the swap for no reason a user could observe.
    """
    doc = page["doc"]
    buttons = [
        node for node in doc.find(lambda node: node["tag"] == "button")
        if doc.text(node) == label
    ]
    assert buttons, f"no {label!r} button on the page"
    swapped_away = _swept_away(doc, _pollers(doc))

    for button in buttons:
        if button["index"] not in swapped_away:
            continue
        name = button["attrs"].get("id")
        assert name, (
            f"the {label!r} button is inside a region the poll replaces and has no id, so "
            "pressing tab to it and waiting two seconds loses it"
        )
        twins = doc.find(lambda other, name=name: other["attrs"].get("id") == name)
        assert len(twins) == 1, (
            f"the {label!r} button's id={name!r} is shared by {len(twins)} elements, so "
            "a tick can hand the keyboard cursor to a different row's control"
        )


# --- what the tick leaves behind (#228, the regression the exclusion caused) --
#
# Keeping the actions cell out of the swap set looks safe until you notice that the
# row's buttons are *derived from the counts the swap delivers*: Retry is rendered
# only once a chunk has failed, Accept all only once the extraction has produced a
# candidate. So a page opened mid-run watches its analysis cell fill in and its
# actions cell stay frozen at the state it was loaded with -- and the poll turns
# itself off on the very tick that reports the failure, so nothing but a manual
# reload ever fixes it.

MID_RUN_PATH = "mid-run.txt"
MID_RUN_CHUNKS = 4
MID_RUN_COMPLETED = 3


@pytest.fixture()
def mid_run(tmp_path):
    """One source rendered twice: during the extraction, and after it ends badly.

    `before` is what a browser gets for opening /sources while chunks are still being
    processed -- nothing has completed, so the row offers neither Retry (which needs a
    failed chunk) nor Accept all (which needs a candidate). `after` is the same page
    once the run is over: three chunks done, one failed, one candidate extracted, and
    the job closed. That is both the state the last tick has to deliver and the tick on
    which the poll stops, so whatever it fails to carry, the browser never gets.
    """
    root = tmp_path / "mid-run"
    root.mkdir(parents=True, exist_ok=True)
    cfg = Config(
        root=root, db_path=root / "kb.sqlite",
        provider="anthropic", model="m", api_key=None, base_url=None,
    )
    app = create_app(cfg)
    client = TestClient(app)
    store = app.state.store

    source_id = store.add_source(MID_RUN_PATH, "text")
    job_id = store.create_extraction_job(
        source_id=source_id, provider="anthropic", model="m", total_chunks=MID_RUN_CHUNKS
    )
    chunk_ids = store.add_source_chunks(
        job_id=job_id, source_id=source_id,
        chunks=[f"chunk {i}" for i in range(MID_RUN_CHUNKS)],
    )
    store.mark_chunk_running(chunk_ids[0])
    before = client.get("/sources")
    assert before.status_code == 200, before.text

    for chunk_id in chunk_ids[:MID_RUN_COMPLETED]:
        store.mark_chunk_done(chunk_id)
    for chunk_id in chunk_ids[MID_RUN_COMPLETED:]:
        store.mark_chunk_failed(chunk_id, "boom")
    store.add_fact(f"{MID_RUN_PATH} subject", "relates to", "object", source_id=source_id)
    store.finish_extraction_job(job_id)
    after = client.get("/sources")
    assert after.status_code == 200, after.text

    return {"before": _parse(before.text), "after": _parse(after.text)}


def _cell(doc: _Doc, class_name: str) -> dict:
    """The single table cell carrying `class_name`. The fixture renders one row."""
    matches = doc.find(
        lambda node: class_name in node["attrs"].get("class", "").split()
    )
    assert len(matches) == 1, (
        f"expected exactly one .{class_name} on the page, found {len(matches)}"
    )
    return matches[0]


def _labels(doc: _Doc, cell: dict) -> set[str]:
    return {doc.text(node) for node in doc.subtree(cell) if node["tag"] == "button"}


def _after_one_tick(mid_run, class_name: str) -> tuple[_Doc, dict]:
    """The version of a cell a browser holding `before` is looking at once a tick lands.

    The server's current rendering if a tick replaces that node, the one the browser
    loaded otherwise. That is the whole of what the poll does for a page nobody
    reloads, and the only question this section asks.
    """
    doc = mid_run["before"]
    cell = _cell(doc, class_name)
    if cell["index"] in _swept_away(doc, _pollers(doc)):
        fresh = mid_run["after"]
        return fresh, _cell(fresh, class_name)
    return doc, cell


def test_a_tick_cannot_deliver_a_failure_without_the_button_that_answers_it(mid_run) -> None:
    """The row a tick leaves behind has to be a row the server would render.

    This is the regression that came with narrowing the swap: the analysis cell is
    refreshed and the actions cell is not, so the page ends up showing "4/4 chunk(s),
    1 failed" next to a cell with no Retry button -- a state no render of /sources ever
    produces. And it is terminal, because the same tick that reports the failure is the
    one that stops the poll.

    Stated as an outcome rather than as a rule about the swap set: reconstruct what the
    browser is looking at after the tick, and require its buttons to be the ones the
    server renders for the state the rest of the row is now showing. Any implementation
    that keeps the two in step passes -- refreshing the cell, or not making the buttons
    conditional on the counts in the first place -- and only the mismatch is red.
    """
    fresh = mid_run["after"]
    stale_labels = _labels(mid_run["before"], _cell(mid_run["before"], "actions"))
    fresh_labels = _labels(fresh, _cell(fresh, "actions"))

    # Non-vacuity: the run has to actually change which buttons the row offers, or the
    # stale cell and the fresh one are the same cell and nothing below can fail.
    assert fresh_labels - stale_labels, (
        f"the fixture renders the same actions before ({sorted(stale_labels)}) and after "
        f"({sorted(fresh_labels)}) the run; there is no staleness for a tick to expose"
    )

    analysis_doc, _analysis_cell = _after_one_tick(mid_run, "analysis-cell")
    assert analysis_doc is fresh, (
        "a tick does not refresh the analysis cell at all, so the progress bar this "
        "page exists for never moves; that is a different bug, and it is here instead"
    )

    actions_doc, actions_cell = _after_one_tick(mid_run, "actions")
    shown = _labels(actions_doc, actions_cell)

    assert shown == fresh_labels, (
        f"after the tick the page shows the finished analysis -- {MID_RUN_CHUNKS - MID_RUN_COMPLETED} "
        f"of {MID_RUN_CHUNKS} chunk(s) failed -- beside {sorted(shown)}, but the server "
        f"renders {sorted(fresh_labels)} for that state. Missing: "
        f"{sorted(fresh_labels - shown)}. The poll stops on this tick, so the row stays "
        "wrong until the user reloads by hand."
    )


@pytest.mark.parametrize("label", ("Re-analyze", "Delete"))
def test_a_control_a_tick_replaces_answers_to_the_same_id_afterwards(mid_run, label) -> None:
    """Restoring focus by id only works if the id names the same control next render.

    htmx looks the focused element's id up in the *response*, so an id that is stable
    within one render and different in the next -- derived from the row's position, from
    a counter, or from the job id, which a re-analysis replaces -- restores nothing:
    `getElementById` finds no node and the keyboard cursor is gone just as if the id had
    never been there. The two labels here are the unconditional actions, the only ones
    present on both sides of the run to compare.

    Vacuous, and rightly so, for an implementation that does not replace these controls:
    there is then no lookup to get wrong.
    """
    before = mid_run["before"]
    replaced = _swept_away(before, _pollers(before))

    def named(doc: _Doc) -> dict:
        matches = [
            node for node in doc.find(lambda node: node["tag"] == "button")
            if doc.text(node) == label
        ]
        assert len(matches) == 1, f"expected one {label!r} button, found {len(matches)}"
        return matches[0]

    button = named(before)
    if button["index"] not in replaced:
        pytest.skip(f"no tick replaces the {label!r} button; nothing to look up")

    was = button["attrs"].get("id")
    now = named(mid_run["after"])["attrs"].get("id")
    assert was and was == now, (
        f"the {label!r} button is id={was!r} while the analysis runs and id={now!r} "
        "afterwards, so htmx cannot find it again and focus is lost on the tick anyway"
    )
