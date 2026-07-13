# SPDX-License-Identifier: MPL-2.0
import json

import pytest

from verinote.engine import compile_dl, coverage
from verinote.store import ENGINE_STATUSES, Store, db


def _store(tmp_path) -> Store:
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    return s


def test_toggle_promotes_and_reverts(tmp_path):
    s = _store(tmp_path)
    fid = s.add_fact("A", "is_a", "B", status="needs_review", confidence=0.9)

    row = s.toggle_review(fid)
    assert row["status"] == "confirmed"

    row = s.toggle_review(fid)
    assert row["status"] == "needs_review"


def test_review_queue_excludes_confirmed(tmp_path):
    s = _store(tmp_path)
    s.add_fact("A", "r", "B", status="needs_review")
    s.add_fact("C", "r", "D", status="confirmed")
    queue = s.review_queue()
    assert [q["subject"] for q in queue] == ["A"]


def test_review_queue_page_limits_counts_and_sorts(tmp_path):
    s = _store(tmp_path)
    for idx in range(1, 56):
        s.add_fact(f"Candidate {idx:02d}", "r", "B", status="candidate")
    s.add_fact("Confirmed", "r", "B", status="confirmed")

    first = s.review_queue_page(page=1, page_size=25, sort="newest")
    second = s.review_queue_page(page=2, page_size=25, sort="newest")

    assert first.total == 55
    assert len(first.rows) == 25
    assert first.start == 1
    assert first.end == 25
    assert first.page_count == 3
    assert first.rows[0]["subject"] == "Candidate 55"
    assert second.rows[0]["subject"] == "Candidate 30"


def test_review_queue_page_clamps_invalid_params(tmp_path):
    s = _store(tmp_path)
    for idx in range(3):
        s.add_fact(f"Candidate {idx}", "r", "B", status="candidate")

    page = s.review_queue_page(page="bad", page_size=999, sort="unsafe")

    assert page.page == 1
    assert page.page_size == 50
    assert page.sort == "newest"
    assert [row["subject"] for row in page.rows] == [
        "Candidate 2",
        "Candidate 1",
        "Candidate 0",
    ]


def test_review_queue_page_sort_source_is_allowlisted(tmp_path):
    s = _store(tmp_path)
    b = s.add_source("sources/b.txt")
    a = s.add_source("sources/a.txt")
    s.add_fact("No Source", "r", "B", status="candidate")
    s.add_fact("B Source", "r", "B", status="candidate", source_id=b)
    s.add_fact("A Source", "r", "B", status="candidate", source_id=a)

    page = s.review_queue_page(page=1, page_size=25, sort="source")

    assert [row["subject"] for row in page.rows] == ["A Source", "B Source", "No Source"]


def test_review_queue_ids_and_facts_by_ids_preserve_sort_order(tmp_path):
    s = _store(tmp_path)
    ids = [
        s.add_fact("First", "r", "B", status="candidate"),
        s.add_fact("Second", "r", "B", status="candidate"),
        s.add_fact("Third", "r", "B", status="candidate"),
    ]
    s.add_fact("Confirmed", "r", "B", status="confirmed")

    newest_ids = s.review_queue_ids(sort="newest")
    rows = s.facts_by_ids([ids[1], ids[0]])

    assert newest_ids == [ids[2], ids[1], ids[0]]
    assert [row["subject"] for row in rows] == ["Second", "First"]


def test_review_log_records_decision(tmp_path):
    s = _store(tmp_path)
    fid = s.add_fact("A", "r", "B", status="needs_review")
    s.toggle_review(fid)
    rows = list(s._conn.execute("SELECT action FROM review_log"))
    assert rows[0]["action"] == "toggled"


def test_compile_dl_only_projects_triples(tmp_path):
    s = _store(tmp_path)
    # non-ASCII placeholder keeps UTF-8 round-trip coverage (not a real entity)
    s.add_fact("예시기관", "is_a", "참여기관", status="confirmed")
    s.add_fact("x", "y", "z", status="needs_review")  # must NOT appear
    dl = compile_dl(s.facts(statuses=ENGINE_STATUSES))
    assert 'relation("예시기관", "is_a", "참여기관").' in dl
    assert "needs_review" not in dl
    assert '"x"' not in dl


