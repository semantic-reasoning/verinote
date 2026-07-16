# SPDX-License-Identifier: MPL-2.0
"""The term/string kind of each triple slot has to reach a screen reader, not
just a sighted user hovering for a tooltip. #278 moved the kind off a
hover-only ``title`` onto an ``aria-label`` that also carries the visible
value, so the accessible name is "<value> (<kind>)" rather than a bare "term"
that would swallow the value. These tests lock that: the kind is in the
aria-label of every slot, and the old kind tooltip is gone."""

from html import unescape

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from verinote.config import Config  # noqa: E402
from verinote.store.fact_input import structural_term  # noqa: E402
from verinote.web import create_app  # noqa: E402


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


def test_review_row_names_the_kind_in_aria_label_not_a_title(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.store
    # A structural triple: every slot is a term.
    store.add_fact(
        structural_term('person("Ada")'),
        structural_term("has_role"),
        structural_term('role(person("Ada"), "PI")'),
        status="candidate",
    )
    # A string-literal subject: same shape, different kind.
    store.add_fact(
        'person("Ada")',
        "has_role",
        'role(person("Ada"), "PI")',
        status="candidate",
    )

    body = unescape(c.get("/review").text)

    # The kind rides on the accessible name together with the visible value,
    # so a screen reader announces the value AND the kind -- not "term" alone.
    assert 'class="subj term-term" aria-label="person("Ada") (term)">person("Ada")' in body
    assert 'class="rel term-term" aria-label="has_role (term)">has_role' in body
    assert 'class="obj term-term" aria-label="role(person("Ada"), "PI") (term)">role(person("Ada"), "PI")' in body
    # The string-literal slot carries its own kind, both as the CSS hook and in
    # the aria-label (checked apart from the escaped value so an escaping change
    # cannot vacuously pass this).
    assert 'class="subj term-string"' in body
    assert '(string)">"person(\\"Ada\\")"' in body

    # The kind must no longer hide in a hover-only tooltip.
    assert 'title="term"' not in body
    assert 'title="string"' not in body


def test_provenance_names_the_kind_in_aria_label_not_a_title(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.store
    sid = store.add_source("sources/x.txt", kind="text")
    fid = store.add_fact(
        structural_term('person("Ada")'),
        structural_term("has_role"),
        structural_term('role(person("Ada"), "PI")'),
        status="needs_review",
        source_id=sid,
    )

    body = unescape(c.get(f"/facts/{fid}/provenance").text)

    assert 'class="subj term-term" aria-label="person("Ada") (term)">person("Ada")' in body
    assert 'class="rel term-term" aria-label="has_role (term)">has_role' in body
    assert 'class="obj term-term" aria-label="role(person("Ada"), "PI") (term)">role(person("Ada"), "PI")' in body

    assert 'title="term"' not in body
    assert 'title="string"' not in body
