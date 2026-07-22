# SPDX-License-Identifier: MPL-2.0
"""Chunk progress on the Sources page: a bar, and a poll that stops eating the page (#228).

Extraction is the only long-running task in the app, and the Sources page reported it
as `3/8 chunk(s)` -- a string you have to read one row at a time. Meanwhile the 2s poll
carried `hx-target="main" hx-select="main"`, so every tick replaced the heading, the
upload form and the table as one unit and took scroll position and focus with it.

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
    """A minimal element tree: every start tag with its attributes and its ancestors.

    Enough to ask "is X inside Y", which is the only structural question here. Using
    a parser rather than a regex matters for the poll test: proving the swapped region
    does not contain the page heading is a containment question, and a regex over the
    source text can only guess at it.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.nodes: list[dict] = []
        self._open: list[int] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        index = len(self.nodes)
        self.nodes.append(
            {"tag": tag, "attrs": dict(attrs), "ancestors": list(self._open), "index": index}
        )
        if tag not in VOID_TAGS:
            self._open.append(index)

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
def test_the_indicator_announces_the_real_chunk_counts(page, path: str) -> None:
    """The value channel: what a screen reader hears must be the job's own numbers.

    Read back from the job row rather than from the fixture's constants, so a bar that
    drifts from the data it claims to show fails here rather than in review.
    """
    job = _job_row(page, path)
    now, ceiling = _reported_counts(_indicator_for(page, path))

    assert (now, ceiling) == (float(job["completed_chunks"]), float(job["total_chunks"])), (
        f"{path}: the indicator announces {now:g}/{ceiling:g} but the job row says "
        f"{job['completed_chunks']}/{job['total_chunks']}"
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


def _poller(doc: _Doc) -> dict:
    polling = doc.find(lambda node: "hx-get" in node["attrs"])
    assert len(polling) == 1, (
        f"expected exactly one polling element on /sources, found {len(polling)}"
    )
    return polling[0]


def _swap_region(doc: _Doc, poller: dict, attribute: str) -> dict:
    """Resolve an hx-target/hx-select value to the element it names.

    Accepts the forms htmx offers for naming a narrow region: `this`, `closest <sel>`,
    and an `#id`. A bare tag selector is resolved too, which is how `main` -- the bug --
    still resolves to a node and gets caught by the caller.
    """
    value = " ".join(poller["attrs"][attribute].split())
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


def test_the_page_still_polls_while_a_job_is_live(page) -> None:
    """The counterpart guard: narrowing the swap must not be achieved by not polling.

    Deleting the poll would make every containment assertion below vacuously true.
    """
    poller = _poller(page["doc"])
    assert poller["attrs"].get("hx-get") == "/sources"
    assert "every" in poller["attrs"].get("hx-trigger", ""), (
        f"the sources poll no longer runs on a timer: {poller['attrs'].get('hx-trigger')!r}"
    )


@pytest.mark.parametrize("attribute", ["hx-target", "hx-select"])
def test_the_poll_leaves_the_page_chrome_alone(page, attribute: str) -> None:
    """Neither half of the swap may take the whole page with it.

    The bug was `hx-target="main" hx-select="main"`: htmx narrowed the *response* and
    then replaced everything anyway. Both attributes are checked, because getting one
    right and the other wrong is worse than the bug -- selecting `main` into a narrow
    target nests the page chrome inside the table.

    The assertion is containment, not a string comparison: the swapped region must not
    contain the <h1>. That is what "scroll position and focus survive" reduces to, and
    it holds for any narrowing (the table, its tbody, a wrapper) rather than pinning
    the one this change happened to pick.
    """
    doc = page["doc"]
    region = _swap_region(doc, _poller(doc), attribute)

    assert region["tag"] not in {"main", "body", "html"}, (
        f"{attribute} still swaps <{region['tag']}>; every tick replaces the page"
    )
    headings = doc.find(lambda node: node["tag"] == "h1")
    assert headings, "the sources page lost its <h1>; the containment check is vacuous"
    for heading in headings:
        assert not doc.contains(region, heading), (
            f"{attribute} resolves to a region containing the page heading, so the 2s "
            "poll rebuilds the whole view and drops scroll position and focus"
        )