def test_compile_dl_escapes_quotes(tmp_path):
    s = _store(tmp_path)
    s.add_fact('a"b', "r", "c", status="confirmed")
    dl = compile_dl(s.facts(statuses=ENGINE_STATUSES))
    assert r'relation("a\"b", "r", "c").' in dl


def test_amend_fact_persists_and_audits(tmp_path):
    s = _store(tmp_path)
    fid = s.add_fact("A", "is_a", "B", status="needs_review", note="orig")
    after = s.amend_fact(fid, subject="A2", relation="became", obj="C", note="fixed")
    assert (after["subject"], after["relation"], after["object"], after["note"]) == (
        "A2",
        "became",
        "C",
        "fixed",
    )
    assert [e["action"] for e in s.fact_log(fid)] == ["amended"]


def test_amend_missing_fact_returns_none(tmp_path):
    s = _store(tmp_path)
    assert s.amend_fact(999, subject="x", relation="y", obj="z") is None


def test_delete_source_removes_source_facts_and_terms(tmp_path):
    s = _store(tmp_path)
    sid = s.add_source("sources/a.txt")
    artifact_id = s.add_source_artifact(
        source_id=sid, kind="original_text", path="sources/a.txt"
    )
    source_fact = s.add_fact("A", "r", "B", status="candidate", source_id=sid)
    unrelated_fact = s.add_fact("C", "r", "D", status="candidate")
    job_id = s.create_extraction_job(
        source_id=sid,
        artifact_id=artifact_id,
        provider="fake",
        model="m",
        total_chunks=1,
    )
    s.add_source_chunks(job_id=job_id, source_id=sid, chunks=["body"])

    deleted = s.delete_source(sid)

    assert deleted is not None
    assert deleted["path"] == "sources/a.txt"
    assert s.sources() == []
    assert [f["id"] for f in s.facts()] == [unrelated_fact]
    assert s.get_fact_terms(source_fact) is None
    assert s.get_fact_terms(unrelated_fact) is not None
    assert s.get_extraction_job(job_id) is None
    assert s.source_chunks(job_id) == []
    assert s.source_artifacts(sid) == []


def test_delete_source_removes_fact_evidence(tmp_path):
    s = _store(tmp_path)
    sid = s.add_source("sources/sample.txt")
    artifact_id = s.add_source_artifact(
        source_id=sid, kind="original_text", path="sources/sample.txt"
    )
    job_id = s.create_extraction_job(
        source_id=sid, artifact_id=artifact_id, provider="fake", model="m", total_chunks=1
    )
    chunk_id = s.add_source_chunks(
        job_id=job_id, source_id=sid, chunks=["Sample Company uses Sample Service."]
    )[0]
    fact_id = s.add_fact(
        "Sample Company",
        "uses",
        "Sample Service",
        source_id=sid,
        job_id=job_id,
    )
    evidence_id = s.add_fact_evidence(
        fact_id=fact_id,
        source_id=sid,
        artifact_id=artifact_id,
        job_id=job_id,
        chunk_id=chunk_id,
        snippet="Sample Company uses Sample Service.",
    )

    assert evidence_id > 0
    assert s.fact_evidence(fact_id)

    s.delete_source(sid)

    assert list(s._conn.execute("SELECT * FROM fact_evidence")) == []


def test_delete_missing_source_returns_none(tmp_path):
    s = _store(tmp_path)
    assert s.delete_source(999) is None


