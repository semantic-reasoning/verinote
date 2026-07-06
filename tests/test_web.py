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
from verinote.pipeline.query_intent import parse_query_intent  # noqa: E402
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


def _target(kind: str, value: str | None) -> dict | None:
    return None if value is None else {"kind": kind, "value": value}


def _intent(kind: str, *, subject: str | None = None) -> dict:
    return {
        "kind": kind,
        "subject": _target("entity", subject),
        "relation": None,
        "object": None,
        "relation_candidates": [],
        "operator": None,
        "value_type": None,
        "value": None,
        "reason": None,
    }


class IntentOnlyClient:
    name = "intent-only"

    def __init__(self, intent):
        self.intent = intent
        self.intent_calls = 0
        self.direct_datalog_calls = 0

    def extract_query_intent(self, *, question: str, schema_hint: str = ""):
        self.intent_calls += 1
        raw = self.intent(question) if callable(self.intent) else self.intent
        return parse_query_intent(raw)

    def translate_query(self, *, question: str, qid: int, schema_hint: str = "") -> str:
        self.direct_datalog_calls += 1
        raise AssertionError("supported planner path must not call direct Datalog")


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


def test_dashboard_shows_action_queue_counts_and_links(tmp_path):
    cfg = Config(
        root=tmp_path,
        db_path=tmp_path / "kb.sqlite",
        provider="anthropic",
        model="m",
        api_key=None,
        base_url=None,
    )
    c = TestClient(create_app(cfg))
    store = c.app.state.store
    store.add_fact("Unsupported Sample", "uses", "Sample Service", status="candidate")
    review_source = store.add_source("sources/review.txt")
    support_a = store.add_source("sources/support-a.txt")
    support_b = store.add_source("sources/support-b.txt")
    store.add_fact(
        "Reviewed Sample",
        "uses",
        "Sample Service",
        status="candidate",
        source_id=review_source,
    )
    store.add_fact(
        "Reviewed Sample",
        "uses",
        "Sample Service",
        status="confirmed",
        source_id=support_a,
    )
    store.add_fact(
        "Reviewed Sample",
        "uses",
        "Sample Service",
        status="accepted",
        source_id=support_b,
    )
    conflict_a = store.add_source("sources/conflict-a.txt")
    conflict_b = store.add_source("sources/conflict-b.txt")
    store.add_fact("Org", "established_on", "2020", status="confirmed", source_id=conflict_a)
    store.add_fact("Org", "established_on", "2021", status="accepted", source_id=conflict_b)
    failed_source = store.add_source("sources/failed.txt")
    job_id = store.create_extraction_job(
        source_id=failed_source,
        provider="fake",
        model="sample-model",
        total_chunks=1,
    )
    chunk_id = store.add_source_chunks(
        job_id=job_id,
        source_id=failed_source,
        chunks=["Sample body"],
    )[0]
    store.mark_extraction_job_running(job_id)
    store.mark_chunk_running(chunk_id)
    store.mark_chunk_failed(chunk_id, "provider down")
    changed = store.add_fact("Changed Sample", "uses", "Service", status="confirmed")
    store.amend_fact(changed, subject="Changed Sample", relation="uses", obj="Service v2")

    body = unescape(c.get("/").text)

    assert "Action queues" in body
    assert "Unsupported review items" in body
    assert "Corroborated review targets" in body
    assert "Single-valued conflicts" in body
    assert "Failed source analyses" in body
    assert "Recent lifecycle changes" in body
    assert "Source-backed engine facts" in body
    assert 'href="/review?filter=unsupported"' in body
    assert 'href="/review?filter=corroborated"' in body
    assert 'href="/workbench"' in body
    assert 'href="/sources"' in body
    assert ">1</td>" in body
    assert ">3</td>" in body


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
    assert "Inspect" in r.text
    assert "Conf." not in r.text


