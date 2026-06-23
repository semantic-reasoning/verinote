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


def test_dashboard_shows_coverage_gap(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.store
    sid = store.add_source("sources/a.txt")  # no file on disk
    store.add_fact("X", "is_a", "Y", status="needs_review", source_id=sid)
    r = c.get("/")
    assert r.status_code == 200
    assert "Coverage" in r.text
    assert "sources/a.txt" in r.text
    assert "gap" in r.text


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


def test_edit_form_renders(tmp_path):
    c = _client(tmp_path)
    r = c.get(f"/facts/{c.fact_id}/edit")
    assert r.status_code == 200
    assert 'name="subject"' in r.text
    assert "/amend" in r.text


def test_amend_endpoint_updates_and_audits(tmp_path):
    c = _client(tmp_path)
    r = c.post(
        f"/facts/{c.fact_id}/amend",
        data={"subject": "NewSubj", "relation": "became", "object": "NewObj", "note": "n"},
    )
    assert r.status_code == 200
    assert "NewSubj" in r.text and "NewObj" in r.text
    store = c.app.state.store
    assert store.get_fact(c.fact_id)["subject"] == "NewSubj"
    assert any(e["action"] == "amended" for e in store.fact_log(c.fact_id))


def test_provenance_shows_source_and_audit(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.store
    sid = store.add_source("sources/x.txt", kind="text")
    fid = store.add_fact("S", "r", "O", status="needs_review", source_id=sid)
    store.toggle_review(fid)  # leaves an audit entry
    r = c.get(f"/facts/{fid}/provenance")
    assert r.status_code == 200
    assert "sources/x.txt" in r.text
    assert "toggled" in r.text


def test_analytics_page_renders(tmp_path):
    from verinote.store.analytics import duckdb_available

    c = _client(tmp_path)
    c.app.state.store.add_fact("A", "is_a", "B", status="confirmed", confidence=0.95)
    r = c.get("/analytics")
    assert r.status_code == 200
    if duckdb_available():
        assert "By status" in r.text and "confirmed" in r.text
    else:
        assert "DuckDB isn't installed" in r.text


def test_add_question_persists(tmp_path):
    c = _client(tmp_path)
    r = c.post("/questions", data={"text": "Where was Ada born?"}, follow_redirects=False)
    assert r.status_code == 303
    assert [q["text"] for q in c.app.state.store.questions()] == ["Where was Ada born?"]


def test_translate_and_report_answers(tmp_path, monkeypatch, fake_client):
    # canned translator: answer_q<id>(O) :- relation("Ada","born_in",O)
    monkeypatch.setattr(
        webapp,
        "get_client",
        lambda cfg: fake_client(
            query=lambda question, qid: f'answer_q{qid}(O) :- relation("Ada", "born_in", O).'
        ),
    )
    c = _client(tmp_path)
    store = c.app.state.store
    store.add_fact("Ada", "born_in", "London", status="confirmed")
    store.add_question("Where was Ada born?")

    r = c.post("/questions/translate", follow_redirects=False)
    assert r.status_code == 303
    assert store.questions()[0]["status"] == "translated"
    # the report and questions page now surface the engine-evaluated answer
    assert "London" in c.get("/report").text
    assert "London" in c.get("/questions").text


def test_translate_surfaces_llm_error(tmp_path, monkeypatch, fake_client):
    monkeypatch.setattr(
        webapp, "get_client", lambda cfg: fake_client(error=LLMError("provider down"))
    )
    c = _client(tmp_path)
    c.app.state.store.add_question("q?")
    r = c.post("/questions/translate")
    assert r.status_code == 502
    assert "translation failed: provider down" in r.text


def test_repair_action_accepts_valid_fix(tmp_path, monkeypatch, fake_client):
    monkeypatch.setattr(
        webapp,
        "get_client",
        lambda cfg: fake_client(
            query=lambda question, qid: f'answer_q{qid}(O) :- relation("Ada", "born_in", O).'
        ),
    )
    c = _client(tmp_path)
    store = c.app.state.store
    qid = store.add_question("Where was Ada born?")
    store.set_question_query(qid, 'review_required("Where was Ada born?")', "review_required")

    r = c.post("/questions/repair", follow_redirects=False)
    assert r.status_code == 303
    assert store.questions()[0]["status"] == "translated"


def test_settings_page_renders(tmp_path):
    c = _client(tmp_path)
    r = c.get("/settings")
    assert r.status_code == 200
    assert "Provider" in r.text and "anthropic" in r.text


def test_settings_save_changes_active_provider(tmp_path, monkeypatch):
    for var in ("VERINOTE_PROVIDER", "VERINOTE_MODEL", "VERINOTE_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    c = _client(tmp_path)
    r = c.post(
        "/settings",
        data={"provider": "ollama", "model": "llama3.1", "base_url": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303
    # the next get_client would pick the ollama adapter — no code change
    assert c.app.state.cfg.provider == "ollama"
    assert (tmp_path / "config.json").is_file()


def test_settings_never_renders_api_key(tmp_path):
    cfg = Config(
        root=tmp_path, db_path=tmp_path / "kb.sqlite",
        provider="anthropic", model="m", api_key="supersecret", base_url=None,
    )
    client = TestClient(create_app(cfg))
    r = client.get("/settings")
    assert "supersecret" not in r.text
    assert "set (from environment)" in r.text


def test_test_connection_reports_adapter(tmp_path, monkeypatch, fake_client):
    monkeypatch.setattr(
        webapp, "get_client", lambda c: fake_client([ExtractedFact("A", "is_a", "B", 0.9)])
    )
    c = _client(tmp_path)
    r = c.post("/settings/test")
    assert r.status_code == 200
    assert "fake answered with 1 fact" in r.text


def test_test_connection_surfaces_llm_error(tmp_path, monkeypatch, fake_client):
    monkeypatch.setattr(webapp, "get_client", lambda c: fake_client(error=LLMError("no key")))
    c = _client(tmp_path)
    r = c.post("/settings/test")
    assert r.status_code == 502
    assert "connection failed: no key" in r.text