def test_clear_source_analysis_keeps_source_and_artifacts(tmp_path):
    s = _store(tmp_path)
    sid = s.add_source("sources/a.txt")
    artifact_id = s.add_source_artifact(
        source_id=sid, kind="original_text", path="sources/a.txt"
    )
    source_fact = s.add_fact("A", "r", "B", status="candidate", source_id=sid)
    unrelated_fact = s.add_fact("C", "r", "D", status="candidate")
    job_id = s.create_extraction_job(
        source_id=sid, artifact_id=artifact_id, provider="fake", model="m", total_chunks=1
    )
    s.add_source_chunks(job_id=job_id, source_id=sid, chunks=["body"])

    removed = s.clear_source_analysis(sid)

    assert removed == 1
    assert s.get_source(sid)["path"] == "sources/a.txt"
    assert [a["id"] for a in s.source_artifacts(sid)] == [artifact_id]
    assert [f["id"] for f in s.facts()] == [unrelated_fact]
    assert s.get_fact_terms(source_fact) is None
    assert s.get_fact_terms(unrelated_fact) is not None
    assert s.get_extraction_job(job_id) is None
    assert s.source_chunks(job_id) == []


def test_clear_source_analysis_removes_fact_evidence(tmp_path):
    s = _store(tmp_path)
    sid = s.add_source("sources/sample.txt")
    artifact_id = s.add_source_artifact(
        source_id=sid, kind="original_text", path="sources/sample.txt"
    )
    job_id = s.create_extraction_job(
        source_id=sid, artifact_id=artifact_id, provider="fake", model="m", total_chunks=1
    )
    chunk_id = s.add_source_chunks(job_id=job_id, source_id=sid, chunks=["evidence"])[0]
    fact_id = s.add_fact("Sample Company", "uses", "Sample Service", source_id=sid)
    s.add_fact_evidence(
        fact_id=fact_id,
        source_id=sid,
        artifact_id=artifact_id,
        job_id=job_id,
        chunk_id=chunk_id,
        snippet="evidence",
    )

    assert s.clear_source_analysis(sid) == 1

    assert list(s._conn.execute("SELECT * FROM fact_evidence")) == []


def test_fact_evidence_persists_chunk_and_span_references(tmp_path):
    s = _store(tmp_path)
    sid = s.add_source("sources/sample.txt")
    artifact_id = s.add_source_artifact(
        source_id=sid, kind="original_text", path="sources/sample.txt"
    )
    job_id = s.create_extraction_job(
        source_id=sid, artifact_id=artifact_id, provider="fake", model="m", total_chunks=1
    )
    chunk_id = s.add_source_chunks(
        job_id=job_id,
        source_id=sid,
        chunks=["Sample Company provides Sample Service."],
    )[0]
    fact_id = s.add_fact(
        "Sample Company",
        "provides",
        "Sample Service",
        source_id=sid,
        job_id=job_id,
    )

    s.add_fact_evidence(
        fact_id=fact_id,
        source_id=sid,
        artifact_id=artifact_id,
        job_id=job_id,
        chunk_id=chunk_id,
        evidence_kind="span",
        start_offset=0,
        end_offset=14,
        locator="paragraph:1",
        snippet="Sample Company",
    )

    evidence = s.fact_evidence(fact_id)[0]
    assert evidence["evidence_kind"] == "span"
    assert evidence["source_path"] == "sources/sample.txt"
    assert evidence["artifact_path"] == "sources/sample.txt"
    assert evidence["chunk_index"] == 0
    assert evidence["start_offset"] == 0
    assert evidence["end_offset"] == 14
    assert evidence["locator"] == "paragraph:1"
    assert evidence["snippet"] == "Sample Company"


def test_fact_evidence_allows_future_evidence_kinds(tmp_path):
    s = _store(tmp_path)
    sid = s.add_source("sources/sample.txt")
    fact_id = s.add_fact("Sample Company", "uses", "Sample Service", source_id=sid)

    s.add_fact_evidence(
        fact_id=fact_id,
        source_id=sid,
        evidence_kind="layout_region",
        locator="page:1:x:0:y:0",
        snippet="Sample Company",
    )

    assert s.fact_evidence(fact_id)[0]["evidence_kind"] == "layout_region"


def test_fact_evidence_bounds_snippet_text(tmp_path):
    s = _store(tmp_path)
    sid = s.add_source("sources/sample.txt")
    fact_id = s.add_fact("Sample Company", "uses", "Sample Service", source_id=sid)

    s.add_fact_evidence(
        fact_id=fact_id,
        source_id=sid,
        snippet="x" * 1200,
    )

    assert len(s.fact_evidence(fact_id)[0]["snippet"]) == 1000