def test_review_paginates_large_queue_and_preserves_controls(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.store
    for idx in range(1200):
        store.add_fact(f"Bulk {idx:03d}", "uses", "Sample", status="candidate")

    body = unescape(c.get("/review").text)

    assert body.count('<tr id="fact-') == 50
    assert "Showing 1-50 of 1201 review facts" in body
    assert "Bulk 1199" in body
    assert "Bulk 1149" not in body
    assert 'href="/review?filter=needs-human-decision&sort=newest&page_size=50&page=2"' in body

    page_two = unescape(c.get("/review?page=2").text)
    assert page_two.count('<tr id="fact-') == 50
    assert "Showing 51-100 of 1201 review facts" in page_two
    assert "Bulk 1149" in page_two
    assert "Bulk 1099" not in page_two

    controls = unescape(c.get("/review?sort=oldest&page_size=25&page=2").text)
    assert 'href="/review?filter=unsupported&sort=oldest&page_size=25&page=1"' in controls
    assert '<option value="oldest" selected>Oldest</option>' in controls
    assert '<option value="25" selected>25</option>' in controls


def test_review_clamps_invalid_pagination_params(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.store
    for idx in range(60):
        store.add_fact(f"Clamp {idx:03d}", "uses", "Sample", status="candidate")

    body = unescape(c.get("/review?page=bad&page_size=1000&sort=subject").text)

    assert body.count('<tr id="fact-') == 50
    assert "Showing 1-50 of 61 review facts" in body
    assert '<option value="newest" selected>Newest</option>' in body
    assert '<option value="50" selected>50</option>' in body


def test_review_trust_filter_applies_before_pagination(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.store
    for idx in range(60):
        source_id = store.add_source(f"sources/support-{idx:03d}.txt")
        store.add_fact(f"Supported {idx:03d}", "uses", "Sample", status="candidate")
        store.add_fact(
            f"Supported {idx:03d}",
            "uses",
            "Sample",
            status="confirmed",
            source_id=source_id,
        )

    unfiltered = unescape(c.get("/review").text)
    assert "is_a" not in unfiltered

    body = unescape(c.get("/review?filter=unsupported").text)

    assert body.count('<tr id="fact-') == 1
    assert "Showing 1-1 of 1 review facts" in body
    assert "is_a" in body
    assert "Supported 059" not in body
    assert "Trust filter is applied to this page" not in body


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


def test_review_rows_show_trust_signals_evidence_and_inspect_link(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.store
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "logic-policy.dl").write_text(
        '.decl functional(rel: symbol)\nfunctional("published_year").\n',
        encoding="utf-8",
    )
    (policy / "relation-aliases.md").write_text(
        "- `publication_year` -> `published_year`\n",
        encoding="utf-8",
    )
    (policy / "typed-relations.md").write_text(
        "- published_year : number as year_number\n",
        encoding="utf-8",
    )
    source_id = store.add_source("sources/sample-candidate.txt")
    artifact_id = store.add_source_artifact(
        source_id=source_id,
        kind="original_text",
        path="sources/sample-candidate.txt",
    )
    job_id = store.create_extraction_job(
        source_id=source_id,
        artifact_id=artifact_id,
        provider="fake",
        model="sample-model",
        total_chunks=1,
    )
    chunk_id = store.add_source_chunks(
        job_id=job_id,
        source_id=source_id,
        chunks=["Sample Report was published in 2024."],
    )[0]
    fact_id = store.add_fact(
        "Sample Report",
        "publication_year",
        "2024",
        status="candidate",
        source_id=source_id,
        job_id=job_id,
    )
    store.add_fact_evidence(
        fact_id=fact_id,
        source_id=source_id,
        artifact_id=artifact_id,
        job_id=job_id,
        chunk_id=chunk_id,
        snippet="Sample Report was published in 2024.",
    )
    support_a = store.add_source("sources/sample-support-a.txt")
    support_b = store.add_source("sources/sample-support-b.txt")
    conflict = store.add_source("sources/sample-conflict.txt")
    store.add_fact(
        "Sample Report",
        "published_year",
        "2024",
        status="accepted",
        source_id=support_a,
    )
    store.add_fact(
        "Sample Report",
        "published_year",
        "2024",
        status="confirmed",
        source_id=support_b,
    )
    store.add_fact(
        "Sample Report",
        "published_year",
        "2025",
        status="accepted",
        source_id=conflict,
    )

    body = unescape(c.get("/review").text)

    assert "source backed" in body
    assert "corroborated" in body
    assert "conflicted" in body
    assert "canonical" in body
    assert "published_year" in body
    assert "year_number" in body
    assert "Sample Report was published in 2024." in body
    assert f'href="/facts/{fact_id}/provenance"' in body
    assert "Inspect" in body


def test_review_filters_by_deterministic_trust_signals(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.store
    unsupported = store.add_fact(
        "Unsupported Sample",
        "uses",
        "Sample Service",
        status="candidate",
    )
    source_a = store.add_source("sources/sample-a.txt")
    source_b = store.add_source("sources/sample-b.txt")
    reviewed = store.add_source("sources/sample-reviewed.txt")
    corroborated = store.add_fact(
        "Reviewed Sample",
        "uses",
        "Sample Service",
        status="candidate",
        source_id=reviewed,
    )
    store.add_fact(
        "Reviewed Sample",
        "uses",
        "Sample Service",
        status="accepted",
        source_id=source_a,
    )
    store.add_fact(
        "Reviewed Sample",
        "uses",
        "Sample Service",
        status="confirmed",
        source_id=source_b,
    )

    unsupported_body = unescape(c.get("/review?filter=unsupported").text)
    assert "Unsupported Sample" in unsupported_body
    assert "Reviewed Sample" not in unsupported_body
    assert f'href="/facts/{unsupported}/provenance"' in unsupported_body

    corroborated_body = unescape(c.get("/review?filter=corroborated").text)
    assert "Reviewed Sample" in corroborated_body
    assert "Unsupported Sample" not in corroborated_body
    assert f'href="/facts/{corroborated}/provenance"' in corroborated_body


def test_workbench_renders_corroboration_conflicts_and_normalization(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.store
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "logic-policy.dl").write_text(
        '.decl functional(rel: symbol)\n'
        'functional("published_year").\n'
        'functional("revenue").\n',
        encoding="utf-8",
    )
    (policy / "relation-aliases.md").write_text(
        "- `pub_year` -> `published_year`\n",
        encoding="utf-8",
    )
    (policy / "typed-relations.md").write_text(
        "- revenue : amount as revenue_scalar\n",
        encoding="utf-8",
    )
    source_a = store.add_source("sources/a.txt")
    source_b = store.add_source("sources/b.txt")
    source_c = store.add_source("sources/c.txt")
    candidate_source = store.add_source("sources/candidate.txt")
    fact_id = store.add_fact(
        "Sample Report",
        "pub_year",
        "2024",
        status="confirmed",
        source_id=source_a,
    )
    store.add_fact(
        "Sample Report",
        "published_year",
        "2024",
        status="accepted",
        source_id=source_b,
    )
    candidate_id = store.add_fact(
        "Sample Report",
        "published_year",
        "2024",
        status="candidate",
        source_id=candidate_source,
    )
    store.add_fact(
        "Sample Company",
        "revenue",
        'amount(5000,"억")',
        status="confirmed",
        source_id=source_a,
    )
    store.add_fact(
        "Sample Company",
        "revenue",
        'amount(0.54,"조")',
        status="accepted",
        source_id=source_b,
    )
    store.add_fact(
        "Sample Company",
        "revenue",
        'amount(5400,"억")',
        status="confirmed",
        source_id=source_c,
    )

    body = unescape(c.get("/workbench").text)

    assert "Trust workbench" in body
    assert "Corroborated facts" in body
    assert "Sample Report" in body
    assert "pub_year -> published_year" in body
    assert "sources/a.txt" in body
    assert "sources/b.txt" in body
    assert "Related candidates" in body
    assert f'href="/facts/{candidate_id}/provenance"' in body
    assert "Single-valued conflicts" in body
    assert "Sample Company" in body
    assert 'amount(5000,"억")' in body
    assert 'amount(0.54,"조")' in body
    assert "revenue_scalar=540000000000" in body
    assert f'href="/facts/{fact_id}/provenance"' in body


def test_review_shows_accept_recommendation(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.store
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "logic-policy.dl").write_text(
        '.decl functional(rel: symbol)\nfunctional("published_year").\n',
        encoding="utf-8",
    )
    source_a = store.add_source("sources/a.txt")
    source_b = store.add_source("sources/b.txt")
    job_a = store.create_extraction_job(
        source_id=source_a,
        provider="fake",
        model="sample-model",
        total_chunks=1,
    )
    job_b = store.create_extraction_job(
        source_id=source_b,
        provider="fake",
        model="sample-model",
        total_chunks=1,
    )
    chunk_a = store.add_source_chunks(job_id=job_a, source_id=source_a, chunks=["a"])[0]
    chunk_b = store.add_source_chunks(job_id=job_b, source_id=source_b, chunks=["b"])[0]
    store.mark_extraction_job_running(job_a)
    store.mark_chunk_running(chunk_a)
    store.mark_chunk_done(chunk_a, candidates=1)
    store.finish_extraction_job(job_a)
    store.mark_extraction_job_running(job_b)
    store.mark_chunk_running(chunk_b)
    store.mark_chunk_done(chunk_b, candidates=1)
    store.finish_extraction_job(job_b)
    store.add_fact(
        "Sample Report",
        "published_year",
        "2024",
        status="candidate",
        source_id=source_a,
        job_id=job_a,
    )
    store.add_fact(
        "Sample Report",
        "published_year",
        "2024",
        status="confirmed",
        source_id=source_b,
        job_id=job_b,
    )

    body = unescape(c.get("/review").text)

    assert "accept recommended" in body


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
    long_artifact_path = (
        "artifacts/sources/1/"
        "very-long-extracted-text-artifact-name-that-should-not-stretch-the-sources-table.txt"
    )
    store.add_source_artifact(
        source_id=sid,
        kind="extracted_text",
        path=long_artifact_path,
    )
    store.add_fact("A", "is_a", "B", status="candidate", source_id=sid)
    job_id = store.create_extraction_job(
        source_id=sid,
        provider="ollama",
        model="qwen3.5:9b",
        total_chunks=2,
    )
    chunks = store.add_source_chunks(job_id=job_id, source_id=sid, chunks=["a", "b"])
    store.mark_extraction_job_running(job_id)
    store.mark_chunk_running(chunks[0])
    store.mark_chunk_done(chunks[0], candidates=1)
    store.mark_chunk_running(chunks[1])
    store.mark_chunk_failed(chunks[1], "provider down")

    r = c.get("/sources")

    assert r.status_code == 200
    assert "sources/a.txt" in r.text
    assert 'title="sources/a.txt"' in r.text
    assert "text" in r.text
    assert "failed" in r.text
    assert "1/2 chunk(s)" in r.text
    assert "provider down" in r.text
    assert "1 candidate(s)" in r.text
    assert "1 unsupported" in r.text
    assert "ollama" in r.text
    assert "qwen3.5:9b" in r.text
    assert "Retry" in r.text
    assert "extracted_text" in r.text
    assert f'title="{long_artifact_path}"' in r.text
    assert 'class="truncate"' in r.text


def test_sources_page_shows_trust_counts_and_evidence_snippets(tmp_path, monkeypatch):
    c = _client(tmp_path)
    store = c.app.state.store
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "logic-policy.dl").write_text(
        '.decl functional(rel: symbol)\nfunctional("published_year").\n',
        encoding="utf-8",
    )
    source_id = store.add_source("sources/sample-source.txt", kind="text")
    artifact_id = store.add_source_artifact(
        source_id=source_id,
        kind="original_text",
        path="sources/sample-source.txt",
    )
    job_id = store.create_extraction_job(
        source_id=source_id,
        artifact_id=artifact_id,
        provider="fake",
        model="sample-model",
        total_chunks=1,
    )
    chunk_id = store.add_source_chunks(
        job_id=job_id,
        source_id=source_id,
        chunks=["Sample Report was published in 2024."],
    )[0]
    store.mark_extraction_job_running(job_id)
    store.mark_chunk_running(chunk_id)
    store.mark_chunk_done(chunk_id, candidates=1)
    fact_id = store.add_fact(
        "Sample Report",
        "published_year",
        "2024",
        status="candidate",
        source_id=source_id,
        job_id=job_id,
    )
    store.add_fact_evidence(
        fact_id=fact_id,
        source_id=source_id,
        artifact_id=artifact_id,
        job_id=job_id,
        chunk_id=chunk_id,
        snippet="Sample Report was published in 2024.",
    )
    support_a = store.add_source("sources/sample-support-a.txt")
    support_b = store.add_source("sources/sample-support-b.txt")
    conflict = store.add_source("sources/sample-conflict.txt")
    store.add_fact(
        "Sample Report",
        "published_year",
        "2024",
        status="accepted",
        source_id=support_a,
    )
    store.add_fact(
        "Sample Report",
        "published_year",
        "2024",
        status="confirmed",
        source_id=support_b,
    )
    store.add_fact(
        "Sample Report",
        "published_year",
        "2025",
        status="accepted",
        source_id=conflict,
    )
    monkeypatch.setattr(
        webapp,
        "fact_trust_summary",
        lambda *args, **kwargs: pytest.fail("sources page should use source rollups"),
    )

    body = unescape(c.get("/sources").text)

    assert "sources/sample-source.txt" in body
    assert "original_text" in body
    assert "sample-model" in body
    assert "chunk size" in body
    assert "max facts" in body
    assert "0 unsupported" in body
    assert "1 conflicted" in body
    assert "1 corroborated" in body
    assert "Sample Report was published in 2024." in body


def test_delete_source_removes_file_and_extracted_facts(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.store
    source_path = tmp_path / "sources" / "a.txt"
    source_path.parent.mkdir()
    source_path.write_text("source body", encoding="utf-8")
    sid = store.add_source("sources/a.txt", kind="text")
    artifact_path = tmp_path / "artifacts" / "sources" / str(sid) / "extracted.txt"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text("artifact body", encoding="utf-8")
    store.add_source_artifact(
        source_id=sid,
        kind="extracted_text",
        path=f"artifacts/sources/{sid}/extracted.txt",
    )
    source_fact = store.add_fact("A", "is_a", "B", status="candidate", source_id=sid)
    unrelated_fact = c.fact_id

    r = c.post(f"/sources/{sid}/delete", follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "/sources"
    assert not source_path.exists()
    assert not artifact_path.exists()
    assert store.sources() == []
    assert store.get_fact(source_fact) is None
    assert store.get_fact_terms(source_fact) is None
    assert store.get_fact(unrelated_fact) is not None
    assert store.source_extraction_jobs() == []


def test_reanalyze_source_reuses_artifact_and_replaces_extracted_facts(
    tmp_path, monkeypatch, fake_client
):
    monkeypatch.setattr(
        webapp,
        "get_client",
        lambda cfg: fake_client([ExtractedFact("New", "is_a", "Fact", 0.9)]),
    )
    c = _client(tmp_path)
    store = c.app.state.store
    sid = store.add_source("sources/a.txt", kind="text")
    artifact_path = tmp_path / "sources" / "a.txt"
    artifact_path.parent.mkdir()
    artifact_path.write_text("source body", encoding="utf-8")
    artifact_id = store.add_source_artifact(
        source_id=sid,
        kind="original_text",
        path="sources/a.txt",
    )
    store.add_fact("Old", "is_a", "Fact", status="candidate", source_id=sid)
    old_job = store.create_extraction_job(
        source_id=sid,
        artifact_id=artifact_id,
        provider="fake",
        model="old",
        total_chunks=1,
    )
    old_chunk = store.add_source_chunks(
        job_id=old_job, source_id=sid, chunks=["old body"]
    )[0]
    store.mark_extraction_job_running(old_job)
    store.mark_chunk_running(old_chunk)
    store.mark_chunk_done(old_chunk, candidates=1)

    body = c.get("/sources").text
    assert "Re-analyze" in body

    r = c.post(f"/sources/{sid}/reanalyze", follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "/sources"

    def reanalyzed():
        assert any(row["subject"] == "New" for row in store.review_queue())
        assert "Analysis complete: 1/1 chunk(s)" in c.get("/sources").text

    _wait_for(reanalyzed)
    assert not any(row["subject"] == "Old" for row in store.review_queue())
    assert len(store.source_extraction_jobs()) == 1
    assert store.get_source(sid)["path"] == "sources/a.txt"
    assert artifact_path.read_text(encoding="utf-8") == "source body"
    fact = [row for row in store.review_queue() if row["subject"] == "New"][0]
    assert fact["source_id"] == sid
    assert store.get_extraction_job_detail(fact["job_id"])["artifact_id"] == artifact_id


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
    # the original file is preserved and converted text is tracked as an artifact.
    assert (tmp_path / "sources" / "report.docx").is_file()
    artifact_files = list((tmp_path / "artifacts" / "sources" / "1").glob("*.txt"))
    assert len(artifact_files) == 1
    assert artifact_files[0].read_text().strip() == "converted text"
    kinds = {s["kind"] for s in c.app.state.store.sources_with_counts()}
    assert "binary" in kinds
    sources_body = c.get("/sources").text
    assert "sources/report.docx" in sources_body
    assert "artifacts/sources/1/" in sources_body
    assert "extracted_text" in sources_body

    def extracted():
        assert any(row["subject"] == "X" for row in c.app.state.store.review_queue())
        assert "complete" in c.get("/sources").text

    _wait_for(extracted)
    fact = [row for row in c.app.state.store.review_queue() if row["subject"] == "X"][0]
    provenance = c.get(f"/facts/{fact['id']}/provenance").text
    assert "sources/report.docx" in provenance
    assert "artifacts/sources/1/" in provenance


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


def test_report_shows_query_trace_links_and_candidate_exclusion(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.store
    source_id = store.add_source("sources/sample.txt")
    fact_id = store.add_fact(
        "Sample Person",
        "born_in",
        "Sample City",
        status="confirmed",
        source_id=source_id,
    )
    store.add_fact_evidence(
        fact_id=fact_id,
        source_id=source_id,
        snippet="Sample Person was born in Sample City.",
    )
    store.add_fact(
        "Candidate Person",
        "born_in",
        "Draft City",
        status="candidate",
        source_id=source_id,
    )
    path = query_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '.decl answer_q1(value: symbol)\n'
        'answer_q1(O) :- relation("Sample Person", "born_in", O).\n',
        encoding="utf-8",
    )

    body = unescape(c.get("/report").text)

    assert "Traceability" in body
    assert "candidate/needs-review fact(s) were excluded from engine input" in body
    assert f'href="/facts/{fact_id}/provenance"' in body
    assert "Sample Person was born in Sample City." in body
    assert "Draft City" not in body


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


def test_report_surfaces_invalid_relation_alias_policy(tmp_path):
    c = _client(tmp_path)
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "relation-aliases.md").write_text("- `role` -> `role`\n", encoding="utf-8")
    path = query_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '.decl answer_q1(value: symbol)\n'
        'answer_q1(O) :- relation("Sample Person", "role", O).\n',
        encoding="utf-8",
    )

    r = c.get("/report")

    assert r.status_code == 200
    assert "ERRORS" in r.text
    assert "policy error" in r.text
    assert "self-map" in r.text


def test_questions_surfaces_invalid_relation_alias_policy(tmp_path):
    c = _client(tmp_path)
    c.app.state.store.add_question("What is the sample role?")
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "relation-aliases.md").write_text("- `role` -> `role`\n", encoding="utf-8")
    path = query_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '.decl answer_q1(value: symbol)\n'
        'answer_q1(O) :- relation("Sample Person", "role", O).\n',
        encoding="utf-8",
    )

    r = c.get("/questions")

    assert r.status_code == 200
    assert "policy error" in r.text
    assert "self-map" in r.text


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
    assert "candidate_created" in r.text
    assert "system" in r.text
    assert "toggled" in r.text
    assert "human" in r.text


def test_provenance_renders_trust_dossier_sections(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.store
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "logic-policy.dl").write_text(
        '.decl functional(rel: symbol)\nfunctional("published_year").\n',
        encoding="utf-8",
    )
    source_id = store.add_source("sources/sample-source.txt")
    artifact_id = store.add_source_artifact(
        source_id=source_id,
        kind="original_text",
        path="sources/sample-source.txt",
    )
    job_id = store.create_extraction_job(
        source_id=source_id,
        artifact_id=artifact_id,
        provider="fake",
        model="sample-model",
        total_chunks=1,
    )
    chunk_id = store.add_source_chunks(
        job_id=job_id,
        source_id=source_id,
        chunks=["Sample Report was published in 2024."],
    )[0]
    store.mark_extraction_job_running(job_id)
    store.mark_chunk_running(chunk_id)
    store.mark_chunk_done(chunk_id, candidates=1)
    run_id = store.add_run(provider="fake", model="sample-model", summary="sample run")
    fact_id = store.add_fact(
        "Sample Report",
        "published_year",
        "2024",
        status="confirmed",
        confidence=0.99,
        source_id=source_id,
        run_id=run_id,
        job_id=job_id,
        note="model note",
    )
    store.add_fact_evidence(
        fact_id=fact_id,
        source_id=source_id,
        artifact_id=artifact_id,
        job_id=job_id,
        chunk_id=chunk_id,
        snippet="Sample Report was published in 2024.",
    )
    store.set_status(fact_id, "accepted", action="accepted")
    other_source = store.add_source("sources/sample-conflict.txt")
    store.add_fact(
        "Sample Report",
        "published_year",
        "2025",
        status="accepted",
        source_id=other_source,
    )

    body = unescape(c.get(f"/facts/{fact_id}/provenance").text)

    assert "Trust dossier" in body
    assert "Trust summary" in body
    assert "source backed" in body
    assert "single source" in body
    assert "conflicted" in body
    assert "source support" in body
    assert "sources/sample-source.txt" in body
    assert "Conflict summary" in body
    assert "2024" in body
    assert "2025" in body
    assert "Lifecycle timeline" in body
    assert "candidate_created" in body
    assert "accepted" in body
    assert f"job #{job_id}" in body
    assert "Source evidence" in body
    assert "Sample Report was published in 2024." in body
    assert "Metadata" in body
    assert "model metadata only" in body
    assert body.index("Trust summary") < body.index("Metadata")


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


def test_ask_page_renders_and_is_linked(tmp_path):
    c = _client(tmp_path)

    home = c.get("/")
    page = c.get("/ask")

    assert home.status_code == 200
    assert 'href="/ask"' in home.text
    assert page.status_code == 200
    assert "<h1>Ask</h1>" in page.text


def test_ask_post_renders_verified_engine_answer_without_persisting(
    tmp_path, monkeypatch
):
    class DeterministicOnly:
        name = "deterministic-only"

        def extract_query_intent(self, *, question: str, schema_hint: str = ""):
            raise AssertionError("deterministic question must bypass LLM")

        def translate_query(self, *, question: str, qid: int, schema_hint: str = ""):
            raise AssertionError("Ask must not call direct Datalog fallback")

        def answer_question(self, *, question: str, context: str):
            raise AssertionError("verified engine answer must not call fallback")

    monkeypatch.setattr(webapp, "get_client", lambda cfg: DeterministicOnly())
    c = _client(tmp_path)
    store = c.app.state.store
    store.add_fact("샘플인물", "역할", "검토자", status="confirmed")

    r = c.post("/ask", data={"question": "샘플인물의 역할은 무엇인가?"})

    assert r.status_code == 200
    assert "VERIFIED — engine" in r.text
    assert "검토자" in r.text
    assert store.questions() == []
    assert not query_path(tmp_path).exists()


def test_ask_post_renders_unverified_llm_fallback(tmp_path, monkeypatch):
    class Fallback:
        name = "fallback"

        def extract_query_intent(self, *, question: str, schema_hint: str = ""):
            return parse_query_intent(
                {
                    "kind": "unknown_or_unsupported",
                    "subject": None,
                    "relation": None,
                    "object": None,
                    "relation_candidates": None,
                    "operator": None,
                    "value_type": None,
                    "value": None,
                    "reason": "unsupported synthetic question",
                }
            )

        def translate_query(self, *, question: str, qid: int, schema_hint: str = ""):
            raise AssertionError("Ask fallback must not call direct Datalog")

        def answer_question(self, *, question: str, context: str):
            assert "sources/sample.txt" in context
            return "Synthetic answer from excerpts."

    monkeypatch.setattr(webapp, "get_client", lambda cfg: Fallback())
    c = _client(tmp_path)
    source = tmp_path / "sources" / "sample.txt"
    source.parent.mkdir()
    source.write_text("Sample Entity provides Sample Service.", encoding="utf-8")
    c.app.state.store.add_source("sources/sample.txt")

    r = c.post("/ask", data={"question": "Sample Entity overview"})

    assert r.status_code == 200
    assert "UNVERIFIED — source exploration" in r.text
    assert "Synthetic answer from excerpts." in r.text
    assert "sources/sample.txt" in r.text


def test_delete_question_removes_query_file_entry(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.store
    store.add_fact("Ada", "born_in", "London", status="confirmed")
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


def test_translate_and_report_answers(tmp_path, monkeypatch, fake_client, intent_payload):
    monkeypatch.setattr(
        webapp,
        "get_client",
        lambda cfg: fake_client(
            intent=intent_payload(
                "lookup_object", subject="Sample Person", relation="born_in"
            )
        ),
    )
    c = _client(tmp_path)
    store = c.app.state.store
    store.add_fact("Sample Person", "born_in", "Sample Place", status="confirmed")
    store.add_question("Where was Sample Person born?")

    r = c.post("/questions/translate", follow_redirects=False)
    assert r.status_code == 303
    assert store.questions()[0]["status"] == "translated"
    # the report and questions page now surface the engine-evaluated answer
    assert "Sample Place" in c.get("/report").text
    assert "Sample Place" in c.get("/questions").text


def test_questions_translate_relation_discovery_shows_actual_lifecycle_states(
    tmp_path, monkeypatch
):
    from verinote.pipeline.query_candidate_eval import QueryCandidateSetEvaluation
    from verinote.pipeline.query_candidate_eval import QueryCandidateSetOutcome

    def intent_for(question: str):
        if question.startswith("Translated"):
            return _intent(
                "discover_entity_relations", subject="Synthetic Web Entity"
            )
        if question.startswith("Review"):
            return _intent(
                "discover_entity_relations", subject="Synthetic Web Review Entity"
            )
        if question.startswith("Ambiguous"):
            return _intent(
                "discover_entity_relations", subject="Synthetic Web Ambiguous Entity"
            )
        return _intent("discover_entity_relations", subject="Synthetic Web Missing Entity")

    client = IntentOnlyClient(intent_for)
    monkeypatch.setattr(webapp, "get_client", lambda cfg: client)
    from verinote.pipeline.query import evaluate_query_candidate_plan as real_eval

    def no_answer_for_empty_plan(store, plan):
        if plan.reason == "no relation discovery candidates matched the schema":
            return QueryCandidateSetEvaluation(
                plan=plan,
                outcome=QueryCandidateSetOutcome.NO_ANSWER,
            )
        return real_eval(store, plan)

    monkeypatch.setattr(
        "verinote.pipeline.query.evaluate_query_candidate_plan",
        no_answer_for_empty_plan,
    )
    c = _client(tmp_path)
    store = c.app.state.store
    store.add_fact(
        "Synthetic Web Entity",
        "synthetic_web_relation",
        "Synthetic Web Value",
        status="confirmed",
    )
    store.add_fact(
        "Synthetic Web Review Entity",
        "source",
        "Synthetic Review Value",
        status="confirmed",
    )
    store.add_fact(
        "Synthetic Web Ambiguous Entity",
        "subject_relation",
        "Synthetic Subject Value",
        status="confirmed",
    )
    store.add_fact(
        "Synthetic Web Source",
        "object_relation",
        "Synthetic Web Ambiguous Entity",
        status="confirmed",
    )
    store.add_question("Translated relation discovery?")
    store.add_question("Review relation discovery?")
    store.add_question("Ambiguous relation discovery?")
    store.add_question("No answer relation discovery?")

    r = c.post("/questions/translate", follow_redirects=False)

    assert r.status_code == 303
    assert [q["status"] for q in store.questions()] == [
        "translated",
        "review_required",
        "ambiguous",
        "no_answer",
    ]
    body = unescape(c.get("/questions").text)
    assert "Translated" in body
    assert "Review required" in body
    assert "Ambiguous" in body
    assert "No answer" in body
    assert "synthetic_web_relation" in body
    assert "relation label requires review: source" in body
    assert "multiple query candidates returned conflicting answers" in body
    assert "no confirmed facts match" in body
    assert client.direct_datalog_calls == 0
    query_dl = query_path(tmp_path).read_text(encoding="utf-8")
    assert (
        'answer_q1("synthetic_web_relation") :- '
        'relation("Synthetic Web Entity", "synthetic_web_relation", O).'
    ) in query_dl
    assert "review_required" not in query_dl
    assert "ambiguous" not in query_dl
    assert "no_answer" not in query_dl


def test_translate_persists_llm_error_reason(tmp_path, monkeypatch, fake_client):
    monkeypatch.setattr(
        webapp,
        "get_client",
        lambda cfg: fake_client(error=LLMError("provider unavailable")),
    )
    c = _client(tmp_path)
    c.app.state.store.add_question("What is the sample answer?")
    r = c.post("/questions/translate", follow_redirects=False)

    assert r.status_code == 303
    q = c.app.state.store.questions()[0]
    assert q["status"] == "translation_failed"
    assert q["reason"] == "provider unavailable"
    page = c.get("/questions").text
    assert "translation_failed" in page
    assert "provider unavailable" in page


def test_translate_persists_get_client_failure_reason(tmp_path, monkeypatch):
    def raise_client_error(cfg):
        raise LLMError("missing provider credentials")

    monkeypatch.setattr(webapp, "get_client", raise_client_error)
    c = _client(tmp_path)
    c.app.state.store.add_question("What is the sample answer?")
    r = c.post("/questions/translate", follow_redirects=False)

    assert r.status_code == 303
    q = c.app.state.store.questions()[0]
    assert q["status"] == "translation_failed"
    assert q["reason"] == "missing provider credentials"
    assert (tmp_path / "facts" / "query.dl").read_text(encoding="utf-8") == ""
    page = c.get("/questions").text
    assert "translation_failed" in page
    assert "missing provider credentials" in page


def test_translate_shows_invalid_model_output_reason_in_question_row(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(webapp, "get_client", lambda cfg: object())

    def translate(store, client, *, root, allow_direct_datalog_fallback=False):
        q = store.questions(pending_only=True)[0]
        reason = "invalid model output: missing answer rule"
        store.set_question_query(
            q["id"], f'review_required("{reason}")', "review_required", reason
        )
        return [{"id": q["id"], "status": "review_required", "reason": reason}]

    monkeypatch.setattr(webapp, "translate_questions", translate)
    c = _client(tmp_path)
    c.app.state.store.add_question("Which synthetic result is available?")

    r = c.post("/questions/translate", follow_redirects=False)

    assert r.status_code == 303
    body = unescape(c.get("/questions").text)
    assert "Review required" in body
    assert "invalid model output: missing answer rule" in body


def test_questions_page_shows_non_executable_reason(tmp_path):
    c = _client(tmp_path)
    qid = c.app.state.store.add_question("Which sample item is current?")
    c.app.state.store.set_question_query(
        qid,
        'ambiguous("multiple sample entities match")',
        "ambiguous",
        "multiple sample entities match",
    )

    r = c.get("/questions")

    assert r.status_code == 200
    assert "ambiguous" in r.text
    assert "multiple sample entities match" in r.text


def test_questions_page_shows_all_visible_outcomes(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.store
    rows = [
        (
            "Translated synthetic question?",
            '.decl answer_q1(value: symbol)\nanswer_q1(O) :- relation("S", "r", O).',
            "translated",
            "",
        ),
        (
            "Review synthetic question?",
            'review_required("unsupported synthetic question")',
            "review_required",
            "unsupported synthetic question",
        ),
        (
            "No synthetic answer?",
            'no_answer("no confirmed facts match")',
            "no_answer",
            "no confirmed facts match",
        ),
        (
            "Failed synthetic translation?",
            None,
            "translation_failed",
            "provider returned invalid schema",
        ),
        (
            "Ambiguous synthetic question?",
            'ambiguous("multiple synthetic candidates matched")',
            "ambiguous",
            "multiple synthetic candidates matched",
        ),
    ]
    for text, query_dl, status, reason in rows:
        qid = store.add_question(text)
        store.set_question_query(qid, query_dl, status, reason)

    body = unescape(c.get("/questions").text)

    assert "Translated" in body
    assert "Review required" in body
    assert "No answer" in body
    assert "Translation failed" in body
    assert "Ambiguous" in body
    assert "badge-question-no-answer" in body
    assert "badge-question-translation-failed" in body
    assert "unsupported synthetic question" in body
    assert "provider returned invalid schema" in body
    assert "multiple synthetic candidates matched" in body
    assert "No engine answers yet" in body
    assert "Check each question outcome above" in body


def test_questions_page_recovers_reason_from_non_executable_query(tmp_path):
    c = _client(tmp_path)
    qid = c.app.state.store.add_question("Which synthetic item matches?")
    c.app.state.store.set_question_query(
        qid,
        'ambiguous("legacy synthetic ambiguity")',
        "ambiguous",
        "",
    )

    body = unescape(c.get("/questions").text)

    assert "legacy synthetic ambiguity" in body


def test_repair_action_accepts_valid_fix(tmp_path, monkeypatch, fake_client, intent_payload):
    monkeypatch.setattr(
        webapp,
        "get_client",
        lambda cfg: fake_client(
            intent=intent_payload(
                "lookup_object", subject="Sample Person", relation="born_in"
            )
        ),
    )
    c = _client(tmp_path)
    store = c.app.state.store
    store.add_fact("Sample Person", "born_in", "Sample Place", status="confirmed")
    qid = store.add_question("Where was Sample Person born?")
    store.set_question_query(
        qid, 'review_required("Where was Sample Person born?")', "review_required"
    )

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
    assert 'name="extraction_chunk_chars"' in r.text
    assert 'name="extraction_chunk_overlap_chars"' in r.text
    assert 'name="extraction_max_facts_per_chunk"' in r.text
    assert 'name="auto_accept_recommendations"' in r.text
    assert 'name="relation_aliases_text"' in r.text
    assert 'href="/prompts"' in r.text


def test_prompts_page_renders_default_prompt(tmp_path):
    c = _client(tmp_path)

    r = c.get("/prompts")

    assert r.status_code == 200
    assert "Prompts" in r.text
    assert "Extraction" in r.text
    assert "semantic subject-predicate-object statement" in r.text
    assert "API keys are not shown" in r.text


def test_prompts_page_switches_prompt_key(tmp_path):
    c = _client(tmp_path)

    r = c.get("/prompts", params={"prompt": "query-intent"})

    assert r.status_code == 200
    assert "Query intent" in r.text
    assert "Classify one natural-language question" in r.text
    assert "semantic subject-predicate-object statement" not in r.text


def test_prompt_save_writes_kb_policy_file(tmp_path):
    c = _client(tmp_path)

    r = c.post(
        "/prompts",
        data={
            "prompt_id": "extraction",
            "prompt_text": "Use only supplied synthetic text.",
        },
        follow_redirects=False,
    )

    assert r.status_code == 303
    assert r.headers["location"] == "/prompts?prompt=extraction"
    path = tmp_path / "policy" / "prompts" / "extraction.md"
    assert path.read_text(encoding="utf-8") == "Use only supplied synthetic text.\n"
    assert "Use only supplied synthetic text." in c.get("/prompts").text


def test_prompt_reset_deletes_override(tmp_path):
    c = _client(tmp_path)
    path = tmp_path / "policy" / "prompts" / "extraction.md"
    path.parent.mkdir(parents=True)
    path.write_text("Custom prompt.\n", encoding="utf-8")

    r = c.post(
        "/prompts/reset",
        data={"prompt_id": "extraction"},
        follow_redirects=False,
    )

    assert r.status_code == 303
    assert not path.exists()
    assert "semantic subject-predicate-object statement" in c.get("/prompts").text


def test_prompt_save_rejects_empty_text(tmp_path):
    c = _client(tmp_path)

    r = c.post(
        "/prompts",
        data={"prompt_id": "extraction", "prompt_text": "   "},
    )

    assert r.status_code == 400
    assert "prompt text is required" in r.text
    assert not (tmp_path / "policy" / "prompts" / "extraction.md").exists()


def test_prompt_save_rejects_missing_required_placeholder(tmp_path):
    c = _client(tmp_path)
    submitted = "Return a query for the supplied question."

    r = c.post(
        "/prompts",
        data={
            "prompt_id": "query-translation",
            "prompt_text": submitted,
        },
    )

    assert r.status_code == 400
    assert "{qid}" in r.text
    assert submitted in r.text
    assert not (tmp_path / "policy" / "prompts" / "query-translation.md").exists()


def test_prompt_routes_reject_unknown_key(tmp_path):
    c = _client(tmp_path)

    assert c.get("/prompts", params={"prompt": "../secret"}).status_code == 400
    r = c.post(
        "/prompts",
        data={"prompt_id": "../secret", "prompt_text": "No."},
    )
    assert r.status_code == 400


def test_prompt_editor_never_renders_api_key(tmp_path):
    cfg = Config(
        root=tmp_path,
        db_path=tmp_path / "kb.sqlite",
        provider="anthropic",
        model="m",
        api_key="supersecret",
        base_url=None,
    )
    client = TestClient(create_app(cfg))

    r = client.get("/prompts")

    assert r.status_code == 200
    assert "supersecret" not in r.text


def test_settings_saves_relation_aliases(tmp_path):
    c = _client(tmp_path)

    initial_body = c.get("/settings").text
    assert "- `제공 요소` -&gt; `provides`" in initial_body

    r = c.post(
        "/settings/relation-aliases",
        data={"relation_aliases_text": "- `title` -> `role`"},
        follow_redirects=False,
    )

    assert r.status_code == 303
    alias_path = tmp_path / "policy" / "relation-aliases.md"
    assert alias_path.read_text(encoding="utf-8") == "- `title` -> `role`\n"
    body = c.get("/settings").text
    assert "- `title` -&gt; `role`" in body
    assert "- `제공 요소` -&gt; `provides`" in body


def test_settings_saves_plain_relation_aliases(tmp_path):
    c = _client(tmp_path)

    r = c.post(
        "/settings/relation-aliases",
        data={"relation_aliases_text": "- role -> 역할"},
        follow_redirects=False,
    )

    assert r.status_code == 303
    alias_path = tmp_path / "policy" / "relation-aliases.md"
    assert alias_path.read_text(encoding="utf-8") == "- role -> 역할\n"


def test_settings_omits_conflicting_default_alias_direction(tmp_path):
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "relation-aliases.md").write_text("- `role` -> `역할`\n", encoding="utf-8")
    c = _client(tmp_path)

    body = c.get("/settings").text

    assert "- `role` -&gt; `역할`" in body
    assert "- `역할` -&gt; `role`" not in body


def test_settings_rejects_invalid_relation_aliases(tmp_path):
    c = _client(tmp_path)

    r = c.post(
        "/settings/relation-aliases",
        data={"relation_aliases_text": "- `role` -> `role`"},
    )

    assert r.status_code == 400
    assert "self-map" in r.text
    assert not (tmp_path / "policy" / "relation-aliases.md").exists()


def test_settings_rejects_malformed_relation_aliases(tmp_path):
    c = _client(tmp_path)

    r = c.post(
        "/settings/relation-aliases",
        data={"relation_aliases_text": "- role 역할"},
    )

    assert r.status_code == 400
    assert "expected `raw` -&gt; `canonical`" in r.text
    assert not (tmp_path / "policy" / "relation-aliases.md").exists()


def test_settings_save_changes_active_provider(tmp_path, monkeypatch):
    for var in ("VERINOTE_PROVIDER", "VERINOTE_MODEL", "VERINOTE_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    c = _client(tmp_path)
    r = c.post(
        "/settings",
        data={
            "provider": "ollama",
            "model": "llama3.1",
            "base_url": "",
            "extraction_chunk_chars": "500",
            "extraction_chunk_overlap_chars": "20",
            "extraction_max_facts_per_chunk": "5",
            "auto_accept_recommendations": "on",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    # the next get_client would pick the ollama adapter — no code change
    assert c.app.state.cfg.provider == "ollama"
    assert c.app.state.cfg.extraction_chunk_chars == 500
    assert c.app.state.cfg.extraction_chunk_overlap_chars == 20
    assert c.app.state.cfg.extraction_max_facts_per_chunk == 5
    assert c.app.state.cfg.auto_accept_recommendations is True
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


def test_settings_switches_active_kb_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
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
