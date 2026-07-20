# SPDX-License-Identifier: MPL-2.0
"""Regression lock on the nav's active-page marker (#225).

The nav used to render ten identical links, so nothing told you which page you
were on. The fix marks the current one with `aria-current="page"`.

The links are read back out of the rendered nav rather than hardcoded here, so
a link added to base.html is covered without touching this file.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from verinote.config import Config  # noqa: E402
from verinote.web import create_app  # noqa: E402

CSS_PATH = Path(__file__).resolve().parents[1] / "verinote" / "web" / "static" / "app.css"

NAV_BLOCK = re.compile(r"<nav\b[^>]*>(.*?)</nav>", re.S)
NAV_ANCHOR = re.compile(r"<a\b([^>]*)>", re.S)
HREF = re.compile(r'href="([^"]*)"')
# Any spelling of the attribute counts as a claim of "this is the current page".
ARIA_CURRENT = re.compile(r'aria-current\s*=\s*"page"')


CURRENT_SELECTOR = r'nav\s+a\[aria-current\s*=\s*"page"\]'
BASE_SELECTOR = r"nav\s+a"
# Properties that cannot mark the link on their own: colour ones fail #226, and
# spacing only makes room for a marker (padding under a `border-bottom: 0` draws
# nothing at all).
UNMARKING_PROPERTIES = frozenset({
    "color", "background", "background-color", "border-color",
    "padding", "padding-top", "padding-bottom", "padding-left", "padding-right",
    "margin", "margin-top", "margin-bottom", "margin-left", "margin-right",
})
LENGTH = re.compile(r"(?<![\w.])(\d*\.?\d+)(?:px|rem|em|%)?(?![\w.])")
# A border draws nothing without a style: the used width of `none`/`hidden` is 0,
# and a bare `border-bottom-width` never gets one.
BORDER_STYLE = re.compile(
    r"(?<![\w-])(?:solid|dashed|dotted|double|groove|ridge|inset|outset)(?![\w-])"
)


def _rule_body(selector: str) -> str | None:
    """The declarations of the rule whose selector list is exactly `selector`."""
    css = CSS_PATH.read_text(encoding="utf-8")
    match = re.search(rf"(?:^|\}}|\n)\s*{selector}\s*\{{([^}}]*)\}}", css)
    return match.group(1) if match else None


def _declarations(body: str) -> dict[str, str]:
    decls = {}
    for chunk in body.split(";"):
        prop, sep, value = chunk.partition(":")
        if sep:
            decls[prop.strip().lower()] = " ".join(value.split())
    return decls


def _has_positive_length(value: str) -> bool:
    return any(float(n) > 0 for n in LENGTH.findall(value))


def _is_visible(prop: str, value: str) -> bool:
    """Whether the declaration actually renders differently from the nav default."""
    if prop.endswith("-color"):
        # Recolouring a channel is not marking it: `text-decoration-color` under
        # the base `text-decoration: none` tints a line that is never drawn.
        return False
    if prop == "font-weight":
        return value == "bold" or (value.isdigit() and int(value) > 400)
    if prop == "box-shadow":
        # The one box channel with no style keyword to require.
        return _has_positive_length(value)
    if prop.startswith(("border", "outline")):
        # `border-bottom: 0`, `2px none var(--accent)` and a bare
        # `border-bottom-width: 2px` all draw nothing.
        return _has_positive_length(value) and bool(BORDER_STYLE.search(value))
    if prop.startswith("text-decoration"):
        return value not in {"none", "initial"}
    return value not in {"normal", "none", "initial", "inherit", "unset", "auto"}


def _client(tmp_path) -> TestClient:
    cfg = Config(
        root=tmp_path, db_path=tmp_path / "kb.sqlite",
        provider="anthropic", model="m", api_key=None, base_url=None,
    )
    app = create_app(cfg)
    client = TestClient(app)
    store = app.state.store
    client.fact_id = store.add_fact("A", "is_a", "B", status="needs_review", confidence=0.9)
    return client


def _nav_links(html: str) -> list[tuple[str, bool]]:
    """Every nav anchor as (href, is_current), in document order."""
    block = NAV_BLOCK.search(html)
    assert block, "the page renders no <nav> at all"
    links = []
    for attrs in NAV_ANCHOR.findall(block.group(1)):
        href = HREF.search(attrs)
        assert href, f"nav anchor without an href: {attrs!r}"
        links.append((href.group(1), bool(ARIA_CURRENT.search(attrs))))
    assert links, "the <nav> renders no links"
    return links


def _current(html: str) -> list[str]:
    return [href for href, is_current in _nav_links(html) if is_current]


def test_each_nav_destination_marks_its_own_link(tmp_path) -> None:
    client = _client(tmp_path)
    hrefs = [href for href, _ in _nav_links(client.get("/").text)]

    for href in hrefs:
        response = client.get(href)
        assert response.status_code == 200, f"GET {href} -> {response.status_code}"
        assert _current(response.text) == [href], (
            f"GET {href} should mark exactly that link as current, "
            f"got {_current(response.text)}"
        )


def test_dashboard_is_not_current_on_other_pages(tmp_path) -> None:
    """`/` is a prefix of every path, so a prefix match would light it up everywhere."""
    client = _client(tmp_path)
    assert "/" not in _current(client.get("/sources").text)


def test_subpath_keeps_its_section_current(tmp_path) -> None:
    """`/sources/…` is still the Sources section, so the parent link stays current.

    A missing source makes the handler re-render the sources page in place rather
    than redirect, which is what leaves the request on the sub-path.
    """
    client = _client(tmp_path)
    response = client.post("/sources/999/reanalyze")
    assert response.status_code == 404
    assert response.url.path == "/sources/999/reanalyze", "the handler redirected away"
    assert _current(response.text) == ["/sources"]


def test_page_outside_the_nav_marks_nothing(tmp_path) -> None:
    """A fact's provenance page extends base.html but has no nav entry of its own."""
    client = _client(tmp_path)
    response = client.get(f"/facts/{client.fact_id}/provenance")
    assert response.status_code == 200
    assert _current(response.text) == []


def test_stylesheet_styles_the_current_link() -> None:
    """Without a rule, the attribute is invisible to anyone not using a screen reader."""
    assert _rule_body(CURRENT_SELECTOR) is not None, (
        "app.css has no rule selecting the current nav link"
    )


def test_current_link_is_marked_off_a_non_colour_channel() -> None:
    """#226 has verdicts leaning on colour alone; the nav must not repeat it.

    A rule that merely *exists* is no guard -- declarations restating the `nav a`
    defaults (`font-weight: 400; border-bottom: 0`) render pixel-identically. So
    this checks the rule moves some non-colour channel off its baseline value.
    """
    body = _rule_body(CURRENT_SELECTOR)
    assert body is not None, "app.css has no rule selecting the current nav link"
    baseline = _declarations(_rule_body(BASE_SELECTOR) or "")

    channels = [
        prop
        for prop, value in _declarations(body).items()
        if prop not in UNMARKING_PROPERTIES
        and baseline.get(prop) != value
        and _is_visible(prop, value)
    ]
    assert channels, (
        "the current nav link is distinguished by colour alone: "
        f"{_declarations(body)} leaves every non-colour channel at its `nav a` value"
    )