def test_fact_evidence_rejects_mismatched_chunk_source(tmp_path):
    import sqlite3

    s = _store(tmp_path)
    source_a = s.add_source("sources/a.txt")
    source_b = s.add_source("sources/b.txt")
    fact_id = s.add_fact("Sample Company", "uses", "Sample Service", source_id=source_a)
    job_b = s.create_extraction_job(
        source_id=source_b, provider="fake", model="m", total_chunks=1
    )
    chunk_b = s.add_source_chunks(job_id=job_b, source_id=source_b, chunks=["body"])[0]

    with pytest.raises(sqlite3.IntegrityError, match="chunk must belong"):
        s.add_fact_evidence(
            fact_id=fact_id,
            source_id=source_a,
            chunk_id=chunk_b,
            snippet="body",
        )


def test_source_artifacts_are_upserted_and_listed_with_counts(tmp_path):
    s = _store(tmp_path)
    sid = s.add_source("sources/report.pdf", kind="binary")
    first = s.add_source_artifact(
        source_id=sid,
        kind="extracted_text",
        path="artifacts/sources/1/aaa.txt",
        checksum="aaa",
    )
    second = s.add_source_artifact(
        source_id=sid,
        kind="extracted_text",
        path="artifacts/sources/1/bbb.txt",
        checksum="bbb",
    )
    s.add_fact("A", "r", "B", status="candidate", source_id=sid)

    assert first != second
    artifacts = s.source_artifacts(sid)
    assert [(a["kind"], a["path"]) for a in artifacts] == [
        ("extracted_text", "artifacts/sources/1/aaa.txt"),
        ("extracted_text", "artifacts/sources/1/bbb.txt"),
    ]
    rows = s.sources_with_counts()
    assert rows[0]["path"] == "sources/report.pdf"
    assert rows[0]["kind"] == "binary"
    assert rows[0]["fact_count"] == 1
    assert rows[0]["candidate_count"] == 1
    assert rows[0]["needs_review_count"] == 0
    assert rows[0]["engine_count"] == 0
    assert "artifact_paths" not in rows[0].keys()


def test_sources_with_counts_includes_latest_analysis_summary(tmp_path):
    s = _store(tmp_path)
    sid = s.add_source("sources/report.pdf", kind="binary")
    artifact_id = s.add_source_artifact(
        source_id=sid,
        kind="extracted_text",
        path="artifacts/sources/1/text.txt",
        checksum="text",
    )
    old_job = s.create_extraction_job(
        source_id=sid,
        artifact_id=artifact_id,
        provider="ollama",
        model="old",
        total_chunks=1,
    )
    old_chunk = s.add_source_chunks(job_id=old_job, source_id=sid, chunks=["old"])[0]
    s.mark_extraction_job_running(old_job)
    s.mark_chunk_running(old_chunk)
    s.mark_chunk_done(old_chunk, candidates=1)
    job_id = s.create_extraction_job(
        source_id=sid,
        artifact_id=artifact_id,
        provider="ollama",
        model="qwen3.5:9b",
        total_chunks=2,
    )
    chunks = s.add_source_chunks(job_id=job_id, source_id=sid, chunks=["a", "b"])
    s.mark_extraction_job_running(job_id)
    s.mark_chunk_running(chunks[0])
    s.mark_chunk_done(chunks[0], candidates=3)
    s.mark_chunk_running(chunks[1])
    s.mark_chunk_failed(chunks[1], "provider down")

    row = s.sources_with_counts()[0]

    assert row["job_id"] == job_id
    assert row["analysis_status"] == "failed"
    assert row["completed_chunks"] == 1
    assert row["total_chunks"] == 2
    assert row["failed_chunks"] == 1
    assert row["analysis_candidate_count"] == 3
    assert row["provider"] == "ollama"
    assert row["model"] == "qwen3.5:9b"


