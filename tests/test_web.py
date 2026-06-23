# SPDX-License-Identifier: MPL-2.0
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

import verinote.web.app as webapp  # noqa: E402
from verinote.config import Config  # noqa: E402
from verinote.llm.base import ExtractedFact, LLMError  # noqa: E402
from verinote.web import create_app  # noqa: E402


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


def test_dashboard_renders(tmp_path):
    c = _client(tmp_path)
    r = c.get("/")
    assert r.status_code == 200
    assert "verinote" in r.text


def test_review_shows_queue(tmp_path):
    c = _client(tmp_path)
    r = c.get("/review")
    assert r.status_code == 200
    assert "is_a" in r.text


def test_toggle_endpoint_swaps_row(tmp_path):
    c = _client(tmp_path)
    r = c.post(f"/facts/{c.fact_id}/toggle")
    assert r.status_code == 200
    assert "confirmed" in r.text
    # the only queued fact was promoted, so the review queue is now empty
    assert "Review queue is empty" in c.get("/review").text


def test_upload_extracts_and_redirects(tmp_path, monkeypatch, fake_client):
    monkeypatch.setattr(
        webapp, "get_client", lambda cfg: fake_client([ExtractedFact("X", "is_a", "Y", 0.9)])
    )
    c = _client(tmp_path)
    r = c.post(
        "/sources",
        files={"file": ("note.txt", b"some text", "text/plain")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/review"
    # the file was saved under the KB's sources/ dir and the candidate is queued
    assert (tmp_path / "sources" / "note.txt").read_text() == "some text"
    assert "is_a" in c.get("/review").text


def test_upload_rejects_unsupported_type(tmp_path):
    c = _client(tmp_path)
    r = c.post("/sources", files={"file": ("blob.bin", b"\x00\x01", "application/octet-stream")})
    assert r.status_code == 400
    assert "unsupported source type" in r.text


def test_sources_page_lists_sources(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.store
    sid = store.add_source("sources/a.txt", kind="text")
    store.add_fact("A", "is_a", "B", status="candidate", source_id=sid)
    r = c.get("/sources")
    assert r.status_code == 200
    assert "sources/a.txt" in r.text
    assert "text" in r.text


def test_upload_docx_converts_and_extracts(tmp_path, monkeypatch, fake_client):
    import io

    import docx

    monkeypatch.setattr(
        webapp, "get_client", lambda cfg: fake_client([ExtractedFact("X", "is_a", "Y", 0.9)])
    )
    d = docx.Document()
    d.add_paragraph("converted text")
    buf = io.BytesIO()
    d.save(buf)

    c = _client(tmp_path)
    r = c.post(
        "/sources",
        files={
            "file": (
                "report.docx",
                buf.getvalue(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    # the binary was converted to a text file and registered as a conversion
    assert (tmp_path / "sources" / "report.txt").read_text().strip() == "converted text"
    kinds = {s["kind"] for s in c.app.state.store.sources_with_counts()}
    assert "conversion" in kinds


def test_upload_surfaces_llm_error(tmp_path, monkeypatch, fake_client):
    monkeypatch.setattr(
        webapp, "get_client", lambda cfg: fake_client(error=LLMError("provider down"))
    )
    c = _client(tmp_path)
    r = c.post("/sources", files={"file": ("note.txt", b"x", "text/plain")})
    # surfaced as a page, not a 500
    assert r.status_code == 502
    assert "extraction failed: provider down" in r.text


def test_report_ok_for_consistent_kb(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.store
    store.add_fact("Org", "established_on", "2020", status="confirmed")
    r = c.get("/report")
    assert r.status_code == 200
    assert "errors: 0" in r.text


def test_report_gates_on_contradiction(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.store
    store.add_fact("Org", "established_on", "2020", status="confirmed")
    store.add_fact("Org", "established_on", "2021", status="confirmed")
    r = c.get("/report")
    assert r.status_code == 200
    assert "ERRORS" in r.text
    assert "functional_conflict" in r.text
