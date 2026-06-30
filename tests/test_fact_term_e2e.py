# SPDX-License-Identifier: MPL-2.0
from html import unescape
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

import verinote.web.app as webapp  # noqa: E402
from verinote.config import Config  # noqa: E402
from verinote.llm.base import ExtractedFact  # noqa: E402
from verinote.pipeline.query import query_path  # noqa: E402
from verinote.store.fact_input import structural_term  # noqa: E402
from verinote.web import create_app  # noqa: E402


def _wait_for(assertion, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            assertion()
            return
        except AssertionError as e:
            last_error = e
            time.sleep(0.01)
    if last_error is not None:
        raise last_error


def test_plain_extraction_and_structural_fact_report_end_to_end(
    tmp_path, monkeypatch, fake_client
):
    cfg = Config(
        root=tmp_path,
        db_path=tmp_path / "kb.sqlite",
        provider="anthropic",
        model="m",
        api_key=None,
        base_url=None,
    )
    monkeypatch.setattr(
        webapp,
        "get_client",
        lambda cfg: fake_client(
            [ExtractedFact('person("Ada")', "has_role", 'role(person("Ada"), "PI")', 0.9)]
        ),
    )
    client = TestClient(create_app(cfg))

    upload = client.post(
        "/sources",
        files={"file": ("note.txt", b"Ada has a role.", "text/plain")},
        follow_redirects=False,
    )
    assert upload.status_code == 303
    store = client.app.state.store

    extracted = None

    def extracted_fact_exists():
        nonlocal extracted
        queue = store.review_queue()
        assert queue
        extracted = queue[0]

    _wait_for(extracted_fact_exists)
    assert extracted is not None

    review_body = unescape(client.get("/review").text)
    assert 'class="subj term-string" title="string">"person(\\"Ada\\")"' in review_body
    assert client.post(f"/facts/{extracted['id']}/accept").status_code == 200

    structural = store.add_fact(
        structural_term('person("Ada")'),
        structural_term("has_role"),
        structural_term('role(person("Ada"), "PI")'),
        status="needs_review",
    )
    structural_review = unescape(client.get("/review").text)
    assert 'class="subj term-term" title="term">person("Ada")' in structural_review
    assert client.post(f"/facts/{structural}/accept").status_code == 200
    path = query_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        ".decl answer_q1(value: symbol)\n"
        'answer_q1(O) :- relation(person("Ada"), has_role, O).\n',
        encoding="utf-8",
    )

    report = unescape(client.get("/report").text)
    assert 'q1: role(person("Ada"), "PI")' in report
    assert 'relation("person(\\"Ada\\")", "has_role", "role(person(\\"Ada\\"), \\"PI\\")")' in report
    assert 'relation(person("Ada"), has_role, role(person("Ada"), "PI"))' in report