def test_sources_with_counts_engine_count_follows_engine_statuses(tmp_path, monkeypatch):
    """The Sources page's "N confirmed" must mean the same thing as coverage.

    `engine_count` is derived from ENGINE_STATUSES, so widening the tier moves
    the Sources page in lockstep with the coverage report. Re-hard-coding
    `IN ('confirmed','accepted')` makes this fail.
    """
    s = _store(tmp_path)
    sid = s.add_source("sources/a.txt")
    s.add_fact("A", "is_a", "B", status="superseded", source_id=sid)
    s.add_fact("C", "is_a", "D", status="superseded", source_id=sid)
    s.add_fact("E", "is_a", "F", status="candidate", source_id=sid)
    superseded_count = 2

    before = s.sources_with_counts()[0]
    assert before["engine_count"] == 0
    # The per-status count columns are a different question and must not move.
    assert before["candidate_count"] == 1
    assert before["fact_count"] == 3

    monkeypatch.setattr(db, "ENGINE_STATUSES", db.ENGINE_STATUSES | {"superseded"})

    after = s.sources_with_counts()[0]
    assert after["engine_count"] == superseded_count
    assert after["engine_count"] == len(
        [r for r in s.facts(statuses=db.ENGINE_STATUSES) if r["source_id"] == sid]
    )
    assert after["candidate_count"] == 1
    assert after["fact_count"] == 3


def test_sources_with_counts_engine_count_matches_coverage(tmp_path):
    """One definition, two consumers: the Sources page and the coverage report."""
    s = _store(tmp_path)
    sid = s.add_source("sources/a.txt")
    s.add_fact("A", "is_a", "B", status="confirmed", source_id=sid)
    s.add_fact("C", "is_a", "D", status="accepted", source_id=sid)
    s.add_fact("E", "is_a", "F", status="needs_review", source_id=sid)

    row = s.sources_with_counts()[0]
    sc = coverage(s, root=tmp_path).sources[0]

    assert row["engine_count"] == sc.engine_facts
    assert row["fact_count"] == sc.total_facts


def test_extraction_job_rejects_artifact_from_another_source(tmp_path):
    s = _store(tmp_path)
    source_a = s.add_source("sources/a.pdf", kind="binary")
    source_b = s.add_source("sources/b.pdf", kind="binary")
    artifact_b = s.add_source_artifact(
        source_id=source_b,
        kind="extracted_text",
        path="artifacts/sources/2/bbb.txt",
        checksum="bbb",
    )

    import sqlite3

    try:
        s.create_extraction_job(
            source_id=source_a,
            artifact_id=artifact_b,
            provider="fake",
            model="m",
            total_chunks=1,
        )
    except sqlite3.IntegrityError as exc:
        assert "artifact must belong" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected IntegrityError")


def test_source_text_inputs_use_latest_artifact_per_source(tmp_path):
    s = _store(tmp_path)
    sid = s.add_source("sources/report.pdf", kind="binary")
    s.add_source_artifact(
        source_id=sid,
        kind="extracted_text",
        path="artifacts/sources/1/old.txt",
        checksum="old",
    )
    latest = s.add_source_artifact(
        source_id=sid,
        kind="extracted_text",
        path="artifacts/sources/1/new.txt",
        checksum="new",
    )

    rows = s.source_text_inputs()

    assert [(row["source_path"], row["artifact_id"], row["artifact_path"]) for row in rows] == [
        ("sources/report.pdf", latest, "artifacts/sources/1/new.txt")
    ]


