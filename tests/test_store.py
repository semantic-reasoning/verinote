# SPDX-License-Identifier: MPL-2.0
import json
import sqlite3

import pytest

from verinote.engine import compile_dl, coverage
from verinote.engine.terms import Compound, NumberLit
from verinote.store import Store, db, engine_statuses


def _store(tmp_path) -> Store:
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    return s


def test_toggle_promotes_and_reverts(tmp_path):
    s = _store(tmp_path)
    fid = s.add_fact("A", "is_a", "B", status="needs_review", confidence=0.9)

    row = s.toggle_review(fid).fact
    assert row["status"] == "confirmed"

    row = s.toggle_review(fid).fact
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
    dl = compile_dl(s.facts(statuses=engine_statuses()))
    assert 'relation("예시기관", "is_a", "참여기관").' in dl
    assert "needs_review" not in dl
    assert '"x"' not in dl


def test_compile_dl_escapes_quotes(tmp_path):
    s = _store(tmp_path)
    s.add_fact('a"b', "r", "c", status="confirmed")
    dl = compile_dl(s.facts(statuses=engine_statuses()))
    assert r'relation("a\"b", "r", "c").' in dl


def test_amend_fact_persists_and_audits(tmp_path):
    s = _store(tmp_path)
    fid = s.add_fact("A", "is_a", "B", status="needs_review", note="orig")
    decision = s.amend_fact(
        fid, subject="A2", relation="became", obj="C", note="fixed"
    )
    after = decision.fact
    assert decision.changed is True
    assert (after["subject"], after["relation"], after["object"], after["note"]) == (
        "A2",
        "became",
        "C",
        "fixed",
    )
    assert [e["action"] for e in s.fact_log(fid)] == ["amended"]


def test_amend_missing_fact_returns_none(tmp_path):
    s = _store(tmp_path)
    decision = s.amend_fact(999, subject="x", relation="y", obj="z")

    assert decision.fact is None
    assert decision.changed is False


def test_replayed_amend_writes_no_audit_event(tmp_path):
    s = _store(tmp_path)
    fid = s.add_fact("A", "is_a", "B", status="needs_review", note="orig")
    before = dict(s.get_fact(fid))

    decision = s.amend_fact(fid, subject="A", relation="is_a", obj="B", note="orig")

    assert decision.fact is not None
    assert decision.changed is False
    assert dict(decision.fact) == before
    assert dict(s.get_fact(fid)) == before
    assert s.fact_log(fid) == []


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


def test_note_fact_reobserved_is_idempotent_per_artifact(tmp_path):
    s = _store(tmp_path)
    sid = s.add_source("sources/a.txt")
    artifact_id = s.add_source_artifact(
        source_id=sid, kind="original_text", path="sources/a.txt"
    )
    fact_id = s.add_fact("A", "is_a", "B", source_id=sid)

    assert (
        s.note_fact_reobserved(
            fact_id=fact_id, source_id=sid, artifact_id=artifact_id, snippet="A is a B"
        )
        is True
    )
    # A second re-observation of the same (fact, artifact) pair anchors nothing.
    assert (
        s.note_fact_reobserved(
            fact_id=fact_id, source_id=sid, artifact_id=artifact_id, snippet="A is a B"
        )
        is False
    )

    evidence = s.fact_evidence(fact_id)
    assert len(evidence) == 1
    assert evidence[0]["artifact_id"] == artifact_id


def test_note_fact_reobserved_anchors_each_distinct_artifact(tmp_path):
    s = _store(tmp_path)
    sid = s.add_source("sources/a.txt")
    artifact_one = s.add_source_artifact(
        source_id=sid, kind="original_text", path="sources/a-v1.txt", checksum="v1"
    )
    artifact_two = s.add_source_artifact(
        source_id=sid, kind="original_text", path="sources/a-v2.txt", checksum="v2"
    )
    fact_id = s.add_fact("A", "is_a", "B", source_id=sid)

    assert (
        s.note_fact_reobserved(
            fact_id=fact_id, source_id=sid, artifact_id=artifact_one
        )
        is True
    )
    assert (
        s.note_fact_reobserved(
            fact_id=fact_id, source_id=sid, artifact_id=artifact_two
        )
        is True
    )

    evidence = s.fact_evidence(fact_id)
    assert len(evidence) == 2
    assert {e["artifact_id"] for e in evidence} == {artifact_one, artifact_two}


def _fact_columns(store) -> set[str]:
    return {row["name"] for row in store._conn.execute("PRAGMA table_info(facts)")}


def test_fresh_facts_table_has_the_stale_column(tmp_path):
    # #329: a fresh DB built straight from schema.sql carries the staleness flag.
    s = _store(tmp_path)
    assert "stale" in _fact_columns(s)


