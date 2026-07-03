# SPDX-License-Identifier: MPL-2.0
import builtins
import time
from html import unescape

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

import verinote.web.app as webapp  # noqa: E402
from verinote.config import Config  # noqa: E402
from verinote.engine.terms import Atom, Compound, StringLit  # noqa: E402
from verinote.llm.base import ExtractedFact, LLMError  # noqa: E402
from verinote.pipeline.query import query_path  # noqa: E402
from verinote.store import Store  # noqa: E402
from verinote.store.fact_input import structural_term  # noqa: E402
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


def test_dashboard_renders(tmp_path):
    c = _client(tmp_path)
    r = c.get("/")
    assert r.status_code == 200
    assert "verinote" in r.text


def test_dashboard_shows_factlog_borrowed_source_signals(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.store
    a = store.add_source("sources/a.md")
    b = store.add_source("sources/b.md")
    csrc = store.add_source("sources/c.md")
    store.add_fact("Acme", "uses", "FastAPI", status="confirmed", source_id=a)
    store.add_fact("Acme", "uses", "FastAPI", status="accepted", source_id=b)
    store.add_fact("Acme", "uses", "FastAPI", status="candidate", source_id=csrc)
    store.add_fact("Org", "established_on", "2020", status="confirmed", source_id=a)
    store.add_fact("Org", "established_on", "2021", status="confirmed", source_id=b)

    body = unescape(c.get("/").text)

    assert "Source corroboration" in body
    assert "Acme" in body
    assert "FastAPI" in body
    assert ">2</td>" in body
    assert "Single-valued conflicts" in body
    assert "Org" in body
    assert "2020" in body
    assert "2021" in body
    assert "(1 source)" in body


def test_no_active_kb_shows_selector(tmp_path, monkeypatch):
    monkeypatch.delenv("VERINOTE_ROOT", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.chdir(tmp_path)

    c = TestClient(create_app())
    r = c.get("/")

    assert r.status_code == 200
    assert "Select a knowledge base" in r.text


def test_select_kb_activates_app(tmp_path, monkeypatch):
    monkeypatch.delenv("VERINOTE_ROOT", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.chdir(tmp_path)
    kb = tmp_path / "chosen"

    c = TestClient(create_app())
    r = c.post("/kb/select", data={"root": str(kb)}, follow_redirects=False)

    assert r.status_code == 303
    assert (kb / "kb.sqlite").is_file()
    assert (kb / "policy" / "logic-policy.dl").is_file()
    assert c.app.state.cfg.root == kb.resolve()
    assert "Knowledge base" in c.get("/").text


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


def test_review_renders_structural_terms_from_duckdb_and_distinguishes_strings(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.store
    store.add_fact(
        'person("Ada")',
        "has_role",
        'role(person("Ada"), "PI")',
        status="candidate",
    )
    store.add_fact(
        structural_term('person("Ada")'),
        structural_term("has_role"),
        structural_term('role(person("Ada"), "PI")'),
        status="candidate",
    )

    body = unescape(c.get("/review").text)

    assert 'class="subj term-string" title="string">"person(\\"Ada\\")"' in body
    assert 'class="subj term-term" title="term">person("Ada")' in body
    assert 'class="obj term-term" title="term">role(person("Ada"), "PI")' in body


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
    assert r.headers["location"] == "/sources"
    # the file is saved immediately; extraction finishes in a background job.
    assert (tmp_path / "sources" / "note.txt").read_text() == "some text"

    def extracted():
        assert "is_a" in c.get("/review").text
        body = c.get("/sources").text
        assert "Analysis complete: 1/1 chunk(s)" in body
        assert "1 candidate(s)" in body

    _wait_for(extracted)


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


def test_delete_source_removes_file_and_extracted_facts(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.store
    source_path = tmp_path / "sources" / "a.txt"
    source_path.parent.mkdir()
    source_path.write_text("source body", encoding="utf-8")
    sid = store.add_source("sources/a.txt", kind="text")
    source_fact = store.add_fact("A", "is_a", "B", status="candidate", source_id=sid)
    unrelated_fact = c.fact_id

    r = c.post(f"/sources/{sid}/delete", follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "/sources"
    assert not source_path.exists()
    assert store.sources() == []
    assert store.get_fact(source_fact) is None
    assert store.get_fact_terms(source_fact) is None
    assert store.get_fact(unrelated_fact) is not None
    assert store.source_extraction_jobs() == []


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

    def extracted():
        assert "is_a" in c.get("/review").text

    _wait_for(extracted)


def test_upload_surfaces_llm_error(tmp_path, monkeypatch, fake_client):
    monkeypatch.setattr(
        webapp, "get_client", lambda cfg: fake_client(error=LLMError("provider down"))
    )
    c = _client(tmp_path)
    r = c.post(
        "/sources",
        files={"file": ("note.txt", b"x", "text/plain")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/sources"

    def failed():
        body = c.get("/sources").text
        assert "Analysis failed: 1 chunk(s) failed" in body
        assert "provider down" in body

    _wait_for(failed)


def test_retry_failed_source_chunks(tmp_path, monkeypatch, fake_client):
    state = {"error": LLMError("provider down")}

    def client_factory(cfg):
        if state["error"] is not None:
            return fake_client(error=state["error"])
        return fake_client([ExtractedFact("X", "is_a", "Y", 0.9)])

    monkeypatch.setattr(webapp, "get_client", client_factory)
    c = _client(tmp_path)
    upload = c.post(
        "/sources",
        files={"file": ("note.txt", b"some text", "text/plain")},
        follow_redirects=False,
    )
    assert upload.status_code == 303

    def failed():
        assert "provider down" in c.get("/sources").text

    _wait_for(failed)
    job_id = c.app.state.store.source_extraction_jobs()[0]["id"]
    state["error"] = None

    retry = c.post(f"/sources/jobs/{job_id}/retry", follow_redirects=False)

    assert retry.status_code == 303

    def retried():
        assert "is_a" in c.get("/review").text
        assert "Analysis complete: 1/1 chunk(s)" in c.get("/sources").text

    _wait_for(retried)


def test_create_app_resumes_pending_source_jobs(tmp_path, monkeypatch, fake_client):
    cfg = Config(
        root=tmp_path,
        db_path=tmp_path / "kb.sqlite",
        provider="anthropic",
        model="m",
        api_key=None,
        base_url=None,
    )
    with Store(cfg.db_path) as store:
        store.init_schema()
        sid = store.add_source("sources/a.txt")
        job_id = store.create_extraction_job(
            source_id=sid, provider="anthropic", model="m", total_chunks=1
        )
        store.add_source_chunks(job_id=job_id, source_id=sid, chunks=["some text"])
    monkeypatch.setattr(
        webapp,
        "get_client",
        lambda cfg: fake_client([ExtractedFact("X", "is_a", "Y", 0.9)]),
    )

    c = TestClient(create_app(cfg))

    def resumed():
        assert "is_a" in c.get("/review").text
        assert "Analysis complete: 1/1 chunk(s)" in c.get("/sources").text

    _wait_for(resumed)


def test_report_ok_for_consistent_kb(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.store
    store.add_fact("Org", "established_on", "2020", status="confirmed")
    r = c.get("/report")
    assert r.status_code == 200
    assert "errors: 0" in r.text
    assert "backend: DuckDB" in r.text


def test_report_gates_on_contradiction(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.store
    store.add_fact("Org", "established_on", "2020", status="confirmed")
    store.add_fact("Org", "established_on", "2021", status="confirmed")
    r = c.get("/report")
    assert r.status_code == 200
    assert "ERRORS" in r.text
    assert "functional_conflict" in r.text


def test_report_shows_missing_duckdb_message(tmp_path, monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "duckdb":
            raise ImportError("missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    cfg = Config(
        root=tmp_path,
        db_path=tmp_path / "kb.sqlite",
        provider="anthropic",
        model="m",
        api_key=None,
        base_url=None,
    )
    c = TestClient(create_app(cfg))
    r = c.get("/report")
    assert r.status_code == 200
    assert "DuckDB verification backend is not available" in r.text
    assert "DuckDB is not installed" in r.text


def test_report_surfaces_invalid_query_file(tmp_path):
    c = _client(tmp_path)
    path = query_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(".decl answer_q1(value: symbol)\nanswer_q1(O) :- bogus(O).\n", encoding="utf-8")

    r = c.get("/report")
    assert r.status_code == 200
    assert "ERRORS" in r.text
    assert "bogus" in r.text


def test_edit_form_renders(tmp_path):
    c = _client(tmp_path)
    r = c.get(f"/facts/{c.fact_id}/edit")
    assert r.status_code == 200
    assert 'name="subject"' in r.text
    assert 'name="subject_kind"' in r.text
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


def test_edit_form_preserves_structural_fact_input_kinds(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.store
    fid = store.add_fact(
        structural_term('person("Ada")'),
        structural_term("born_in"),
        "London",
        status="needs_review",
    )

    r = c.get(f"/facts/{fid}/edit")

    assert r.status_code == 200
    assert 'name="subject_kind"' in r.text
    assert '<option value="term" selected>term</option>' in r.text
    assert 'name="object_kind"' in r.text
    assert '<option value="string" selected>string</option>' in r.text


def test_edit_form_uses_duckdb_term_values_not_stale_sqlite_mirrors(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.store
    fid = store.add_fact(
        structural_term('person("Ada")'),
        structural_term("has_role"),
        structural_term('role(person("Ada"), "PI")'),
        status="needs_review",
    )
    store._conn.execute(
        "UPDATE facts SET subject = ?, relation = ?, object = ? WHERE id = ?",
        ("stale_subject", "stale_relation", "stale_object", fid),
    )

    body = unescape(c.get(f"/facts/{fid}/edit").text)

    assert 'value="person("Ada")"' in body
    assert 'value="has_role"' in body
    assert 'value="role(person("Ada"), "PI")"' in body
    assert "stale_subject" not in body


def test_edit_form_uses_raw_values_for_stringlit_inputs(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.store
    fid = store.add_fact(
        'person("Ada")',
        "has_role",
        'role(person("Ada"), "PI")',
        status="needs_review",
    )

    body = unescape(c.get(f"/facts/{fid}/edit").text)

    assert 'value="person("Ada")"' in body
    assert 'value="role(person("Ada"), "PI")"' in body
    assert 'value=""person(' not in body


def test_edit_save_preserves_duckdb_terms_when_sqlite_mirror_is_stale(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.store
    fid = store.add_fact(
        structural_term('person("Ada")'),
        structural_term("has_role"),
        structural_term('role(person("Ada"), "PI")'),
        status="needs_review",
    )
    store._conn.execute(
        "UPDATE facts SET subject = ?, relation = ?, object = ? WHERE id = ?",
        ("stale_subject", "stale_relation", "stale_object", fid),
    )

    r = c.post(
        f"/facts/{fid}/amend",
        data={
            "subject": 'person("Ada")',
            "subject_kind": "term",
            "relation": "has_role",
            "relation_kind": "term",
            "object": 'role(person("Ada"), "PI")',
            "object_kind": "term",
            "note": "",
        },
    )

    assert r.status_code == 200
    assert store.get_fact_terms(fid) == (
        Compound("person", (StringLit("Ada"),)),
        Atom("has_role"),
        Compound("role", (Compound("person", (StringLit("Ada"),)), StringLit("PI"))),
    )


def test_edit_save_preserves_unchanged_stringlit_without_adding_display_quotes(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.store
    fid = store.add_fact(
        'person("Ada")',
        "has_role",
        'role(person("Ada"), "PI")',
        status="needs_review",
    )

    r = c.post(
        f"/facts/{fid}/amend",
        data={
            "subject": 'person("Ada")',
            "subject_kind": "string",
            "relation": "has_role",
            "relation_kind": "string",
            "object": 'role(person("Ada"), "PI")',
            "object_kind": "string",
            "note": "",
        },
    )

    assert r.status_code == 200
    assert store.get_fact_terms(fid) == (
        StringLit('person("Ada")'),
        StringLit("has_role"),
        StringLit('role(person("Ada"), "PI")'),
    )


def test_amend_endpoint_can_save_explicit_structural_terms(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.store
    fid = store.add_fact("A", "r", "B", status="needs_review")

    r = c.post(
        f"/facts/{fid}/amend",
        data={
            "subject": 'person("Ada")',
            "subject_kind": "term",
            "relation": "born_in",
            "relation_kind": "term",
            "object": "London",
            "object_kind": "string",
            "note": "",
        },
    )

    assert r.status_code == 200
    assert store.get_fact_terms(fid) == (
        Compound("person", (StringLit("Ada"),)),
        Atom("born_in"),
        StringLit("London"),
    )


def test_amend_endpoint_saves_term_looking_text_as_stringlit_in_string_mode(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.store
    fid = store.add_fact("A", "r", "B", status="needs_review")

    r = c.post(
        f"/facts/{fid}/amend",
        data={
            "subject": 'person("Ada")',
            "subject_kind": "string",
            "relation": "has_role",
            "relation_kind": "string",
            "object": 'role(person("Ada"), "PI")',
            "object_kind": "string",
            "note": "",
        },
    )

    assert r.status_code == 200
    assert store.get_fact_terms(fid) == (
        StringLit('person("Ada")'),
        StringLit("has_role"),
        StringLit('role(person("Ada"), "PI")'),
    )
    assert 'class="subj term-string" title="string">"person(\\"Ada\\")"' in unescape(r.text)


def test_amend_endpoint_rejects_invalid_structural_terms_without_writing(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.store
    fid = store.add_fact(
        structural_term('person("Ada")'),
        structural_term("born_in"),
        "London",
        status="needs_review",
    )
    before_row = dict(store.get_fact(fid))
    before_terms = store.get_fact_terms(fid)

    r = c.post(
        f"/facts/{fid}/amend",
        data={
            "subject": 'person("Ada"',
            "subject_kind": "term",
            "relation": "born_in",
            "relation_kind": "term",
            "object": "London",
            "object_kind": "string",
            "note": "bad",
        },
    )

    assert r.status_code == 400
    assert "expected" in r.text
    assert dict(store.get_fact(fid)) == before_row
    assert store.get_fact_terms(fid) == before_terms
    assert store.fact_log(fid) == []


def test_amend_endpoint_rejects_nonground_structural_terms_without_writing(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.store
    fid = store.add_fact("A", "r", "B", status="needs_review")
    before_row = dict(store.get_fact(fid))
    before_terms = store.get_fact_terms(fid)

    r = c.post(
        f"/facts/{fid}/amend",
        data={
            "subject": "person(X)",
            "subject_kind": "term",
            "relation": "r",
            "relation_kind": "string",
            "object": "B",
            "object_kind": "string",
            "note": "bad",
        },
    )

    assert r.status_code == 400
    assert "ground" in r.text
    assert dict(store.get_fact(fid)) == before_row
    assert store.get_fact_terms(fid) == before_terms
    assert store.fact_log(fid) == []


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


def test_provenance_renders_structural_terms_from_duckdb(tmp_path):
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

    assert 'class="subj term-term" title="term">person("Ada")' in body
    assert 'class="rel term-term" title="term">has_role' in body
    assert 'class="obj term-term" title="term">role(person("Ada"), "PI")' in body
    assert "sources/x.txt" in body


def test_report_renders_compound_fact_input_and_answer(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.store
    store.add_fact(
        structural_term('person("Ada")'),
        structural_term("has_role"),
        structural_term('role(person("Ada"), "PI")'),
        status="confirmed",
    )
    path = query_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        ".decl answer_q1(value: symbol)\n"
        'answer_q1(O) :- relation(person("Ada"), has_role, O).\n',
        encoding="utf-8",
    )

    body = unescape(c.get("/report").text)

    assert 'q1: role(person("Ada"), "PI")' in body
    assert 'relation(person("Ada"), has_role, role(person("Ada"), "PI"))' in body


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


def test_delete_question_removes_query_file_entry(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.store
    qid = store.add_question("Where was Ada born?")
    store.set_question_query(
        qid,
        '.decl answer_q1(value: symbol)\nanswer_q1(O) :- relation("Ada", "born_in", O).',
        "translated",
    )
    query_file = query_path(tmp_path)
    query_file.parent.mkdir(parents=True, exist_ok=True)
    query_file.write_text(store.questions()[0]["query_dl"] + "\n", encoding="utf-8")

    r = c.post(f"/questions/{qid}/delete", follow_redirects=False)

    assert r.status_code == 303
    assert store.questions() == []
    assert query_file.read_text(encoding="utf-8") == ""
    assert "No questions yet" in c.get("/questions").text


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
    assert "Provider" in r.text and "Anthropic" in r.text
    assert "ClaudeCLI" in r.text
    assert str(tmp_path) in r.text


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


def test_settings_disables_connection_test_for_non_api_provider(tmp_path):
    cfg = Config(
        root=tmp_path,
        db_path=tmp_path / "kb.sqlite",
        provider="claudecli",
        model="",
        api_key=None,
        base_url=None,
    )
    client = TestClient(create_app(cfg))

    r = client.get("/settings")

    assert "ClaudeCLI" in r.text
    assert "Test connection" in r.text
    assert 'aria-disabled="true"' in r.text
    assert "Connection test is not available for this provider." not in r.text


def test_test_connection_rejects_non_api_provider(tmp_path):
    cfg = Config(
        root=tmp_path,
        db_path=tmp_path / "kb.sqlite",
        provider="claudecli",
        model="",
        api_key=None,
        base_url=None,
    )
    client = TestClient(create_app(cfg))

    r = client.post("/settings/test")

    assert r.status_code == 400
    assert "Connection test is not available for this provider." in r.text


def test_settings_enables_connection_test_for_ollama(tmp_path):
    cfg = Config(
        root=tmp_path,
        db_path=tmp_path / "kb.sqlite",
        provider="ollama",
        model="llama3.1",
        api_key=None,
        base_url=None,
    )
    client = TestClient(create_app(cfg))

    r = client.get("/settings")

    assert "Ollama" in r.text
    assert "Test connection" in r.text
    assert 'aria-disabled="true"' not in r.text


def test_settings_switches_active_kb_root(tmp_path):
    c = _client(tmp_path)
    other = tmp_path / "other-kb"

    r = c.post("/settings/root", data={"root": str(other)}, follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert c.app.state.cfg.root == other.resolve()
    assert c.app.state.store.db_path == other.resolve() / "kb.sqlite"
    assert (other / "kb.sqlite").is_file()
    assert "Review queue is empty" in c.get("/review").text
    assert str(other.resolve()) in c.get("/").text


def test_settings_rejects_empty_kb_root(tmp_path):
    c = _client(tmp_path)

    r = c.post("/settings/root", data={"root": "   "})

    assert r.status_code == 400
    assert "KB directory is required" in r.text


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