def test_extraction_job_tracks_chunk_progress_and_retry(tmp_path):
    s = _store(tmp_path)
    sid = s.add_source("sources/a.txt")
    job_id = s.create_extraction_job(
        source_id=sid,
        provider="fake",
        model="m",
        total_chunks=2,
        message="queued",
    )
    chunk_ids = s.add_source_chunks(job_id=job_id, source_id=sid, chunks=["a", "b"])

    s.mark_extraction_job_running(job_id)
    assert s.mark_chunk_running(chunk_ids[0])["attempts"] == 1
    s.mark_chunk_done(chunk_ids[0], candidates=2)
    s.mark_chunk_running(chunk_ids[1])
    s.mark_chunk_failed(chunk_ids[1], "provider down")

    job = s.get_extraction_job(job_id)
    assert job["status"] == "failed"
    assert job["completed_chunks"] == 1
    assert job["failed_chunks"] == 1
    assert job["candidate_count"] == 2
    assert "1 chunk(s) failed" in job["message"]

    assert s.retry_failed_chunks(job_id) == 1
    assert s.source_chunks(job_id)[1]["status"] == "pending"


def test_reset_running_chunks_makes_job_resumable(tmp_path):
    s = _store(tmp_path)
    sid = s.add_source("sources/a.txt")
    job_id = s.create_extraction_job(
        source_id=sid, provider="fake", model="m", total_chunks=1
    )
    chunk_id = s.add_source_chunks(job_id=job_id, source_id=sid, chunks=["a"])[0]

    s.mark_extraction_job_running(job_id)
    s.mark_chunk_running(chunk_id)

    assert s.reset_running_chunks(job_id) == 1
    assert s.next_pending_chunk(job_id)["id"] == chunk_id
    assert s.get_extraction_job(job_id)["status"] == "pending"


def test_mark_chunk_running_returns_none_when_chunk_already_claimed(tmp_path):
    s = _store(tmp_path)
    sid = s.add_source("sources/a.txt")
    job_id = s.create_extraction_job(
        source_id=sid, provider="fake", model="m", total_chunks=1
    )
    chunk_id = s.add_source_chunks(job_id=job_id, source_id=sid, chunks=["a"])[0]

    assert s.mark_chunk_running(chunk_id) is not None
    assert s.mark_chunk_running(chunk_id) is None


def test_fact_log_orders_decisions(tmp_path):
    s = _store(tmp_path)
    fid = s.add_fact("A", "r", "B", status="needs_review")
    s.toggle_review(fid)
    s.amend_fact(fid, subject="A", relation="r", obj="B2")
    assert [e["action"] for e in s.fact_log(fid)] == ["toggled", "amended"]


def test_fact_events_record_creation_and_review_lifecycle(tmp_path):
    s = _store(tmp_path)
    sid = s.add_source("sources/sample.txt")
    fid = s.add_fact(
        "Sample Company",
        "uses",
        "Sample Service",
        status="needs_review",
        source_id=sid,
    )

    s.toggle_review(fid)
    s.amend_fact(
        fid,
        subject="Sample Company",
        relation="uses",
        obj="Sample Service v2",
    )

    events = s.fact_events(fid)
    assert [event["event_type"] for event in events] == [
        "candidate_created",
        "toggled",
        "amended",
    ]
    assert [event["actor"] for event in events] == ["system", "human", "human"]
    assert [event["action"] for event in s.fact_log(fid)] == ["toggled", "amended"]
    assert json.loads(events[0]["after_json"]) == {
        "status": "needs_review",
        "run_id": None,
        "has_note": False,
    }
    assert json.loads(events[-1]["after_json"])["object"] == "Sample Service v2"


def test_fact_events_can_represent_rule_and_reanalysis_events(tmp_path):
    s = _store(tmp_path)
    sid = s.add_source("sources/sample.txt")
    job_id = s.create_extraction_job(
        source_id=sid,
        provider="fake",
        model="sample-model",
        total_chunks=1,
    )
    fid = s.add_fact(
        "Sample Company",
        "uses",
        "Sample Service",
        status="candidate",
        source_id=sid,
        job_id=job_id,
    )

    s.add_fact_event(
        fact_id=fid,
        event_type="auto_accept_recommended",
        actor="rule",
        source_id=sid,
        job_id=job_id,
        rule_name="corroborated_no_conflict",
        after={"support_sources": 2},
    )
    s.add_fact_event(
        fact_id=fid,
        event_type="reanalyzed",
        actor="system",
        source_id=sid,
        job_id=job_id,
        after={"replacement_candidate_id": 123},
    )

    events = s.fact_events(fid)
    assert [event["event_type"] for event in events] == [
        "candidate_created",
        "auto_accept_recommended",
        "reanalyzed",
    ]
    assert events[1]["actor"] == "rule"
    assert events[1]["rule_name"] == "corroborated_no_conflict"
    assert json.loads(events[1]["after_json"])["support_sources"] == 2
    assert json.loads(events[2]["after_json"])["replacement_candidate_id"] == 123