def test_migration_adds_the_stale_column_to_a_legacy_facts_table(tmp_path):
    # A pre-#329 KB predates the column; reopening must migrate it in so a fresh
    # and a migrated DB expose the same `facts` schema. Mirrors the term_token
    # legacy-migration guard, and the column here (unlike term_token) is NOT NULL,
    # so every backfilled row must land a concrete 0 rather than NULL.
    conn = sqlite3.connect(tmp_path / "kb.sqlite")
    conn.execute(
        """
        CREATE TABLE facts (
            id INTEGER PRIMARY KEY,
            subject TEXT NOT NULL,
            relation TEXT NOT NULL,
            object TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'candidate',
            confidence REAL NOT NULL DEFAULT 0.0,
            source_id INTEGER,
            run_id INTEGER,
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute("INSERT INTO facts(subject, relation, object) VALUES('A', 'is_a', 'B')")
    conn.commit()
    conn.close()

    reopened = _store(tmp_path)
    try:
        assert "stale" in _fact_columns(reopened)
        row = reopened._conn.execute("SELECT stale FROM facts").fetchone()
        assert row["stale"] == 0
    finally:
        reopened.close()


def test_note_fact_reobserved_clears_stale_on_the_existing_anchor_path(tmp_path):
    # The drop-then-revert regression: a fact demoted stale=1 whose content returns
    # at an artifact it was ALREADY anchored to. The anchor exists, so the method
    # early-returns False with no new row -- yet it must still clear the stale bit,
    # and must leave the human's needs_review status untouched.
    s = _store(tmp_path)
    sid = s.add_source("sources/a.txt")
    artifact_id = s.add_source_artifact(
        source_id=sid, kind="original_text", path="sources/a.txt"
    )
    fact_id = s.add_fact("A", "is_a", "B", source_id=sid)
    assert (
        s.note_fact_reobserved(fact_id=fact_id, source_id=sid, artifact_id=artifact_id)
        is True
    )
    # Simulate Unit 3's future sweep demoting the citation as stale.
    s._conn.execute(
        "UPDATE facts SET status = 'needs_review', stale = 1 WHERE id = ?", (fact_id,)
    )

    reobserved = s.note_fact_reobserved(
        fact_id=fact_id, source_id=sid, artifact_id=artifact_id
    )

    assert reobserved is False  # anchor already present -- no new row
    assert len(s.fact_evidence(fact_id)) == 1
    assert s.get_fact(fact_id)["stale"] == 0  # cleared despite the early return
    assert s.get_fact(fact_id)["status"] == "needs_review"  # status left to the human


def test_note_fact_reobserved_clears_stale_on_the_new_anchor_path(tmp_path):
    # A stale fact re-observed at a genuinely NEW artifact both anchors fresh
    # evidence and clears the stale bit.
    s = _store(tmp_path)
    sid = s.add_source("sources/a.txt")
    artifact_one = s.add_source_artifact(
        source_id=sid, kind="original_text", path="sources/a-v1.txt", checksum="v1"
    )
    artifact_two = s.add_source_artifact(
        source_id=sid, kind="original_text", path="sources/a-v2.txt", checksum="v2"
    )
    fact_id = s.add_fact("A", "is_a", "B", source_id=sid)
    assert (
        s.note_fact_reobserved(fact_id=fact_id, source_id=sid, artifact_id=artifact_one)
        is True
    )
    s._conn.execute(
        "UPDATE facts SET status = 'needs_review', stale = 1 WHERE id = ?", (fact_id,)
    )

    reobserved = s.note_fact_reobserved(
        fact_id=fact_id, source_id=sid, artifact_id=artifact_two
    )

    assert reobserved is True  # a new anchor at the new artifact
    assert len(s.fact_evidence(fact_id)) == 2
    assert s.get_fact(fact_id)["stale"] == 0
    assert s.get_fact(fact_id)["status"] == "needs_review"


# --- surface_stale_engine_facts (#329 Part B) --------------------------------


def _stale_parts(store):
    """A confirmed engine-tier fact anchored ONLY at an old artifact (the stale
    citation), a survivor confirmed fact anchored at the NEW artifact (satisfies
    the empty/whiff guard and must never be swept), and the two artifact ids."""
    sid = store.add_source("sources/a.txt")
    old = store.add_source_artifact(
        source_id=sid, kind="original_text", path="sources/a-v1.txt", checksum="v1"
    )
    new = store.add_source_artifact(
        source_id=sid, kind="original_text", path="sources/a-v2.txt", checksum="v2"
    )
    stale = store.add_fact("Ada", "born_in", "London", status="confirmed", source_id=sid)
    store.add_fact_evidence(fact_id=stale, source_id=sid, artifact_id=old, snippet="London")
    survivor = store.add_fact("Ada", "lived_in", "Paris", status="confirmed", source_id=sid)
    store.add_fact_evidence(
        fact_id=survivor, source_id=sid, artifact_id=new, snippet="Paris"
    )
    return sid, old, new, stale, survivor


def _done_job_at(store, *, source_id, artifact_id):
    """A completed, clean job over `artifact_id` — `finish` forces status='done'
    with failed_chunks=0, the authoritative clean-run signal the sweep gates on."""
    job_id = store.create_extraction_job(
        source_id=source_id,
        artifact_id=artifact_id,
        provider="fake",
        model="m",
        total_chunks=1,
    )
    store.finish_extraction_job(job_id)
    return job_id


def test_surface_stale_demotes_a_confirmed_fact_with_no_current_evidence(tmp_path):
    s = _store(tmp_path)
    sid, old, new, stale, survivor = _stale_parts(s)
    job = _done_job_at(s, source_id=sid, artifact_id=new)

    demoted = s.surface_stale_engine_facts(job)

    assert [d["id"] for d in demoted] == [stale]
    # status AND stale flip together — the invariant both witness guards depend on.
    row = s.get_fact(stale)
    assert row["status"] == "needs_review"
    assert row["stale"] == 1
    # the survivor has evidence at the current artifact and is left untouched.
    survivor_row = s.get_fact(survivor)
    assert survivor_row["status"] == "confirmed"
    assert survivor_row["stale"] == 0


def test_surface_stale_demotes_an_accepted_fact_too(tmp_path):
    s = _store(tmp_path)
    sid, old, new, stale, survivor = _stale_parts(s)
    s._conn.execute("UPDATE facts SET status = 'accepted' WHERE id = ?", (stale,))
    job = _done_job_at(s, source_id=sid, artifact_id=new)

    demoted = s.surface_stale_engine_facts(job)

    assert [d["id"] for d in demoted] == [stale]
    row = s.get_fact(stale)
    assert row["status"] == "needs_review"
    assert row["stale"] == 1


def test_surface_stale_never_touches_a_superseded_fact(tmp_path):
    s = _store(tmp_path)
    sid, old, new, stale, survivor = _stale_parts(s)
    gone = s.add_fact("Ada", "died_in", "Rome", status="candidate", source_id=sid)
    s.add_fact_evidence(fact_id=gone, source_id=sid, artifact_id=old, snippet="Rome")
    s.reject_fact(gone)  # a human's terminal rejection
    job = _done_job_at(s, source_id=sid, artifact_id=new)

    demoted = s.surface_stale_engine_facts(job)

    assert gone not in [d["id"] for d in demoted]
    assert s.get_fact(gone)["status"] == "superseded"
    assert s.get_fact(gone)["stale"] == 0


def test_surface_stale_guarded_update_noops_on_a_concurrent_status_change(tmp_path):
    # The cross-connection race: a human reject lands on another connection after
    # the sweep read the row but before its guarded UPDATE. The guard's WHERE
    # status = <observed> then finds rowcount 0, so stale is NEVER stamped on a
    # fact that did not actually transition (the Gap-1 re-promotion invariant).
    db = tmp_path / "kb.sqlite"
    sweeping = Store(db)
    sweeping.init_schema()
    other = Store(db)
    sid, old, new, stale, survivor = _stale_parts(sweeping)
    job = _done_job_at(sweeping, source_id=sid, artifact_id=new)

    real_get_fact = sweeping.get_fact
    interleaved = []

    def reject_once_after_the_read(target_id):
        row = real_get_fact(target_id)
        if target_id == stale and not interleaved:
            interleaved.append(True)
            other.reject_fact(stale)  # the other connection's human rejects it
        return row

    sweeping.get_fact = reject_once_after_the_read
    demoted = sweeping.surface_stale_engine_facts(job)

    assert interleaved, "the concurrent reject never landed — the test proves nothing"
    assert demoted == []  # the guard refused the stale write
    assert other.get_fact(stale)["status"] == "superseded"
    assert other.get_fact(stale)["stale"] == 0  # never stamped on a non-transition


def test_surface_stale_returns_empty_when_the_job_artifact_is_null(tmp_path):
    s = _store(tmp_path)
    sid, old, new, stale, survivor = _stale_parts(s)
    # A completed job with no artifact of its own cannot define "current artifact".
    null_job = s.create_extraction_job(
        source_id=sid, artifact_id=None, provider="fake", model="m", total_chunks=1
    )
    s.finish_extraction_job(null_job)

    assert s.surface_stale_engine_facts(null_job) == []
    assert s.get_fact(stale)["status"] == "confirmed"


def test_surface_stale_returns_empty_for_a_non_done_job(tmp_path):
    s = _store(tmp_path)
    sid, old, new, stale, survivor = _stale_parts(s)
    running = s.create_extraction_job(
        source_id=sid, artifact_id=new, provider="fake", model="m", total_chunks=1
    )
    s.mark_extraction_job_running(running)
    assert s.get_extraction_job(running)["status"] == "running"

    assert s.surface_stale_engine_facts(running) == []
    assert s.get_fact(stale)["status"] == "confirmed"


def test_surface_stale_returns_empty_when_failed_chunks_is_nonzero(tmp_path):
    s = _store(tmp_path)
    sid, old, new, stale, survivor = _stale_parts(s)
    job = _done_job_at(s, source_id=sid, artifact_id=new)
    # The belt: a 'done' row that still carries a nonzero failed_chunks counter is
    # refused even though status=='done' (a state fail_extraction_job can leave).
    s._conn.execute("UPDATE extraction_jobs SET failed_chunks = 1 WHERE id = ?", (job,))

    assert s.surface_stale_engine_facts(job) == []
    assert s.get_fact(stale)["status"] == "confirmed"


def test_surface_stale_returns_empty_when_not_the_latest_job(tmp_path):
    s = _store(tmp_path)
    sid, old, new, stale, survivor = _stale_parts(s)
    done = _done_job_at(s, source_id=sid, artifact_id=new)
    # A newer job supersedes it: the older done job ran over a now-superseded view.
    s.create_extraction_job(
        source_id=sid, artifact_id=new, provider="fake", model="m", total_chunks=1
    )

    assert s.surface_stale_engine_facts(done) == []
    assert s.get_fact(stale)["status"] == "confirmed"


def test_surface_stale_returns_empty_when_no_evidence_anchors_the_current_artifact(
    tmp_path,
):
    # The empty/whiff guard: a source edited to empty (or a total LLM whiff) leaves
    # NO anchor at the current artifact, so nothing is swept on pure absence.
    s = _store(tmp_path)
    sid = s.add_source("sources/a.txt")
    old = s.add_source_artifact(
        source_id=sid, kind="original_text", path="sources/a-v1.txt", checksum="v1"
    )
    new = s.add_source_artifact(
        source_id=sid, kind="original_text", path="sources/a-v2.txt", checksum="v2"
    )
    stale = s.add_fact("Ada", "born_in", "London", status="confirmed", source_id=sid)
    s.add_fact_evidence(fact_id=stale, source_id=sid, artifact_id=old, snippet="London")
    job = _done_job_at(s, source_id=sid, artifact_id=new)  # no anchor at `new`

    assert s.surface_stale_engine_facts(job) == []
    assert s.get_fact(stale)["status"] == "confirmed"


def test_surface_stale_excludes_a_fact_with_only_null_artifact_evidence(tmp_path):
    s = _store(tmp_path)
    sid, old, new, stale, survivor = _stale_parts(s)
    # A legacy/non-chunked fact whose ONLY evidence carries a NULL artifact_id has
    # no artifact baseline to judge staleness against and is never swept.
    legacy = s.add_fact("Legacy", "is_a", "Fact", status="confirmed", source_id=sid)
    s.add_fact_evidence(fact_id=legacy, source_id=sid, artifact_id=None, snippet="x")
    job = _done_job_at(s, source_id=sid, artifact_id=new)

    demoted = [d["id"] for d in s.surface_stale_engine_facts(job)]

    assert legacy not in demoted
    assert s.get_fact(legacy)["status"] == "confirmed"
    # the genuinely stale fact still demotes, proving the sweep did run.
    assert stale in demoted


def test_surface_stale_emits_a_stale_citation_surfaced_event(tmp_path):
    s = _store(tmp_path)
    sid, old, new, stale, survivor = _stale_parts(s)
    job = _done_job_at(s, source_id=sid, artifact_id=new)

    s.surface_stale_engine_facts(job)

    events = [
        e for e in s.fact_events(stale) if e["event_type"] == "stale_citation_surfaced"
    ]
    assert len(events) == 1
    assert events[0]["actor"] == "system"
    assert events[0]["job_id"] == job  # keyed to THIS sweep's job, not the origin
    assert events[0]["source_id"] == sid
    # the untouched survivor gets no such event.
    assert [
        e
        for e in s.fact_events(survivor)
        if e["event_type"] == "stale_citation_surfaced"
    ] == []


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
    s.finish_extraction_job(job_id)  # terminalise to `failed` (#337)

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


def test_sources_with_counts_engine_count_matches_coverage(tmp_path, monkeypatch):
    """One definition, two consumers: the Sources page and the coverage report.

    Checked under a mutated tier as well — agreement between two consumers that
    both read the same hard-coded literal would be agreement on a wrong number.
    """
    s = _store(tmp_path)
    sid = s.add_source("sources/a.txt")
    s.add_fact("A", "is_a", "B", status="confirmed", source_id=sid)
    s.add_fact("C", "is_a", "D", status="accepted", source_id=sid)
    s.add_fact("E", "is_a", "F", status="needs_review", source_id=sid)
    s.add_fact("G", "is_a", "H", status="superseded", source_id=sid)

    row = s.sources_with_counts()[0]
    sc = coverage(s, root=tmp_path).sources[0]
    assert row["engine_count"] == sc.engine_facts == 2
    assert row["fact_count"] == sc.total_facts

    monkeypatch.setattr(db, "ENGINE_STATUSES", db.ENGINE_STATUSES | {"superseded"})

    row = s.sources_with_counts()[0]
    sc = coverage(s, root=tmp_path).sources[0]
    assert row["engine_count"] == sc.engine_facts == 3
    assert row["fact_count"] == sc.total_facts


def test_status_filter_rejects_an_empty_tier(tmp_path):
    """An empty tier must crash, not quietly answer zero.

    SQLite returns zero rows for `status IN ()` rather than raising, so an empty
    constant would silently make coverage call every source a gap and make
    `accept_review_facts_for_source` promote nothing while reporting success.
    """
    s = _store(tmp_path)
    s.add_fact("A", "is_a", "B", status="confirmed")

    # Positive: a populated tier filters normally.
    assert len(s.facts(statuses=engine_statuses())) == 1
    # None still means "no filter at all", not "an empty filter".
    assert len(s.facts(statuses=None)) == 1

    # Negative: an empty tier is refused rather than answered with zero rows.
    with pytest.raises(ValueError, match="must not be empty"):
        s.facts(statuses=frozenset())
    with pytest.raises(ValueError, match="must not be empty"):
        s.facts(statuses=[])


def test_engine_reads_are_refused_when_the_engine_tier_is_empty(tmp_path, monkeypatch):
    """The guard covers the real engine-input paths, not just the helper."""
    s = _store(tmp_path)
    sid = s.add_source("sources/a.txt")
    s.add_fact("A", "is_a", "B", status="confirmed", source_id=sid)

    monkeypatch.setattr(db, "ENGINE_STATUSES", frozenset())

    with pytest.raises(ValueError, match="must not be empty"):
        s.source_fact_counts()
    with pytest.raises(ValueError, match="must not be empty"):
        s.sources_with_counts()
    with pytest.raises(ValueError, match="must not be empty"):
        s.facts(statuses=db.ENGINE_STATUSES)


def test_review_promotion_is_refused_when_the_review_tier_is_empty(tmp_path, monkeypatch):
    """An empty review tier must not silently no-op the human gate."""
    s = _store(tmp_path)
    sid = s.add_source("sources/a.txt")
    s.add_fact("A", "is_a", "B", status="needs_review", source_id=sid)

    monkeypatch.setattr(db, "REVIEW_STATUSES", frozenset())

    with pytest.raises(ValueError, match="must not be empty"):
        s.accept_review_facts_for_source(sid)
    with pytest.raises(ValueError, match="must not be empty"):
        s.review_queue_page()

    # The fact was not promoted behind our back.
    assert s.get_fact(1)["status"] == "needs_review"


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

    # The job is still owned and mid-run, so it stays `running` even with a failed
    # chunk (#337); the counters and candidate tally track progress underneath.
    job = s.get_extraction_job(job_id)
    assert job["status"] == "running"
    assert job["completed_chunks"] == 1
    assert job["failed_chunks"] == 1
    assert job["candidate_count"] == 2
    assert "1/2 chunk(s) complete" in job["message"]

    # Only an explicit finish terminalises the job to `failed`; that is the state
    # the retry claim can then take.
    s.finish_extraction_job(job_id)
    assert s.get_extraction_job(job_id)["status"] == "failed"
    assert s.claim_extraction_job_for_retry(job_id, max_attempts=None) is True
    assert s.source_chunks(job_id)[1]["status"] == "pending"


def test_claim_pending_extraction_job_is_exclusive_across_connections(tmp_path):
    """The `pending`->`running` flip is the ownership handshake: exactly one wins (#240)."""
    db_path = tmp_path / "kb.sqlite"
    store_a = Store(db_path)
    store_a.init_schema()
    sid = store_a.add_source("sources/a.txt")
    job_id = store_a.create_extraction_job(
        source_id=sid, provider="fake", model="m", total_chunks=1
    )
    store_a.add_source_chunks(job_id=job_id, source_id=sid, chunks=["a"])
    store_b = Store(db_path)  # a second process's connection to the same KB file

    assert store_a.claim_pending_extraction_job(job_id) is True
    assert store_b.claim_pending_extraction_job(job_id) is False

    assert store_a.get_extraction_job(job_id)["status"] == "running"
    started = list(
        store_a._conn.execute(
            "SELECT id FROM fact_events WHERE event_type = 'extraction_job_started'"
        )
    )
    assert len(started) == 1  # only the winner recorded a claim
    store_a.close()
    store_b.close()


def test_claim_pending_extraction_job_rejects_non_pending(tmp_path):
    """Only `pending` is claimable; a decided or in-flight job is left untouched (#240)."""
    s = _store(tmp_path)
    sid = s.add_source("sources/a.txt")

    def _fresh_job(status_setup) -> int:
        job_id = s.create_extraction_job(
            source_id=sid, provider="fake", model="m", total_chunks=1
        )
        s.add_source_chunks(job_id=job_id, source_id=sid, chunks=["a"])
        status_setup(job_id)
        return job_id

    def _started_count(job_id: int) -> int:
        return len(
            list(
                s._conn.execute(
                    "SELECT id FROM fact_events "
                    "WHERE event_type = 'extraction_job_started' AND job_id = ?",
                    (job_id,),
                )
            )
        )

    cases = {
        "running": lambda jid: s.mark_extraction_job_running(jid),
        "done": lambda jid: s.finish_extraction_job(jid),
        "failed": lambda jid: s.fail_extraction_job(jid, "boom"),
        "canceled": lambda jid: s._conn.execute(
            "UPDATE extraction_jobs SET status = 'canceled' WHERE id = ?", (jid,)
        ),
    }
    for status, setup in cases.items():
        job_id = _fresh_job(setup)
        before_started = _started_count(job_id)

        assert s.claim_pending_extraction_job(job_id) is False, status

        assert s.get_extraction_job(job_id)["status"] == status
        assert _started_count(job_id) == before_started  # no new claim recorded


def test_claim_pending_extraction_job_reclaims_a_stray_running_chunk(tmp_path):
    """A claim reclaims a stray `running` chunk WITHOUT flipping the job back out of
    `running` — the reason the reclaim is a raw update, not a helper that recomputes
    job status from the chunks (#240)."""
    s = _store(tmp_path)
    sid = s.add_source("sources/a.txt")
    job_id = s.create_extraction_job(
        source_id=sid, provider="fake", model="m", total_chunks=2
    )
    chunk_ids = s.add_source_chunks(job_id=job_id, source_id=sid, chunks=["a", "b"])
    # A crash can leave the job `pending` (see rollback) with a chunk still
    # `running`; force exactly that shape without disturbing the job status.
    s._conn.execute(
        "UPDATE source_chunks SET status = 'running' WHERE id = ?", (chunk_ids[0],)
    )
    assert s.get_extraction_job(job_id)["status"] == "pending"

    assert s.claim_pending_extraction_job(job_id) is True

    # The CAS set the job `running` and the reclaim is a raw update, so the job
    # stays `running`: the reclaim never routes through `_refresh_extraction_job`,
    # which since #337 would keep an owned job `running` anyway.
    assert s.get_extraction_job(job_id)["status"] == "running"
    assert s.source_chunks(job_id)[0]["status"] == "pending"  # stray chunk reclaimed
    assert s.next_pending_chunk(job_id)["id"] == chunk_ids[0]


def test_claim_extraction_job_for_retry_is_exclusive_across_connections(tmp_path):
    """The retry claim CASes `failed`->`running` like the pending claim: exactly one
    of two racing connections wins, and the loser resets nothing (#323)."""
    db_path = tmp_path / "kb.sqlite"
    store_a = Store(db_path)
    store_a.init_schema()
    sid = store_a.add_source("sources/a.txt")
    job_id = store_a.create_extraction_job(
        source_id=sid, provider="fake", model="m", total_chunks=1
    )
    chunk_id = store_a.add_source_chunks(job_id=job_id, source_id=sid, chunks=["a"])[0]
    store_a.mark_extraction_job_running(job_id)
    store_a.mark_chunk_running(chunk_id)
    store_a.mark_chunk_failed(chunk_id, "provider down")  # chunk 'failed'
    store_a.finish_extraction_job(job_id)  # terminalise: job now genuinely 'failed'
    store_b = Store(db_path)  # a second process's connection to the same KB file

    assert store_a.claim_extraction_job_for_retry(job_id, max_attempts=None) is True
    assert store_b.claim_extraction_job_for_retry(job_id, max_attempts=None) is False

    assert store_a.get_extraction_job(job_id)["status"] == "running"  # the winner owns it
    chunk = store_a.get_source_chunk(chunk_id)
    assert chunk["status"] == "pending"  # reset by the winner
    assert chunk["attempts"] == 1  # the loser did not touch it
    retried = list(
        store_a._conn.execute(
            "SELECT id FROM fact_events WHERE event_type = 'chunk_retried'"
        )
    )
    assert len(retried) == 1  # only the winner recorded a retry; the loser wrote nothing
    store_a.close()
    store_b.close()


def test_claim_extraction_job_for_retry_respects_the_attempts_cap(tmp_path):
    """A capped auto-retry leaves a chunk at/over the cap `failed`; `max_attempts=None`
    (the human override) resets it regardless (#323)."""
    s = _store(tmp_path)
    sid = s.add_source("sources/a.txt")
    job_id = s.create_extraction_job(
        source_id=sid, provider="fake", model="m", total_chunks=1
    )
    chunk_id = s.add_source_chunks(job_id=job_id, source_id=sid, chunks=["a"])[0]
    s.mark_extraction_job_running(job_id)
    s.mark_chunk_running(chunk_id)
    s.mark_chunk_failed(chunk_id, "boom")
    # Force the chunk to the cap without disturbing its `failed` status.
    s._conn.execute("UPDATE source_chunks SET attempts = 2 WHERE id = ?", (chunk_id,))
    s.finish_extraction_job(job_id)  # terminalise the job to `failed` first (#337)

    # attempts (2) is NOT < cap (2): the claim still takes ownership but resets nothing.
    assert s.claim_extraction_job_for_retry(job_id, max_attempts=2) is True
    assert s.get_source_chunk(chunk_id)["status"] == "failed"  # exhausted, left alone

    # The human-override path resets even an exhausted chunk.
    s._conn.execute(
        "UPDATE extraction_jobs SET status = 'failed' WHERE id = ?", (job_id,)
    )
    assert s.claim_extraction_job_for_retry(job_id, max_attempts=None) is True
    assert s.get_source_chunk(chunk_id)["status"] == "pending"  # reset regardless of cap


def test_failed_chunk_attempt_status_counts_failed_and_exhausted(tmp_path):
    """`failed_chunk_attempt_status` reports (failed, exhausted) so planning can decide
    retry-vs-give-up without mutating anything (#323)."""
    s = _store(tmp_path)
    sid = s.add_source("sources/a.txt")
    job_id = s.create_extraction_job(
        source_id=sid, provider="fake", model="m", total_chunks=3
    )
    chunk_ids = s.add_source_chunks(job_id=job_id, source_id=sid, chunks=["a", "b", "c"])

    assert s.failed_chunk_attempt_status(job_id, max_attempts=2) == (0, 0)  # none failed

    # One failed but still under the cap: counted as failed, not exhausted.
    s._conn.execute(
        "UPDATE source_chunks SET status = 'failed', attempts = 1 WHERE id = ?",
        (chunk_ids[0],),
    )
    assert s.failed_chunk_attempt_status(job_id, max_attempts=2) == (1, 0)

    # A second failed at the cap (attempts >= max) counts toward exhausted too.
    s._conn.execute(
        "UPDATE source_chunks SET status = 'failed', attempts = 2 WHERE id = ?",
        (chunk_ids[1],),
    )
    assert s.failed_chunk_attempt_status(job_id, max_attempts=2) == (2, 1)


def test_claim_extraction_job_for_retry_refuses_a_canceled_job(tmp_path):
    """The CAS admits only `pending`/`failed`, so a canceled job is left entirely
    alone: no reset, no ownership, no audit event — the planner's done/canceled
    gate is the primary guard, this is the store's own defense-in-depth (#323)."""
    s = _store(tmp_path)
    sid = s.add_source("sources/a.txt")
    job_id = s.create_extraction_job(
        source_id=sid, provider="fake", model="m", total_chunks=1
    )
    chunk_id = s.add_source_chunks(job_id=job_id, source_id=sid, chunks=["a"])[0]
    s.mark_extraction_job_running(job_id)
    s.mark_chunk_running(chunk_id)
    s.mark_chunk_failed(chunk_id, "provider down")  # chunk 'failed'; job 'running'
    s._conn.execute(
        "UPDATE extraction_jobs SET status = 'canceled' WHERE id = ?", (job_id,)
    )
    events_before = s._conn.execute("SELECT COUNT(*) AS n FROM fact_events").fetchone()["n"]

    assert s.claim_extraction_job_for_retry(job_id, max_attempts=None) is False

    assert s.get_extraction_job(job_id)["status"] == "canceled"  # untouched
    assert s.get_source_chunk(chunk_id)["status"] == "failed"  # not reset
    events_after = s._conn.execute("SELECT COUNT(*) AS n FROM fact_events").fetchone()["n"]
    assert events_after == events_before  # no chunk_retried / extraction_job_started


def test_claim_extraction_job_for_retry_resets_only_under_cap_chunks_in_a_mixed_job(
    tmp_path,
):
    """A job carrying a MIX of under-cap and exhausted failed chunks resets — and
    audits — only the ones still under the cap; the exhausted one stays `failed`
    with no `chunk_retried` event (#323)."""
    s = _store(tmp_path)
    sid = s.add_source("sources/a.txt")
    job_id = s.create_extraction_job(
        source_id=sid, provider="fake", model="m", total_chunks=2
    )
    chunk_ids = s.add_source_chunks(job_id=job_id, source_id=sid, chunks=["a", "b"])
    s.mark_extraction_job_running(job_id)
    for chunk_id in chunk_ids:
        s.mark_chunk_running(chunk_id)
        s.mark_chunk_failed(chunk_id, "boom")
    s._conn.execute(
        "UPDATE source_chunks SET attempts = 1 WHERE id = ?", (chunk_ids[0],)
    )  # under the cap
    s._conn.execute(
        "UPDATE source_chunks SET attempts = 3 WHERE id = ?", (chunk_ids[1],)
    )  # at the cap
    s.finish_extraction_job(job_id)  # terminalise the job to `failed` first (#337)

    assert s.claim_extraction_job_for_retry(job_id, max_attempts=3) is True

    assert s.get_source_chunk(chunk_ids[0])["status"] == "pending"  # reset
    assert s.get_source_chunk(chunk_ids[1])["status"] == "failed"  # exhausted, left alone
    retried = list(
        s._conn.execute(
            "SELECT chunk_id FROM fact_events WHERE event_type = 'chunk_retried'"
        )
    )
    assert [row["chunk_id"] for row in retried] == [chunk_ids[0]]  # only the reset chunk


def test_claim_extraction_job_for_retry_reclaims_a_stray_running_chunk_on_a_failed_job(
    tmp_path,
):
    """A non-LLMError crash mid-chunk can leave a stray `running` chunk on a job the
    web worker then marks `failed` — a state the pending-claim path never sees. The
    retry claim reclaims it to `pending` under the same lock that takes ownership."""
    s = _store(tmp_path)
    sid = s.add_source("sources/a.txt")
    job_id = s.create_extraction_job(
        source_id=sid, provider="fake", model="m", total_chunks=1
    )
    chunk_id = s.add_source_chunks(job_id=job_id, source_id=sid, chunks=["a"])[0]
    s.mark_extraction_job_running(job_id)
    s.mark_chunk_running(chunk_id)  # 'running', attempts = 1
    # The job fails while its chunk is still in flight (a crash the worker's
    # `except Exception` reported), leaving a stray `running` chunk on a `failed` job.
    s._conn.execute(
        "UPDATE extraction_jobs SET status = 'failed' WHERE id = ?", (job_id,)
    )
    assert s.get_source_chunk(chunk_id)["status"] == "running"

    assert s.claim_extraction_job_for_retry(job_id, max_attempts=3) is True

    assert s.get_extraction_job(job_id)["status"] == "running"  # claimed
    assert s.get_source_chunk(chunk_id)["status"] == "pending"  # stray reclaimed


def test_mark_chunk_done_keeps_an_owned_job_running_against_a_second_claim(tmp_path):
    """W1 (#337): a chunk finishing mid-run must not drop an owned job to `pending`
    where a second connection's pending-claim could steal it.

    Between one chunk finishing and the next being claimed, zero chunks are
    `running` in the DB. Recomputing job status from that aggregate used to flip
    the owner's job to `pending`, and a concurrent `claim_pending_extraction_job`
    (CAS on `status='pending'`) then matched a job already being processed.
    """
    db_path = tmp_path / "kb.sqlite"
    store_a = Store(db_path)
    store_a.init_schema()
    sid = store_a.add_source("sources/a.txt")
    job_id = store_a.create_extraction_job(
        source_id=sid, provider="fake", model="m", total_chunks=2
    )
    chunk_ids = store_a.add_source_chunks(
        job_id=job_id, source_id=sid, chunks=["a", "b"]
    )
    store_b = Store(db_path)  # a second process's connection to the same KB file

    assert store_a.claim_pending_extraction_job(job_id) is True
    store_a.mark_chunk_running(chunk_ids[0])
    store_a.mark_chunk_done(chunk_ids[0], candidates=1)

    # Zero chunks `running` now, but the job is still owned: it stays `running`.
    assert store_a.get_extraction_job(job_id)["status"] == "running"
    # So the second connection cannot claim it out from under the live owner...
    assert store_b.claim_pending_extraction_job(job_id) is False
    # ...and the owner keeps processing its remaining chunk unobstructed.
    assert store_a.mark_chunk_running(chunk_ids[1]) is not None
    store_a.close()
    store_b.close()


def test_mark_chunk_failed_keeps_an_owned_job_running_against_a_retry_claim(tmp_path):
    """W2 (#337): a chunk failing mid-run must not drop an owned job to `failed`
    where a second connection's retry-claim could steal it — and reset the owner's
    in-flight chunk out from under it.

    `claim_extraction_job_for_retry` CASes `status IN ('pending','failed')` and, on
    a match, reclaims stray `running` chunks. A mid-run failed chunk used to
    terminalise the owned job to `failed`, so the retry claim matched it and its
    stray-running reclaim rewound the owner's next chunk to `pending` — the same
    chunk then sent to the LLM a second time.
    """
    db_path = tmp_path / "kb.sqlite"
    store_a = Store(db_path)
    store_a.init_schema()
    sid = store_a.add_source("sources/a.txt")
    job_id = store_a.create_extraction_job(
        source_id=sid, provider="fake", model="m", total_chunks=2
    )
    chunk_ids = store_a.add_source_chunks(
        job_id=job_id, source_id=sid, chunks=["a", "b"]
    )
    store_b = Store(db_path)  # a second process's connection to the same KB file

    assert store_a.claim_pending_extraction_job(job_id) is True
    store_a.mark_chunk_running(chunk_ids[0])
    store_a.mark_chunk_failed(chunk_ids[0], "provider down")

    # A failed chunk mid-run does NOT terminalise the still-owned job.
    assert store_a.get_extraction_job(job_id)["status"] == "running"
    # The owner moves on and claims its next chunk as `running`.
    assert store_a.mark_chunk_running(chunk_ids[1]) is not None

    # The retry claim cannot match a `running` job, so it neither takes ownership
    # nor resets the owner's in-flight chunk.
    assert store_b.claim_extraction_job_for_retry(job_id, max_attempts=3) is False
    assert store_a.get_source_chunk(chunk_ids[1])["status"] == "running"
    store_a.close()
    store_b.close()


def test_finish_extraction_job_terminalises_a_running_job(tmp_path):
    """finish still terminalises correctly despite the running-guard (#337): a job
    carrying a failed chunk stays `running` right up until finish, then `failed`;
    a job whose chunks all finish cleanly ends `done`."""
    s = _store(tmp_path)
    sid = s.add_source("sources/a.txt")

    mixed = s.create_extraction_job(
        source_id=sid, provider="fake", model="m", total_chunks=2
    )
    mixed_chunks = s.add_source_chunks(job_id=mixed, source_id=sid, chunks=["a", "b"])
    s.mark_extraction_job_running(mixed)
    s.mark_chunk_running(mixed_chunks[0])
    s.mark_chunk_done(mixed_chunks[0], candidates=1)
    s.mark_chunk_running(mixed_chunks[1])
    s.mark_chunk_failed(mixed_chunks[1], "provider down")
    # owned and mid-run, so still `running` even though a chunk has failed
    assert s.get_extraction_job(mixed)["status"] == "running"
    s.finish_extraction_job(mixed)
    assert s.get_extraction_job(mixed)["status"] == "failed"

    clean = s.create_extraction_job(
        source_id=sid, provider="fake", model="m", total_chunks=2
    )
    clean_chunks = s.add_source_chunks(job_id=clean, source_id=sid, chunks=["a", "b"])
    s.mark_extraction_job_running(clean)
    for cid in clean_chunks:
        s.mark_chunk_running(cid)
        s.mark_chunk_done(cid, candidates=1)
    s.finish_extraction_job(clean)
    assert s.get_extraction_job(clean)["status"] == "done"


def _job_with_mixed_chunks(s, sid):
    """A job caught mid-flight: one chunk done, one failed, one in flight."""
    job_id = s.create_extraction_job(
        source_id=sid, provider="fake", model="m", total_chunks=3
    )
    chunks = s.add_source_chunks(job_id=job_id, source_id=sid, chunks=["a", "b", "c"])
    s.mark_extraction_job_running(job_id)
    s.mark_chunk_running(chunks[0])
    s.mark_chunk_done(chunks[0], candidates=2)
    s.mark_chunk_running(chunks[1])
    s.mark_chunk_failed(chunks[1], "provider down")
    s.mark_chunk_running(chunks[2])
    return job_id, chunks


def test_rollback_extraction_job_requeues_only_the_in_flight_chunk(tmp_path):
    """A halted job must be resumable: `running` is a state nothing ever resets (#194)."""
    s = _store(tmp_path)
    sid = s.add_source("sources/a.txt")
    job_id, chunks = _job_with_mixed_chunks(s, sid)

    s.rollback_extraction_job(job_id, "Halted: policy missing. Rolled back to pending.")

    job = s.get_extraction_job(job_id)
    assert job["status"] == "pending"
    assert job["message"] == "Halted: policy missing. Rolled back to pending."
    rows = s.source_chunks(job_id)
    # done work is kept (its candidate facts are real), the failure keeps its
    # error, and only the in-flight chunk goes back in the queue
    assert [row["status"] for row in rows] == ["done", "failed", "pending"]
    assert rows[1]["error"] == "provider down"
    assert rows[2]["error"] == ""
    assert s.next_pending_chunk(job_id)["id"] == chunks[2]


def test_rollback_extraction_job_leaves_the_counters_true(tmp_path):
    """Counters count `done`/`failed` chunks; a rollback changes neither."""
    s = _store(tmp_path)
    sid = s.add_source("sources/a.txt")
    job_id, _ = _job_with_mixed_chunks(s, sid)
    before = s.get_extraction_job(job_id)
    counters = (
        before["completed_chunks"],
        before["failed_chunks"],
        before["candidate_count"],
        before["total_chunks"],
    )

    s.rollback_extraction_job(job_id, "halted")

    after = s.get_extraction_job(job_id)
    assert (
        after["completed_chunks"],
        after["failed_chunks"],
        after["candidate_count"],
        after["total_chunks"],
    ) == counters == (1, 1, 2, 3)


def test_rollback_extraction_job_records_a_fact_event(tmp_path):
    """The rewind is part of the KB's history, not an invisible edit."""
    s = _store(tmp_path)
    sid = s.add_source("sources/a.txt")
    job_id, _ = _job_with_mixed_chunks(s, sid)

    s.rollback_extraction_job(job_id, "halted")

    events = list(
        s._conn.execute(
            "SELECT event_type, actor, job_id, source_id, before_json, after_json "
            "FROM fact_events WHERE event_type = 'extraction_job_rolled_back'"
        )
    )
    assert len(events) == 1
    event = events[0]
    assert event["actor"] == "system"
    assert event["job_id"] == job_id
    assert event["source_id"] == sid
    # the job was halted mid-run, so it genuinely stood at 'running' before the
    # rewind (a failed chunk no longer terminalises an owned job, #337)
    assert json.loads(event["before_json"])["status"] == "running"
    assert json.loads(event["after_json"])["status"] == "pending"


def test_rollback_extraction_job_does_not_revive_a_canceled_job(tmp_path):
    """A human cancelled it; a halt must not put it — or its chunks — back in the queue.

    Guarding the job row alone is not enough. With the chunk UPDATE left ungated,
    the in-flight chunk goes back to `pending` and `next_pending_chunk` starts
    handing out chunks of a job that is `canceled` — the queue and the job row
    disagreeing about the same job.
    """
    s = _store(tmp_path)
    sid = s.add_source("sources/a.txt")
    job_id, _ = _job_with_mixed_chunks(s, sid)
    s._conn.execute(
        "UPDATE extraction_jobs SET status = 'canceled', message = 'canceled by user' "
        "WHERE id = ?",
        (job_id,),
    )

    s.rollback_extraction_job(job_id, "halted")

    job = s.get_extraction_job(job_id)
    assert job["status"] == "canceled"
    assert job["message"] == "canceled by user"
    # the in-flight chunk stays `running`: it is not back in the queue
    assert [row["status"] for row in s.source_chunks(job_id)] == ["done", "failed", "running"]
    assert s.next_pending_chunk(job_id) is None


def test_rollback_extraction_job_records_no_event_for_a_canceled_job(tmp_path):
    """Nothing was rolled back, so claiming a rollback in the history is a lie.

    `extraction_job_rolled_back` with before == after == `canceled` is exactly the
    kind of KB self-misreport #194 exists to remove — written by the fix for it.
    """
    s = _store(tmp_path)
    sid = s.add_source("sources/a.txt")
    job_id, _ = _job_with_mixed_chunks(s, sid)
    s._conn.execute(
        "UPDATE extraction_jobs SET status = 'canceled' WHERE id = ?", (job_id,)
    )

    s.rollback_extraction_job(job_id, "halted")

    events = [
        row["event_type"]
        for row in s._conn.execute("SELECT event_type FROM fact_events WHERE job_id = ?", (job_id,))
    ]
    assert "extraction_job_rolled_back" not in events


def test_rollback_extraction_job_ignores_an_unknown_job(tmp_path):
    s = _store(tmp_path)

    s.rollback_extraction_job(9999, "halted")

    assert list(s._conn.execute("SELECT id FROM fact_events")) == []


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
    # A failed chunk leaves the owned job `running`; only finish terminalises it to
    # `failed` (a completed event) so the retry claim can then take it (#337).
    s.finish_extraction_job(failed_job)
    s.claim_extraction_job_for_retry(failed_job, max_attempts=None)
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
        "extraction_job_started",  # mark_extraction_job_running(failed_job)
        "chunk_failed",  # mark_chunk_failed
        "extraction_job_completed",  # finish_extraction_job terminalises it (#337)
        "chunk_retried",  # claim_extraction_job_for_retry resets the failed chunk
        "extraction_job_started",  # ...and takes ownership in the same locked step
        "extraction_job_failed",  # fail_extraction_job
        "extraction_job_started",  # mark_extraction_job_running(done_job)
        "extraction_job_completed",  # finish_extraction_job
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


def test_existing_fact_for_source_distinguishes_string_from_compound(tmp_path):
    s = _store(tmp_path)
    sid = s.add_source("sources/sample.txt")
    s.add_fact("A", "at", "point(1, 2)", source_id=sid)

    assert (
        s.existing_fact_for_source(
            source_id=sid,
            subject="A",
            relation="at",
            obj=Compound("point", (NumberLit(1), NumberLit(2))),
        )
        is None
    )


def test_existing_fact_for_source_distinguishes_string_from_number(tmp_path):
    s = _store(tmp_path)
    sid = s.add_source("sources/sample.txt")
    s.add_fact("A", "count", "36", source_id=sid)

    assert (
        s.existing_fact_for_source(
            source_id=sid, subject="A", relation="count", obj=NumberLit(36)
        )
        is None
    )


def test_existing_fact_for_source_matches_identical_structural_triple(tmp_path):
    s = _store(tmp_path)
    sid = s.add_source("sources/sample.txt")
    fact_id = s.add_fact(
        "A", "count", NumberLit(36), source_id=sid, status="needs_review"
    )

    existing = s.existing_fact_for_source(
        source_id=sid, subject="A", relation="count", obj=NumberLit(36)
    )
    assert existing == db.ExistingFact(fact_id=fact_id, status="needs_review")


def test_existing_fact_for_source_falls_back_for_legacy_null_token(tmp_path):
    s = _store(tmp_path)
    sid = s.add_source("sources/sample.txt")
    fact_id = s.add_fact("A", "count", NumberLit(36), source_id=sid)
    # A row written before the term_token column carries NULL there.
    s._conn.execute("UPDATE facts SET term_token = NULL WHERE id = ?", (fact_id,))

    existing = s.existing_fact_for_source(
        source_id=sid, subject="A", relation="count", obj=NumberLit(36)
    )
    assert existing is not None
    assert existing.fact_id == fact_id


def test_existing_fact_for_source_is_scoped_to_the_source(tmp_path):
    s = _store(tmp_path)
    source_a = s.add_source("sources/a.txt")
    source_b = s.add_source("sources/b.txt")
    s.add_fact("A", "count", NumberLit(36), source_id=source_a)

    assert (
        s.existing_fact_for_source(
            source_id=source_b, subject="A", relation="count", obj=NumberLit(36)
        )
        is None
    )


def test_reconcile_fact_never_resurrects_a_legacy_superseded_row(tmp_path):
    # A rejected fact whose row predates the term_token column (NULL token) must
    # still be recognised on the fallback and left superseded -- never reinserted.
    s = _store(tmp_path)
    sid = s.add_source("sources/sample.txt")
    fact_id = s.add_fact("A", "count", NumberLit(36), source_id=sid)
    s.reject_fact(fact_id)
    s._conn.execute("UPDATE facts SET term_token = NULL WHERE id = ?", (fact_id,))
    before = len(s.facts())

    result = s.reconcile_fact("A", "count", NumberLit(36), source_id=sid)

    assert result == db.FactReconcileResult(
        fact_id=fact_id, created=False, matched_status="superseded"
    )
    assert len(s.facts()) == before


def _suppression_events(store, fact_id):
    return [
        event
        for event in store.fact_events(fact_id)
        if event["event_type"] == "reextraction_suppressed"
    ]


def test_reconcile_fact_records_suppression_event_on_superseded_hit(tmp_path):
    s = _store(tmp_path)
    sid = s.add_source("sources/sample.txt")
    run_id = s.add_run(provider="fake", model="m")
    fact_id = s.add_fact("A", "count", NumberLit(36), source_id=sid)
    s.reject_fact(fact_id)
    before = len(s.facts())

    result = s.reconcile_fact(
        "A", "count", NumberLit(36), source_id=sid, run_id=run_id
    )

    assert result.created is False
    assert result.matched_status == "superseded"
    assert s.get_fact(fact_id)["status"] == "superseded"
    assert len(s.facts()) == before

    events = _suppression_events(s, fact_id)
    assert len(events) == 1
    assert events[0]["actor"] == "system"
    assert events[0]["source_id"] == sid
    assert json.loads(events[0]["after_json"]) == {
        "status": "superseded",
        "run_id": run_id,
    }


def test_reconcile_fact_emits_one_suppression_event_per_run(tmp_path):
    s = _store(tmp_path)
    sid = s.add_source("sources/sample.txt")
    fact_id = s.add_fact("A", "count", NumberLit(36), source_id=sid)
    s.reject_fact(fact_id)

    run_one = s.add_run(provider="fake", model="m")
    # Two hits in one run mimic chunk overlap re-extracting a boundary triple.
    s.reconcile_fact("A", "count", NumberLit(36), source_id=sid, run_id=run_one)
    s.reconcile_fact("A", "count", NumberLit(36), source_id=sid, run_id=run_one)
    assert len(_suppression_events(s, fact_id)) == 1

    run_two = s.add_run(provider="fake", model="m")
    s.reconcile_fact("A", "count", NumberLit(36), source_id=sid, run_id=run_two)

    events = _suppression_events(s, fact_id)
    assert len(events) == 2
    assert [json.loads(e["after_json"])["run_id"] for e in events] == [run_one, run_two]


def test_reconcile_fact_seed_path_emits_suppression_event_each_time(tmp_path):
    s = _store(tmp_path)
    sid = s.add_source("sources/sample.txt")
    fact_id = s.add_fact("A", "count", NumberLit(36), source_id=sid)
    s.reject_fact(fact_id)

    # run_id=None is the seed path: a re-seed is human-initiated and each is signal.
    s.reconcile_fact("A", "count", NumberLit(36), source_id=sid)
    s.reconcile_fact("A", "count", NumberLit(36), source_id=sid)

    events = _suppression_events(s, fact_id)
    assert len(events) == 2
    assert {json.loads(e["after_json"])["run_id"] for e in events} == {None}


def test_reconcile_fact_records_no_suppression_event_on_live_hit(tmp_path):
    s = _store(tmp_path)
    sid = s.add_source("sources/sample.txt")
    run_id = s.add_run(provider="fake", model="m")
    fact_id = s.add_fact("A", "count", NumberLit(36), source_id=sid, status="confirmed")

    result = s.reconcile_fact(
        "A", "count", NumberLit(36), source_id=sid, run_id=run_id
    )

    assert result.matched_status == "confirmed"
    assert _suppression_events(s, fact_id) == []


def test_reconcile_fact_created_path_records_only_candidate_created(tmp_path):
    s = _store(tmp_path)
    sid = s.add_source("sources/sample.txt")
    run_id = s.add_run(provider="fake", model="m")

    result = s.reconcile_fact(
        "A", "count", NumberLit(36), source_id=sid, run_id=run_id
    )

    assert result.created is True
    assert [e["event_type"] for e in s.fact_events(result.fact_id)] == [
        "candidate_created"
    ]


def test_reconcile_fact_prefers_live_row_over_superseded_duplicate(tmp_path):
    # A pre-fix source can hold both a live and a superseded row for one triple.
    # Reconcile must return the live row and suppress nothing.
    s = _store(tmp_path)
    sid = s.add_source("sources/sample.txt")
    run_id = s.add_run(provider="fake", model="m")
    rejected = s.add_fact("A", "count", NumberLit(36), source_id=sid)
    s.reject_fact(rejected)
    live = s.add_fact("A", "count", NumberLit(36), source_id=sid, status="needs_review")

    result = s.reconcile_fact(
        "A", "count", NumberLit(36), source_id=sid, run_id=run_id
    )

    assert result.fact_id == live
    assert result.matched_status == "needs_review"
    assert _suppression_events(s, rejected) == []
    assert _suppression_events(s, live) == []
