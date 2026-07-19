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


def test_page_outside_the_nav_marks_nothing(tmp_path) -> None:
    """A fact's provenance page extends base.html but has no nav entry of its own."""
    client = _client(tmp_path)
    response = client.get(f"/facts/{client.fact_id}/provenance")
    assert response.status_code == 200
    assert _current(response.text) == []


def test_stylesheet_styles_the_current_link() -> None:
    """Without a rule, the attribute is invisible to anyone not using a screen reader."""
    css = CSS_PATH.read_text(encoding="utf-8")
    assert re.search(r'nav\s+a\[aria-current\s*=\s*"page"\]', css), (
        "app.css has no rule selecting the current nav link"
    )