def test_extraction_job_records_lifecycle_events(tmp_path):
    s = _store(tmp_path)
    sid = s.add_source("sources/sample.txt")
    failed_job = s.create_extraction_job(
        source_id=sid,
        provider="fake",
        model="sample-model",
        total_chunks=1,
    )
    failed_chunk = s.add_source_chunks(
        job_id=failed_job,
        source_id=sid,
        chunks=["Sample body"],
    )[0]

    s.mark_extraction_job_running(failed_job)
    s.mark_chunk_running(failed_chunk)
    s.mark_chunk_failed(failed_chunk, "provider down")
    s.retry_failed_chunks(failed_job)
    s.fail_extraction_job(failed_job, "analysis failed")

    done_job = s.create_extraction_job(
        source_id=sid,
        provider="fake",
        model="sample-model",
        total_chunks=1,
    )
    done_chunk = s.add_source_chunks(
        job_id=done_job,
        source_id=sid,
        chunks=["Sample body"],
    )[0]
    s.mark_extraction_job_running(done_job)
    s.mark_chunk_running(done_chunk)
    s.mark_chunk_done(done_chunk, candidates=1)
    s.finish_extraction_job(done_job)

    events = list(
        s._conn.execute(
            "SELECT event_type, actor, job_id, chunk_id, after_json "
            "FROM fact_events ORDER BY id"
        )
    )
    event_types = [event["event_type"] for event in events]
    assert event_types == [
        "extraction_job_started",
        "chunk_failed",
        "chunk_retried",
        "extraction_job_failed",
        "extraction_job_started",
        "extraction_job_completed",
    ]
    assert {event["actor"] for event in events} == {"system"}
    failed_event = [event for event in events if event["event_type"] == "chunk_failed"][0]
    assert failed_event["job_id"] == failed_job
    assert failed_event["chunk_id"] == failed_chunk
    assert json.loads(failed_event["after_json"])["error"] == "provider down"


def test_questions_add_list_and_translate(tmp_path):
    s = _store(tmp_path)
    qid = s.add_question("Where was Ada born?")
    assert [(q["id"], q["status"], q["reason"]) for q in s.questions()] == [
        (qid, "pending", "")
    ]
    assert [q["id"] for q in s.questions(pending_only=True)] == [qid]

    s.set_question_query(qid, ".decl answer_q1(value: symbol)", "translated")
    assert s.questions(pending_only=True) == []
    assert s.questions()[0]["status"] == "translated"
    assert s.questions()[0]["reason"] == ""

    s.delete_question(qid)
    assert s.questions() == []


def test_question_schema_migration_preserves_legacy_rows_and_adds_reason(tmp_path):
    db_path = tmp_path / "kb.sqlite"
    s = Store(db_path)
    s._conn.executescript(
        """
        CREATE TABLE questions (
            id         INTEGER PRIMARY KEY,
            text       TEXT NOT NULL,
            query_dl   TEXT,
            status     TEXT NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending','translated','review_required')),
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        INSERT INTO questions(id, text, query_dl, status, created_at)
        VALUES(7, 'Synthetic question?', 'review_required("needs sample policy")',
               'review_required', '2026-01-01 00:00:00');
        """
    )
    s.init_schema()

    row = s.questions()[0]
    assert row["id"] == 7
    assert row["text"] == "Synthetic question?"
    assert row["status"] == "review_required"
    assert row["reason"] == ""

    s.set_question_query(7, None, "translation_failed", "provider unavailable")
    row = s.questions()[0]
    assert row["status"] == "translation_failed"
    assert row["reason"] == "provider unavailable"
